"""M2 deterministic production runs.

Revision ID: 0002_m2_deterministic_runs
Revises: 0001_initial
"""

from alembic import op
import sqlalchemy as sa

from game_assets_api.models import Base


revision = "0002_m2_deterministic_runs"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


PLAN_COLUMNS = (
    sa.Column("name", sa.String(200), nullable=False, server_default="未命名生成计划"),
    sa.Column("suggested_extra_calls", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("extra_call_budget", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("extra_calls_used", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("actual_calls", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("actual_cost", sa.Float(), nullable=False, server_default="0"),
    sa.Column("max_paid_remediation_rounds", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("max_transport_retries", sa.Integer(), nullable=False, server_default="2"),
    sa.Column("max_concurrency", sa.Integer(), nullable=False, server_default="6"),
)
JOB_COLUMNS = (
    sa.Column("stage", sa.String(40), nullable=False, server_default="queued"),
    sa.Column("paid_remediation_rounds", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("lease_owner", sa.String(120), nullable=True),
    sa.Column("lease_token", sa.String(80), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("resolved_request_json", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("pending_action_id", sa.String(48), nullable=True),
)
ATTEMPT_COLUMNS = (
    sa.Column("phase", sa.String(40), nullable=False, server_default="succeeded"),
    sa.Column("purpose", sa.String(40), nullable=False, server_default="base"),
    sa.Column("idempotency_key", sa.String(160), nullable=True),
    sa.Column("request_hash", sa.String(64), nullable=True),
    sa.Column("request_json", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("output_path", sa.Text(), nullable=True),
    sa.Column("output_hash", sa.String(64), nullable=True),
    sa.Column("result_revision_id", sa.String(48), nullable=True),
    sa.Column("billable", sa.Boolean(), nullable=False, server_default=sa.true()),
    sa.Column("estimated_cost", sa.Float(), nullable=True),
)
M2_INDEXES = (
    ("ix_generation_jobs_stage", "generation_jobs", "stage"),
    ("ix_generation_jobs_lease_owner", "generation_jobs", "lease_owner"),
    ("ix_generation_jobs_lease_expires_at", "generation_jobs", "lease_expires_at"),
    ("ix_generation_jobs_pending_action_id", "generation_jobs", "pending_action_id"),
    ("ix_generation_attempts_phase", "generation_attempts", "phase"),
    (
        "ix_generation_attempts_idempotency_key",
        "generation_attempts",
        "idempotency_key",
    ),
)


def _add_missing(table: str, columns: tuple[sa.Column, ...]) -> None:
    bind = op.get_bind()
    existing = {column["name"] for column in sa.inspect(bind).get_columns(table)}
    for column in columns:
        if column.name not in existing:
            op.add_column(table, column)


def upgrade() -> None:
    bind = op.get_bind()
    _add_missing("generation_plans", PLAN_COLUMNS)
    _add_missing("generation_jobs", JOB_COLUMNS)
    _add_missing("generation_attempts", ATTEMPT_COLUMNS)
    for table_name in (
        "run_events",
        "production_findings",
        "run_evidence",
        "remediation_actions",
    ):
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)
    indexes_by_table = {
        table: {index["name"] for index in sa.inspect(bind).get_indexes(table)}
        for _, table, _ in M2_INDEXES
    }
    for name, table, column in M2_INDEXES:
        if name not in indexes_by_table[table]:
            op.create_index(name, table, [column])


def downgrade() -> None:
    for table_name in (
        "remediation_actions",
        "run_evidence",
        "production_findings",
        "run_events",
    ):
        op.drop_table(table_name)
