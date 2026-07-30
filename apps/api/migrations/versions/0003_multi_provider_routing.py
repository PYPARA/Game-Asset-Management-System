"""M2.1 multi-provider model routing.

Revision ID: 0003_multi_provider_routing
Revises: 0002_m2_deterministic_runs
"""

from alembic import op
import sqlalchemy as sa

from game_assets_api.models import Base


revision = "0003_multi_provider_routing"
down_revision = "0002_m2_deterministic_runs"
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
        "provider_profiles",
        (
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("models_json", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("models_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        ),
    )
    _add_missing(
        "generation_jobs",
        (sa.Column("provider_snapshot_json", sa.JSON(), nullable=False, server_default="{}"),),
    )
    Base.metadata.tables["provider_routing_defaults"].create(bind=bind, checkfirst=True)
    indexes = {index["name"] for index in sa.inspect(bind).get_indexes("provider_profiles")}
    if "ix_provider_profiles_is_active" not in indexes:
        op.create_index("ix_provider_profiles_is_active", "provider_profiles", ["is_active"])

    first = bind.execute(
        sa.text(
            "SELECT id, text_model, image_model FROM provider_profiles "
            "WHERE is_active = 1 ORDER BY created_at, id LIMIT 1"
        )
    ).mappings().first()
    existing_defaults = bind.execute(
        sa.text("SELECT id FROM provider_routing_defaults WHERE id = 'global'")
    ).first()
    if existing_defaults is None:
        bind.execute(
            sa.text(
                "INSERT INTO provider_routing_defaults "
                "(id, text_provider_profile_id, text_model, image_provider_profile_id, image_model, updated_at) "
                "VALUES ('global', :provider_id, :text_model, :provider_id, :image_model, CURRENT_TIMESTAMP)"
            ),
            {
                "provider_id": first["id"] if first else None,
                "text_model": first["text_model"] if first else None,
                "image_model": first["image_model"] if first else None,
            },
        )


def downgrade() -> None:
    op.drop_table("provider_routing_defaults")
