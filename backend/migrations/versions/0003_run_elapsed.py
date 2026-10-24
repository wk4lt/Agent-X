"""Persist total answer duration for historical conversations."""
from alembic import op
import sqlalchemy as sa

revision = "0003_run_elapsed"
down_revision = "0002_run_idempotency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_runs", sa.Column("elapsed_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversation_runs", "elapsed_ms")
