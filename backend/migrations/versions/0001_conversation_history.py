"""conversation history base schema

Revision ID: 0001_conversation_history
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_conversation_history"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("principals", sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("kind", sa.String(16), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("anonymous_credentials", sa.Column("token_hash", sa.String(64), primary_key=True),
                    sa.Column("principal_id", sa.String(64), sa.ForeignKey("principals.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("revoked_at", sa.DateTime(timezone=True)),
                    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_anonymous_credentials_principal_id", "anonymous_credentials", ["principal_id"])
    op.create_index("ix_anonymous_credentials_expires_at", "anonymous_credentials", ["expires_at"])
    op.create_table("conversations", sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("owner_principal_id", sa.String(64), sa.ForeignKey("principals.id", ondelete="RESTRICT"), nullable=False),
                    sa.Column("title", sa.String(160), nullable=False),
                    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
                    sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_index("ix_conversations_owner_principal_id", "conversations", ["owner_principal_id"])
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_index("ix_conversations_deleted_at", "conversations", ["deleted_at"])
    op.create_table("conversation_messages", sa.Column("id", sa.String(64), primary_key=True),
                    sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
                    sa.Column("sequence", sa.Integer, nullable=False), sa.Column("role", sa.String(16), nullable=False),
                    sa.Column("content", sa.Text, nullable=False), sa.Column("tool_calls_json", sa.JSON, nullable=False),
                    sa.Column("tool_call_id", sa.String(128)), sa.Column("tool_name", sa.String(256)),
                    sa.Column("run_id", sa.String(64)), sa.Column("status", sa.String(32), nullable=False),
                    sa.Column("source_entry_id", sa.String(64)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
                    sa.UniqueConstraint("conversation_id", "sequence", name="uq_message_sequence"),
                    sa.UniqueConstraint("conversation_id", "source_entry_id", name="uq_message_source_entry"))
    op.create_index("ix_conversation_messages_conversation_id", "conversation_messages", ["conversation_id"])
    op.create_index("ix_conversation_messages_run_id", "conversation_messages", ["run_id"])
    op.create_table("conversation_runs", sa.Column("conversation_id", sa.String(64), sa.ForeignKey("conversations.id", ondelete="CASCADE"), primary_key=True),
                    sa.Column("run_id", sa.String(64), primary_key=True), sa.Column("task_id", sa.String(64), nullable=False, unique=True),
                    sa.Column("session_id", sa.String(64), nullable=False), sa.Column("project_id", sa.String(160), nullable=False),
                    sa.Column("input", sa.Text, nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_conversation_runs_task_id", "conversation_runs", ["task_id"])


def downgrade() -> None:
    op.drop_table("conversation_runs")
    op.drop_table("conversation_messages")
    op.drop_table("conversations")
    op.drop_table("anonymous_credentials")
    op.drop_table("principals")
