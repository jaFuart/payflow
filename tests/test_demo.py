from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import get_session
from app.demo import webhook_token
from app.main import app
from app.models import Base, Outbox, Payment, WebhookReceipt, utcnow


@pytest.fixture
async def demo_api(tmp_path, monkeypatch):
    monkeypatch.setenv("DEMO_ENABLED", "true")
    monkeypatch.setenv("DEMO_CALLBACK_BASE_URL", "http://testserver")
    get_settings.cache_clear()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'demo.sqlite'}")

    @event.listens_for(engine.sync_engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def sessions():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"X-API-Key": get_settings().api_key},
    ) as client:
        yield client, factory
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()


async def create_demo(client, scenario="success", key=None, **changes):
    response = await client.post(
        "/api/v1/demo/payments",
        headers={"Idempotency-Key": key or str(uuid4())},
        json={"scenario": scenario, **changes},
    )
    assert response.status_code == 202, response.text
    return response.json()


async def set_delivery(factory, payment_id, *, scenario=None, attempt=1, generation=0):
    async with factory() as session, session.begin():
        payment = await session.get(Payment, UUID(payment_id))
        if scenario is not None:
            payment.demo_scenario = scenario
        payment.processing_attempts = attempt
        payment.delivery_generation = generation
        payment.status = "failed" if payment.demo_scenario == "decline" else "succeeded"
        payment.processed_at = utcnow()
        return payment.webhook_url


async def test_demo_is_opt_in(demo_api, monkeypatch):
    client, _ = demo_api
    monkeypatch.setenv("DEMO_ENABLED", "false")
    get_settings.cache_clear()
    assert (
        await client.post(
            "/api/v1/demo/payments",
            json={"scenario": "success"},
            headers={"Idempotency-Key": "disabled"},
        )
    ).status_code == 404
    payment_id = uuid4()
    assert (
        await client.post(
            f"/demo/webhooks/{payment_id}/{webhook_token(payment_id)}",
            json={"payment_id": str(payment_id)},
        )
    ).status_code == 404


async def test_demo_authentication_and_validation(demo_api):
    client, factory = demo_api
    assert (
        await client.post(
            "/api/v1/demo/payments",
            json={"scenario": "success"},
            headers={"X-API-Key": "", "Idempotency-Key": "unauthenticated"},
        )
    ).status_code == 401
    assert (
        await client.post("/api/v1/demo/payments", json={"scenario": "success"})
    ).status_code == 422
    assert (
        await client.post(
            "/api/v1/demo/payments",
            json={"scenario": "success"},
            headers={"Idempotency-Key": "   "},
        )
    ).status_code == 422
    for changes in (
        {"scenario": "unknown"},
        {"scenario": "success", "amount": "0"},
        {"scenario": "success", "amount": "1.001"},
        {"scenario": "success", "currency": "GBP"},
        {"scenario": "success", "webhook_url": "http://outside.test/hook"},
    ):
        response = await client.post(
            "/api/v1/demo/payments", json=changes, headers={"Idempotency-Key": str(uuid4())}
        )
        assert response.status_code == 422
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0


async def test_demo_idempotency_ignores_generated_callback_and_checks_scenario(demo_api):
    client, factory = demo_api
    first = await create_demo(client, key="same-demo", amount="1490")
    repeated = await create_demo(client, key="same-demo", amount="1490.00")
    assert repeated == first
    response = await client.post(
        "/api/v1/demo/payments",
        headers={"Idempotency-Key": "same-demo"},
        json={"scenario": "decline"},
    )
    assert response.status_code == 409
    async with factory() as session:
        payment = await session.get(Payment, UUID(first["payment_id"]))
        assert payment.demo_scenario == "success"
        assert payment.webhook_url.endswith(f"/{payment.id}/{webhook_token(payment.id)}")
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1


@pytest.mark.parametrize(
    ("scenario", "attempt", "generation", "expected"),
    [
        ("success", 1, 0, 204),
        ("decline", 1, 0, 204),
        ("retry", 1, 0, 503),
        ("retry", 2, 0, 503),
        ("retry", 3, 0, 204),
        ("dlq", 1, 0, 503),
        ("dlq", 3, 0, 503),
        ("dlq", 1, 1, 204),
    ],
)
async def test_receiver_records_attempt_without_changing_payment(
    demo_api, scenario, attempt, generation, expected
):
    client, factory = demo_api
    accepted = await create_demo(client, scenario)
    payment_id = accepted["payment_id"]
    callback = await set_delivery(factory, payment_id, attempt=attempt, generation=generation)
    async with factory() as session:
        before = await session.get(Payment, UUID(payment_id))
        state = (before.status, before.processed_at, before.processing_attempts)
    payload = {"payment_id": payment_id, "event": "payment.processed", "amount": "1490.00"}
    for _ in range(3):
        response = await client.post(
            callback,
            json=payload,
            headers={"X-API-Key": "", "Idempotency-Key": payment_id, "X-Webhook-ID": payment_id},
        )
        assert response.status_code == expected
    async with factory() as session:
        payment = await session.get(Payment, UUID(payment_id))
        assert (payment.status, payment.processed_at, payment.processing_attempts) == state
        assert payment.delivery_generation == generation
        receipts = list((await session.scalars(select(WebhookReceipt))).all())
        assert len(receipts) == 3
        assert all(receipt.status_code == expected for receipt in receipts)
        assert all(receipt.attempt == attempt for receipt in receipts)
        assert all(receipt.delivery_generation == generation for receipt in receipts)
        assert all(receipt.payload == payload for receipt in receipts)
    assert not WebhookReceipt.__table__.foreign_keys


async def test_receiver_rejects_forged_tokens_or_mismatched_payment(demo_api):
    client, factory = demo_api
    accepted = await create_demo(client)
    payment_id = accepted["payment_id"]
    callback = await set_delivery(factory, payment_id)
    payload = {"payment_id": payment_id}
    assert (
        await client.post(f"/demo/webhooks/{payment_id}/incorrect", json=payload)
    ).status_code == 404
    assert (await client.post(callback, json={"payment_id": str(uuid4())})).status_code == 422
    assert (
        await client.post(callback, json=payload, headers={"X-Webhook-ID": str(uuid4())})
    ).status_code == 422
    unknown_id = uuid4()
    assert (
        await client.post(
            f"/demo/webhooks/{unknown_id}/{webhook_token(unknown_id)}",
            json={"payment_id": str(unknown_id)},
        )
    ).status_code == 404
    async with factory() as session, session.begin():
        payment = await session.get(Payment, UUID(payment_id))
        payment.demo_scenario = None
    assert (await client.post(callback, json=payload)).status_code == 404
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(WebhookReceipt)) == 0
