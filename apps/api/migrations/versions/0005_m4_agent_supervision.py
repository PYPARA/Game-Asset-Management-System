"""M4 Codex supervision sessions, events and review-only ChangeSets.

Revision ID: 0005_m4_agent_supervision
Revises: 0004_release_delivery
"""

from alembic import op
import sqlalchemy as sa

from game_assets_api.models import Base


revision = "0005_m4_agent_supervision"
down_revision = "0004_release_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in ("agent_sessions", "agent_events", "change_sets"):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)
    existing = {column["name"] for column in sa.inspect(bind).get_columns("agent_events")}
    if "asset_id" not in existing:
        op.add_column(
            "agent_events",
            sa.Column("asset_id", sa.String(36), nullable=True),
        )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("agent_events")}
    if "ix_agent_events_asset_id" not in indexes:
        op.create_index("ix_agent_events_asset_id", "agent_events", ["asset_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_events_asset_id", table_name="agent_events")
    op.drop_table("change_sets")
    op.drop_table("agent_events")
    op.drop_table("agent_sessions")
