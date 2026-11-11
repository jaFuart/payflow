import hashlib
import json
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.activity import record_activity
from app.models import Outbox, Payment, utcnow
from app.schemas import PaymentCreate


async def create_payment(
    body: PaymentCreate,
    session: AsyncSession,
    idempotency_key: str,
    *,
    demo_scenario: str | None = None,
    payment_id: UUID | None = None,
) -> Payment:
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    payload = body.model_dump(mode="json")
    if demo_scenario is not None:
        # A retry creates a fresh candidate callback URL, but represents the same demo request.
        payload.pop("webhook_url")
        payload["demo_scenario"] = demo_scenario
    fingerprint = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    payment_id = payment_id or uuid4()
    now = utcnow()
    payment = Payment(
        id=payment_id,
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        payment_metadata=body.metadata,
        status="pending",
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        webhook_url=str(body.webhook_url),
        created_at=now,
        delivery_generation=0,
        demo_scenario=demo_scenario,
    )
    try:
        session.add(payment)
        await session.flush()
        session.add(
            Outbox(
                payment_id=payment_id,
                event_type="payments.new",
                payload={"payment_id": str(payment_id), "attempt": 1},
                created_at=now,
                available_at=now,
            )
        )
        record_activity(
            session,
            payment,
            "payment.created",
            details={"scenario": demo_scenario, "generation": 0},
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        if existing is None:
            raise
        if existing.request_fingerprint != fingerprint:
            raise HTTPException(
                status_code=409, detail="Idempotency-Key was already used for a different request"
            ) from None
        return existing
    return payment
