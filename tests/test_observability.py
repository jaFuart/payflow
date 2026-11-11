import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db import get_session
from app.main import app
from app.models import Base, Outbox, Payment, PaymentActivity, WebhookReceipt


@pytest.fixture
async def observability_api(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'observability.db'}")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async def sessions():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_session] = sessions
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
            headers={"X-API-Key": get_settings().api_key},
        ) as client:
            yield client, factory
    finally:
        app.dependency_overrides.pop(get_session, None)
        await engine.dispose()


async def seed_payments(factory):
    now = datetime(2026, 11, 11, 9, tzinfo=UTC)
    definitions = [
        ("queued", "pending", "Queued order", 0, False, False, None),
        ("retrying", "succeeded", "Retry inventory", 1, False, False, "HTTP 503"),
        ("delivered", "succeeded", "Invoice 50%_off", 1, True, False, None),
        ("dead_lettered", "succeeded", "Waiting operators", 3, False, True, "HTTP 503"),
        ("declined", "failed", "Declined renewal", 1, True, False, None),
        ("processing", "pending", "Queued processing", 1, False, False, None),
    ]
    payments = {}
    async with factory() as session, session.begin():
        for index, (name, status, description, attempts, delivered, dead, error) in enumerate(
            definitions, start=1
        ):
            created_at = now + timedelta(minutes=index)
            payment = Payment(
                id=UUID(f"{index:08x}-0000-4000-8000-{index:012x}"),
                amount=Decimal("125.50"),
                currency="RUB",
                description=description,
                payment_metadata={"fixture": name},
                status=status,
                idempotency_key=f"fixture-{name}",
                request_fingerprint="f" * 64,
                webhook_url="http://client.test/webhook",
                created_at=created_at,
                processed_at=created_at + timedelta(seconds=1) if status != "pending" else None,
                processing_attempts=attempts,
                webhook_delivered_at=created_at + timedelta(seconds=2) if delivered else None,
                dead_lettered_at=created_at + timedelta(seconds=3) if dead else None,
                last_error=error,
                delivery_generation=0,
            )
            session.add(payment)
            payments[name] = payment
        await session.flush()
        for index, payment in enumerate(payments.values()):
            session.add(
                Outbox(
                    payment_id=payment.id,
                    event_type="payments.new",
                    payload={"payment_id": str(payment.id), "attempt": 1},
                    created_at=payment.created_at,
                    available_at=payment.created_at,
                    published_at=now + timedelta(hours=1) if index % 2 else None,
                    attempts=1 if index % 2 else 0,
                )
            )
    return payments


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/payments"),
        ("GET", "/api/v1/stats"),
        ("GET", f"/api/v1/payments/{UUID(int=1)}/events"),
        ("GET", f"/api/v1/payments/{UUID(int=1)}/webhooks"),
        ("POST", f"/api/v1/payments/{UUID(int=1)}/redeliver"),
    ],
)
async def test_observability_endpoints_require_api_key(observability_api, method, path):
    client, _ = observability_api
    assert (await client.request(method, path, headers={"X-API-Key": "invalid"})).status_code == 401
    client.headers.pop("X-API-Key")
    assert (await client.request(method, path)).status_code == 401


async def test_dashboard_shell_and_local_assets_are_public_without_exposing_api_key(
    observability_api,
):
    client, _ = observability_api
    client.headers.pop("X-API-Key")
    response = await client.get("/")
    assert response.status_code == 200
    assert get_settings().api_key not in response.text
    assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', response.text)
    assert assets
    for asset in assets:
        assert (await client.get(asset)).status_code == 200
    assert (await client.get("/api/v1/payments")).status_code == 401


async def test_list_is_newest_first_and_pagination_total_is_unpaged(observability_api):
    client, factory = observability_api
    payments = await seed_payments(factory)
    response = await client.get("/api/v1/payments", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == 6
    assert result["limit"] == 2
    assert result["offset"] == 1
    assert [item["payment_id"] for item in result["items"]] == [
        str(payments["declined"].id),
        str(payments["dead_lettered"].id),
    ]
    empty_page = (await client.get("/api/v1/payments", params={"offset": 20})).json()
    assert empty_page["items"] == []
    assert empty_page["total"] == 6


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ({"status": "pending"}, {"queued", "processing"}),
        ({"status": "failed"}, {"declined"}),
        ({"delivery": "delivered"}, {"delivered", "declined"}),
        ({"delivery": "dead_lettered"}, {"dead_lettered"}),
        ({"delivery": "retrying"}, {"retrying"}),
        ({"delivery": "pending"}, {"queued", "processing"}),
        ({"status": "failed", "delivery": "retrying"}, set()),
        ({"status": "succeeded", "delivery": "delivered"}, {"delivered"}),
    ],
)
async def test_list_filters_combine_payment_and_delivery_states(
    observability_api, filters, expected
):
    client, factory = observability_api
    payments = await seed_payments(factory)
    response = await client.get("/api/v1/payments", params=filters)
    assert response.status_code == 200
    result = response.json()
    assert result["total"] == len(expected)
    assert {item["payment_id"] for item in result["items"]} == {
        str(payments[name].id) for name in expected
    }


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("  QUEUED  ", {"queued", "processing"}),
        ("50%_", {"delivered"}),
        ("%", {"delivered"}),
        ("_", {"delivered"}),
        ("not-present", set()),
        ("   ", {"queued", "retrying", "delivered", "dead_lettered", "declined", "processing"}),
    ],
)
async def test_search_is_case_insensitive_and_treats_wildcards_literally(
    observability_api, query, expected
):
    client, factory = observability_api
    payments = await seed_payments(factory)
    response = await client.get("/api/v1/payments", params={"search": query})
    assert response.status_code == 200
    result = response.json()
    assert {item["payment_id"] for item in result["items"]} == {
        str(payments[name].id) for name in expected
    }
    assert result["total"] == len(expected)


async def test_search_can_find_payment_by_id_prefix(observability_api):
    client, factory = observability_api
    payments = await seed_payments(factory)
    target = payments["delivered"]
    response = await client.get("/api/v1/payments", params={"search": str(target.id)[:8]})
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["payment_id"] == str(target.id)


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"offset": 100001},
        {"status": "unknown"},
        {"delivery": "unknown"},
        {"search": "x" * 101},
    ],
)
async def test_list_rejects_invalid_filters_and_page_sizes(observability_api, params):
    client, _ = observability_api
    assert (await client.get("/api/v1/payments", params=params)).status_code == 422


async def test_stats_counts_payment_and_delivery_outcomes_separately(observability_api):
    client, factory = observability_api
    empty = (await client.get("/api/v1/stats")).json()
    count_keys = (
        "total",
        "pending",
        "succeeded",
        "failed",
        "delivered",
        "dead_lettered",
        "retrying",
        "outbox_pending",
        "outbox_published",
    )
    assert all(empty[key] == 0 for key in count_keys)
    await seed_payments(factory)
    response = await client.get("/api/v1/stats")
    assert response.status_code == 200
    result = response.json()
    assert {key: result[key] for key in count_keys} == {
        "total": 6,
        "pending": 2,
        "succeeded": 3,
        "failed": 1,
        "delivered": 2,
        "dead_lettered": 1,
        "retrying": 1,
        "outbox_pending": 3,
        "outbox_published": 3,
    }
    assert result["demo_enabled"] == get_settings().demo_enabled
    assert datetime.fromisoformat(result["as_of"]).utcoffset() == timedelta(0)


def payment_body():
    return {
        "amount": "125.50",
        "currency": "RUB",
        "description": "Created from API",
        "metadata": {"order_id": "portfolio"},
        "webhook_url": "http://client.test/webhook",
    }


async def test_create_records_one_atomic_activity_even_when_request_is_repeated(observability_api):
    client, factory = observability_api
    responses = [
        await client.post(
            "/api/v1/payments", json=payment_body(), headers={"Idempotency-Key": "activity-test"}
        )
        for _ in range(2)
    ]
    assert all(response.status_code == 202 for response in responses)
    assert responses[0].json() == responses[1].json()
    payment_id = UUID(responses[0].json()["payment_id"])
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 1
        activities = list((await session.scalars(select(PaymentActivity))).all())
    assert len(activities) == 1
    assert activities[0].payment_id == payment_id
    assert activities[0].event_type == "payment.created"
    assert activities[0].details["generation"] == 0


async def test_activity_persistence_failure_rolls_back_payment_and_outbox(observability_api):
    client, factory = observability_api

    def invalidate_activity(mapper, connection, target):
        target.event_type = None

    event.listen(PaymentActivity, "before_insert", invalidate_activity)
    try:
        response = await client.post(
            "/api/v1/payments", json=payment_body(), headers={"Idempotency-Key": "rollback-test"}
        )
    finally:
        event.remove(PaymentActivity, "before_insert", invalidate_activity)
    assert response.status_code == 500
    async with factory() as session:
        for entity in (Payment, Outbox, PaymentActivity):
            assert await session.scalar(select(func.count()).select_from(entity)) == 0


async def test_events_and_receipts_are_chronological_and_scoped_to_payment(observability_api):
    client, factory = observability_api
    payments = await seed_payments(factory)
    target = payments["delivered"]
    now = datetime(2026, 11, 11, 10, tzinfo=UTC)
    async with factory() as session, session.begin():
        for payment in (target, payments["declined"]):
            for ordinal in (2, 0, 1):
                session.add(
                    PaymentActivity(
                        payment_id=payment.id,
                        event_type=f"step.{ordinal}",
                        attempt=ordinal + 1,
                        details={"generation": 0},
                        created_at=now + timedelta(seconds=ordinal),
                    )
                )
                session.add(
                    WebhookReceipt(
                        payment_id=payment.id,
                        delivery_generation=0,
                        attempt=ordinal + 1,
                        status_code=204 if ordinal == 2 else 503,
                        payload={"payment_id": str(payment.id), "sequence": ordinal},
                        created_at=now + timedelta(seconds=ordinal),
                    )
                )
    events_response = await client.get(f"/api/v1/payments/{target.id}/events")
    receipts_response = await client.get(f"/api/v1/payments/{target.id}/webhooks")
    assert events_response.status_code == receipts_response.status_code == 200
    events = events_response.json()["items"]
    receipts = receipts_response.json()["items"]
    assert [entry["event_type"] for entry in events] == ["step.0", "step.1", "step.2"]
    assert [entry["attempt"] for entry in receipts] == [1, 2, 3]
    assert [entry["status_code"] for entry in receipts] == [503, 503, 204]
    assert all(entry["payload"]["payment_id"] == str(target.id) for entry in receipts)
    for suffix in ("events", "webhooks"):
        assert (await client.get(f"/api/v1/payments/{uuid4()}/{suffix}")).status_code == 404


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_redelivery_keeps_gateway_result_and_starts_one_fenced_delivery(
    observability_api, status
):
    client, factory = observability_api
    payments = await seed_payments(factory)
    payment_id = payments["dead_lettered"].id
    async with factory() as session, session.begin():
        payment = await session.get(Payment, payment_id)
        payment.status = status
        payment.delivery_generation = 2
        original_processed_at = payment.processed_at
        original_created_at = payment.created_at
        original_url = payment.webhook_url
        existing_outboxes = await session.scalar(select(func.count()).select_from(Outbox))
    response = await client.post(f"/api/v1/payments/{payment_id}/redeliver")
    assert response.status_code == 202
    assert response.json()["status"] == status
    assert (await client.post(f"/api/v1/payments/{payment_id}/redeliver")).status_code == 409
    async with factory() as session:
        payment = await session.get(Payment, payment_id)
        activities = list(
            (
                await session.scalars(
                    select(PaymentActivity).where(PaymentActivity.payment_id == payment_id)
                )
            ).all()
        )
        outboxes = list(
            (await session.scalars(select(Outbox).where(Outbox.payment_id == payment_id))).all()
        )
        assert (
            await session.scalar(select(func.count()).select_from(Outbox)) == existing_outboxes + 1
        )
    assert payment.status == status
    assert payment.processed_at == original_processed_at
    assert payment.created_at == original_created_at
    assert payment.webhook_url == original_url
    assert payment.processing_attempts == 0
    assert payment.delivery_generation == 3
    assert payment.dead_lettered_at is None
    assert payment.last_error is None
    assert payment.webhook_delivered_at is None
    assert len(activities) == 1
    assert activities[0].event_type == "webhook.redelivery_requested"
    assert activities[0].details["generation"] == 3
    assert len(outboxes) == 2
    assert (
        sum(
            entry.payload == {"payment_id": str(payment_id), "attempt": 1, "generation": 3}
            for entry in outboxes
        )
        == 1
    )


@pytest.mark.parametrize("name", ["queued", "retrying", "delivered", "declined", "processing"])
async def test_redelivery_rejects_non_exhausted_payments_without_writes(observability_api, name):
    client, factory = observability_api
    payments = await seed_payments(factory)
    response = await client.post(f"/api/v1/payments/{payments[name].id}/redeliver")
    assert response.status_code == 409
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Outbox)) == 6
        assert await session.scalar(select(func.count()).select_from(PaymentActivity)) == 0
    assert (await client.post(f"/api/v1/payments/{uuid4()}/redeliver")).status_code == 404
