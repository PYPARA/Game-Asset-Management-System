"""Four provider routes and generation runtime defaults.

Revision ID: 0007_provider_defaults_and_runtime
Revises: 0006_provider_connection_discovery
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_provider_defaults_and_runtime"
down_revision = "0006_provider_connection_discovery"
branch_labels = None
depends_on = None


def _add_missing(table: str, columns: tuple[sa.Column, ...]) -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    _add_missing(
        "provider_routing_defaults",
        (
            sa.Column("video_provider_profile_id", sa.String(length=36), nullable=True),
            sa.Column("video_model", sa.String(length=160), nullable=True),
            sa.Column("audio_provider_profile_id", sa.String(length=36), nullable=True),
            sa.Column("audio_model", sa.String(length=160), nullable=True),
            sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("max_transport_retries", sa.Integer(), nullable=False, server_default="2"),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = {
        column["name"]
        for column in sa.inspect(bind).get_columns("provider_routing_defaults")
    }
    for name in (
        "max_transport_retries",
        "max_concurrency",
        "audio_model",
        "audio_provider_profile_id",
        "video_model",
        "video_provider_profile_id",
    ):
        if name in existing:
            op.drop_column("provider_routing_defaults", name)
