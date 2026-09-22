"""Persist conversational generation-planning state on Agent sessions.

Revision ID: 0008_generation_planning_sessions
Revises: 0007_provider_defaults_and_runtime
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_generation_planning_sessions"
down_revision = "0007_provider_defaults_and_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("agent_sessions")}
    additions = (
        sa.Column("purpose", sa.String(length=40), nullable=False, server_default="diagnosis"),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("draft_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("draft_hash", sa.String(length=128), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
    )
    for column in additions:
        if column.name not in existing:
            op.add_column("agent_sessions", column)
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_sessions")}
    if "ix_agent_sessions_purpose" not in indexes:
        op.create_index("ix_agent_sessions_purpose", "agent_sessions", ["purpose"])


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_sessions")}
    if "ix_agent_sessions_purpose" in indexes:
        op.drop_index("ix_agent_sessions_purpose", table_name="agent_sessions")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("agent_sessions")}
    for name in ("turn_count", "draft_version", "draft_hash", "draft_json", "title", "purpose"):
        if name in existing:
            op.drop_column("agent_sessions", name)
