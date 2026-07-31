"""Release Manifest v2 and rebuildable Delivery index.

Revision ID: 0004_release_delivery
Revises: 0003_multi_provider_routing
"""

from alembic import op
import sqlalchemy as sa

from game_assets_api.models import Base


revision = "0004_release_delivery"
down_revision = "0003_multi_provider_routing"
branch_labels = None
depends_on = None


def _add_missing(table: str, columns: tuple[sa.Column, ...]) -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing(
        "releases",
        (
            sa.Column("manifest_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("snapshot_hash", sa.String(80), nullable=True),
        ),
    )
    # Delivery receipts live in Project/history and this table is only a
    # rebuildable query index.  Creating from the declarative model keeps the
    # foreign-key behavior identical on fresh and upgraded databases.
    Base.metadata.tables["deliveries"].create(bind=bind, checkfirst=True)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("deliveries")}
    for name, column in (
        ("ix_deliveries_project_id", "project_id"),
        ("ix_deliveries_release_id", "release_id"),
        ("ix_deliveries_status", "status"),
    ):
        if name not in indexes:
            op.create_index(name, "deliveries", [column])


def downgrade() -> None:
    op.drop_table("deliveries")
