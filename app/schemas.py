from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, JsonValue, field_validator


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]
    currency: Literal["RUB", "USD", "EUR"]
    description: str = Field(default="", max_length=1000)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    webhook_url: Annotated[AnyHttpUrl, Field(max_length=2048)]

    @field_validator("amount")
    @classmethod
    def normalize_amount(cls, value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.01"))


class PaymentAccepted(BaseModel):
    payment_id: UUID
    status: Literal["pending", "succeeded", "failed"]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def ensure_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class PaymentDetails(PaymentAccepted):
    amount: Decimal
    currency: str
    description: str
    metadata: dict[str, JsonValue]
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None
    processing_attempts: int
    webhook_delivered_at: datetime | None
    dead_lettered_at: datetime | None
    last_error: str | None
    demo_scenario: str | None = None
    delivery_generation: int = 0


class PaymentList(BaseModel):
    items: list[PaymentDetails]
    total: int
    limit: int
    offset: int


class ActivityDetails(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_type: str
    attempt: int | None
    details: dict[str, JsonValue]
    created_at: datetime


class ActivityList(BaseModel):
    items: list[ActivityDetails]


class ReceiptDetails(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    delivery_generation: int
    attempt: int
    status_code: int
    payload: dict[str, JsonValue]
    created_at: datetime


class ReceiptList(BaseModel):
    items: list[ReceiptDetails]


class OverviewStats(BaseModel):
    total: int
    pending: int
    succeeded: int
    failed: int
    delivered: int
    dead_lettered: int
    retrying: int
    outbox_pending: int
    outbox_published: int
    demo_enabled: bool
    as_of: datetime
