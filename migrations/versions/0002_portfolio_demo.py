"""Add demo scenarios, delivery generations and payment activity history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("payments", sa.Column("demo_scenario", sa.String(16), nullable=True))
    op.add_column(
        "payments",
        sa.Column("delivery_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.create_table(
        "payment_activities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["payment_id"], ["payments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_activities_payment_id", "payment_activities", ["payment_id"])
    op.create_table(
        "webhook_receipts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("delivery_generation", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_webhook_receipts_payment_id", "webhook_receipts", ["payment_id"])


def downgrade():
    op.drop_index("ix_webhook_receipts_payment_id", table_name="webhook_receipts")
    op.drop_table("webhook_receipts")
    op.drop_index("ix_payment_activities_payment_id", table_name="payment_activities")
    op.drop_table("payment_activities")
    op.drop_column("payments", "delivery_generation")
    op.drop_column("payments", "demo_scenario")
