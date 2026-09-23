"""Distinguish local preparation from provider dispatch."""
from alembic import op
import sqlalchemy as sa
revision = "0011_provider_dispatch"
down_revision = "0010_durable_planning"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("generation_attempts")}
    for column in (
        sa.Column("dispatch_state", sa.String(32), nullable=False, server_default="legacy_unknown"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True)),
        sa.Column("dispatch_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_hint", sa.Text()),
        sa.Column("network_policy", sa.String(40)),
    ):
        if column.name not in existing:
            op.add_column("generation_attempts", column)


def downgrade():
    for name in ("network_policy", "error_hint", "error_code", "dispatch_count", "dispatched_at", "dispatch_state"):
        op.drop_column("generation_attempts", name)
