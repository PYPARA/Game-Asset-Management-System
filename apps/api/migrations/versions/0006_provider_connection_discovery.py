"""Provider connection modes and model discovery diagnostics.

Revision ID: 0006_provider_connection_discovery
Revises: 0005_m4_agent_supervision
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_provider_connection_discovery"
down_revision = "0005_m4_agent_supervision"
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
        "provider_profiles",
        (
            sa.Column("credential_mode", sa.String(length=20), nullable=False, server_default="required"),
            sa.Column("model_discovery_mode", sa.String(length=20), nullable=False, server_default="auto"),
            sa.Column("models_path", sa.Text(), nullable=True, server_default="models"),
            sa.Column("models_sync_state", sa.String(length=24), nullable=False, server_default="never"),
            sa.Column("models_sync_checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("models_sync_diagnostic", sa.JSON(), nullable=False, server_default="{}"),
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns("provider_profiles")}
    for name in (
        "models_sync_diagnostic",
        "models_sync_checked_at",
        "models_sync_state",
        "models_path",
        "model_discovery_mode",
        "credential_mode",
    ):
        if name in existing:
            op.drop_column("provider_profiles", name)
