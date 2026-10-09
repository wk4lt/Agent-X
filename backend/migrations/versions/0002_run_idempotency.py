"""add backend request idempotency key

Revision ID: 0002_run_idempotency
Revises: 0001_conversation_history
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_run_idempotency"
down_revision = "0001_conversation_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_runs", sa.Column("request_key", sa.String(200), nullable=True))
    op.create_index("uq_conversation_runs_request_key", "conversation_runs", ["request_key"], unique=True)


def downgrade() -> None:
    op.drop_index("uq_conversation_runs_request_key", table_name="conversation_runs")
    op.drop_column("conversation_runs", "request_key")
