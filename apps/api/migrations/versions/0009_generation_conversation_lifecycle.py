"""Add model selection and archive state to planning conversations.

Revision ID: 0009_generation_conversation_lifecycle
Revises: 0008_generation_planning_sessions
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_generation_conversation_lifecycle"
down_revision = "0008_generation_planning_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("agent_sessions")}
    if "agent_model" not in existing:
        op.add_column("agent_sessions", sa.Column("agent_model", sa.String(length=160), nullable=True))
    if "archived_at" not in existing:
        op.add_column("agent_sessions", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_sessions")}
    if "ix_agent_sessions_agent_model" not in indexes:
        op.create_index("ix_agent_sessions_agent_model", "agent_sessions", ["agent_model"])
    if "ix_agent_sessions_archived_at" not in indexes:
        op.create_index("ix_agent_sessions_archived_at", "agent_sessions", ["archived_at"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_sessions")}
    if "ix_agent_sessions_archived_at" in indexes:
        op.drop_index("ix_agent_sessions_archived_at", table_name="agent_sessions")
    if "ix_agent_sessions_agent_model" in indexes:
        op.drop_index("ix_agent_sessions_agent_model", table_name="agent_sessions")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("agent_sessions")}
    if "archived_at" in existing:
        op.drop_column("agent_sessions", "archived_at")
    if "agent_model" in existing:
        op.drop_column("agent_sessions", "agent_model")
