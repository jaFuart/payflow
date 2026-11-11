import hashlib
import hmac
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.models import Payment, WebhookReceipt
from app.payments import create_payment
from app.schemas import PaymentAccepted, PaymentCreate


async def require_demo() -> None:
    if not get_settings().demo_enabled:
        raise HTTPException(status_code=404, detail="Demo mode is disabled")


protected_router = APIRouter(prefix="/api/v1/demo", dependencies=[Depends(require_demo)])
receiver_router = APIRouter(prefix="/demo/webhooks", dependencies=[Depends(require_demo)])
Session = Annotated[AsyncSession, Depends(get_session)]


class DemoPaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: Literal["success", "decline", "retry", "dlq"]
    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)] = Decimal("1490.00")
    currency: Literal["RUB", "USD", "EUR"] = "RUB"
    description: str = Field(default="Демонстрационный платёж", max_length=1000)

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


def webhook_token(payment_id: UUID) -> str:
    return hmac.new(
        get_settings().api_key.encode(), str(payment_id).encode(), hashlib.sha256
    ).hexdigest()


@protected_router.post("/payments", status_code=202, response_model=PaymentAccepted)
async def create_demo_payment(
    body: DemoPaymentCreate,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
) -> PaymentAccepted:
    payment_id = uuid4()
    callback_base = get_settings().demo_callback_base_url.rstrip("/")
    payload = PaymentCreate(
        amount=body.amount,
        currency=body.currency,
        description=body.description,
        metadata={"source": "demo"},
        webhook_url=f"{callback_base}/demo/webhooks/{payment_id}/{webhook_token(payment_id)}",
    )
    payment = await create_payment(
        payload,
        session,
        idempotency_key,
        demo_scenario=body.scenario,
        payment_id=payment_id,
    )
    return PaymentAccepted(
        payment_id=payment.id,
        status=payment.status,
        created_at=payment.created_at,
    )


@receiver_router.post("/{payment_id}/{token}", include_in_schema=False)
async def receive_demo_webhook(
    payment_id: UUID,
    token: str,
    payload: dict[str, JsonValue],
    session: Session,
    idempotency_key: Annotated[str | None, Header()] = None,
    x_webhook_id: Annotated[str | None, Header()] = None,
) -> Response:
    if not hmac.compare_digest(token.encode(), webhook_token(payment_id).encode()):
        raise HTTPException(status_code=404, detail="Webhook not found")
    payment = await session.get(Payment, payment_id)
    if payment is None or payment.demo_scenario is None:
        raise HTTPException(status_code=404, detail="Webhook not found")
    if payload.get("payment_id") != str(payment_id):
        raise HTTPException(status_code=422, detail="Webhook payment_id does not match URL")
    if any(
        value is not None and value != str(payment_id) for value in (idempotency_key, x_webhook_id)
    ):
        raise HTTPException(
            status_code=422, detail="Webhook deduplication header does not match URL"
        )

    attempt = payment.processing_attempts
    generation = payment.delivery_generation
    retrying = payment.demo_scenario == "retry" and attempt < 3
    dead_lettering = payment.demo_scenario == "dlq" and generation == 0
    status_code = 503 if retrying or dead_lettering else 204
    # The worker holds a payment row lock while awaiting this HTTP response.
    # Receipts have no payment foreign key and never update the locked payment.
    session.add(
        WebhookReceipt(
            payment_id=payment_id,
            delivery_generation=generation,
            attempt=attempt,
            status_code=status_code,
            payload=payload,
        )
    )
    await session.commit()
    return Response(status_code=status_code)
