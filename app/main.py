import hmac
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Security
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.security import APIKeyHeader
from sqlalchemy import String, case, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.activity import record_activity
from app.config import get_settings
from app.db import engine, get_session
from app.models import Outbox, Payment, PaymentActivity, WebhookReceipt, utcnow
from app.payments import create_payment as save_payment
from app.schemas import (
    ActivityList,
    OverviewStats,
    PaymentAccepted,
    PaymentCreate,
    PaymentDetails,
    PaymentList,
    ReceiptList,
)

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(key: Annotated[str | None, Security(api_key_header)]):
    if key is None or not hmac.compare_digest(key.encode(), get_settings().api_key.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await engine.dispose()


app = FastAPI(
    title="MCC Basis",
    version="1.1.0",
    description="Payment processing playground with a durable event trail and webhook recovery.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
api = APIRouter(dependencies=[Depends(require_api_key)])
Session = Annotated[AsyncSession, Depends(get_session)]


def accepted(payment: Payment) -> PaymentAccepted:
    return PaymentAccepted(
        payment_id=payment.id, status=payment.status, created_at=payment.created_at
    )


def payment_details(payment: Payment) -> PaymentDetails:
    return PaymentDetails(
        **accepted(payment).model_dump(),
        amount=payment.amount,
        currency=payment.currency,
        description=payment.description,
        metadata=payment.payment_metadata,
        idempotency_key=payment.idempotency_key,
        webhook_url=payment.webhook_url,
        processed_at=payment.processed_at,
        processing_attempts=payment.processing_attempts,
        webhook_delivered_at=payment.webhook_delivered_at,
        dead_lettered_at=payment.dead_lettered_at,
        last_error=payment.last_error,
        demo_scenario=payment.demo_scenario,
        delivery_generation=payment.delivery_generation,
    )


async def require_payment(session: AsyncSession, payment_id: UUID) -> Payment:
    payment = await session.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment


@api.get("/health", include_in_schema=False)
async def health(session: Session):
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@api.get("/openapi.json", include_in_schema=False)
async def openapi():
    return app.openapi()


@api.get("/docs", include_in_schema=False)
async def docs():
    return get_swagger_ui_html(openapi_url="/openapi.json", title="MCC Basis — API")


@api.post("/api/v1/payments", status_code=202, response_model=PaymentAccepted)
async def create_payment(
    body: PaymentCreate,
    session: Session,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=255)],
):
    return accepted(await save_payment(body, session, idempotency_key))


@api.get("/api/v1/payments", response_model=PaymentList)
async def list_payments(
    session: Session,
    status: Literal["pending", "succeeded", "failed"] | None = None,
    delivery: Literal["pending", "retrying", "delivered", "dead_lettered"] | None = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
):
    conditions = []
    if status is not None:
        conditions.append(Payment.status == status)
    if delivery == "delivered":
        conditions.append(Payment.webhook_delivered_at.is_not(None))
    elif delivery == "dead_lettered":
        conditions.append(Payment.dead_lettered_at.is_not(None))
    elif delivery in {"pending", "retrying"}:
        conditions.extend(
            [Payment.webhook_delivered_at.is_(None), Payment.dead_lettered_at.is_(None)]
        )
        if delivery == "retrying":
            conditions.append(Payment.last_error.is_not(None))
        else:
            conditions.append(Payment.last_error.is_(None))
    if search and search.strip():
        term = search.strip().lower()
        conditions.append(
            or_(
                func.lower(Payment.description).contains(term, autoescape=True),
                func.lower(cast(Payment.id, String)).contains(term, autoescape=True),
            )
        )
    total = await session.scalar(select(func.count()).select_from(Payment).where(*conditions))
    payments = await session.scalars(
        select(Payment)
        .where(*conditions)
        .order_by(Payment.created_at.desc(), Payment.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return PaymentList(
        items=[payment_details(payment) for payment in payments],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@api.get("/api/v1/stats", response_model=OverviewStats)
async def overview(session: Session):
    def count_when(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    counts = (
        (
            await session.execute(
                select(
                    func.count(Payment.id).label("total"),
                    count_when(Payment.status == "pending").label("pending"),
                    count_when(Payment.status == "succeeded").label("succeeded"),
                    count_when(Payment.status == "failed").label("failed"),
                    count_when(Payment.webhook_delivered_at.is_not(None)).label("delivered"),
                    count_when(Payment.dead_lettered_at.is_not(None)).label("dead_lettered"),
                    count_when(
                        Payment.last_error.is_not(None)
                        & Payment.webhook_delivered_at.is_(None)
                        & Payment.dead_lettered_at.is_(None)
                    ).label("retrying"),
                )
            )
        )
        .one()
        ._mapping
    )
    outbox = (
        (
            await session.execute(
                select(
                    count_when(Outbox.published_at.is_(None)).label("outbox_pending"),
                    count_when(Outbox.published_at.is_not(None)).label("outbox_published"),
                )
            )
        )
        .one()
        ._mapping
    )
    return OverviewStats(
        **dict(counts),
        **dict(outbox),
        demo_enabled=get_settings().demo_enabled,
        as_of=utcnow(),
    )


@api.get("/api/v1/payments/{payment_id}", response_model=PaymentDetails)
async def get_payment(payment_id: UUID, session: Session):
    return payment_details(await require_payment(session, payment_id))


@api.get("/api/v1/payments/{payment_id}/events", response_model=ActivityList)
async def get_events(payment_id: UUID, session: Session):
    await require_payment(session, payment_id)
    events = await session.scalars(
        select(PaymentActivity)
        .where(PaymentActivity.payment_id == payment_id)
        .order_by(PaymentActivity.created_at, PaymentActivity.id)
    )
    return ActivityList(items=list(events))


@api.get("/api/v1/payments/{payment_id}/webhooks", response_model=ReceiptList)
async def get_webhooks(payment_id: UUID, session: Session):
    await require_payment(session, payment_id)
    receipts = await session.scalars(
        select(WebhookReceipt)
        .where(WebhookReceipt.payment_id == payment_id)
        .order_by(WebhookReceipt.created_at, WebhookReceipt.id)
    )
    return ReceiptList(items=list(receipts))


@api.post(
    "/api/v1/payments/{payment_id}/redeliver", status_code=202, response_model=PaymentAccepted
)
async def redeliver_webhook(payment_id: UUID, session: Session):
    payment = await session.scalar(
        select(Payment).where(Payment.id == payment_id).with_for_update()
    )
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.dead_lettered_at is None or payment.status == "pending":
        raise HTTPException(
            status_code=409, detail="Only exhausted webhook deliveries can be retried"
        )
    payment.delivery_generation += 1
    payment.processing_attempts = 0
    payment.dead_lettered_at = None
    payment.last_error = None
    now = utcnow()
    session.add(
        Outbox(
            payment_id=payment.id,
            event_type="payments.new",
            payload={
                "payment_id": str(payment.id),
                "attempt": 1,
                "generation": payment.delivery_generation,
            },
            created_at=now,
            available_at=now,
        )
    )
    record_activity(
        session,
        payment,
        "webhook.redelivery_requested",
        details={"generation": payment.delivery_generation},
    )
    await session.commit()
    return accepted(payment)


app.include_router(api)

from app.dashboard import configure_dashboard  # noqa: E402
from app.demo import protected_router, receiver_router  # noqa: E402

app.include_router(protected_router, dependencies=[Depends(require_api_key)])
app.include_router(receiver_router)
configure_dashboard(app)
