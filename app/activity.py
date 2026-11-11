from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Payment, PaymentActivity, utcnow


def record_activity(
    session: AsyncSession,
    payment: Payment,
    event_type: str,
    *,
    attempt: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        PaymentActivity(
            payment_id=payment.id,
            event_type=event_type,
            attempt=attempt,
            details=details or {},
            created_at=utcnow(),
        )
    )
