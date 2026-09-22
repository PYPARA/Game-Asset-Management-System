"""Persist interactive planning input requests.

Revision ID: 0010_agent_input_requests
Revises: 0009_generation_conversation_lifecycle
"""

from alembic import op
import sqlalchemy as sa


revision = "0010_agent_input_requests"
down_revision = "0009_generation_conversation_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_input_requests",
        sa.Column("id", sa.String(48), primary_key=True),
        sa.Column("session_id", sa.String(48), sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("turn_id", sa.String(240), nullable=False),
        sa.Column("remote_thread_id", sa.String(240)),
        sa.Column("remote_turn_id", sa.String(240)),
        sa.Column("item_id", sa.String(240), nullable=False),
        sa.Column("transport_request_id", sa.String(240)),
        sa.Column("response_mode", sa.String(20), nullable=False),
        sa.Column("questions_json", sa.JSON(), nullable=False),
        sa.Column("answers_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("auto_resolution_ms", sa.Integer()),
        sa.Column("fallback_reason", sa.String(100)),
        sa.Column("client_response_id", sa.String(160)),
        sa.Column("response_turn_id", sa.String(240)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("answered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("session_id", "turn_id", "item_id", name="uq_agent_input_request_item"),
    )
    for column in ("session_id", "turn_id", "status"):
        op.create_index(f"ix_agent_input_requests_{column}", "agent_input_requests", [column])


def downgrade() -> None:
    op.drop_table("agent_input_requests")
