from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .settings import Settings


def make_engine(settings: Settings) -> Engine:
    settings.prepare()
    connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
    engine = create_engine(settings.resolved_database_url, connect_args=connect_args, future=True)

    if settings.resolved_database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


class Database:
    def __init__(self, settings: Settings):
        self.engine = make_engine(settings)
        self.sessions = make_session_factory(self.engine)

    def create_schema(self) -> None:
        from .models import Base

        Base.metadata.create_all(self.engine)
        if self.engine.url.get_backend_name() == "sqlite":
            self._upgrade_sqlite_columns()

    def _upgrade_sqlite_columns(self) -> None:
        """Add M2 columns to local indexes created before migration 0002.

        The Project remains the fact source; this compatibility migration only
        evolves rebuildable task/runtime tables without requiring a destructive DB reset.
        """

        columns: dict[str, tuple[tuple[str, str], ...]] = {
            "provider_profiles": (
                ("is_active", "BOOLEAN NOT NULL DEFAULT 1"),
                ("models_json", "JSON NOT NULL DEFAULT '[]'"),
                ("models_refreshed_at", "DATETIME"),
            ),
            "generation_plans": (
                ("name", "VARCHAR(200) NOT NULL DEFAULT '未命名生成计划'"),
                ("suggested_extra_calls", "INTEGER NOT NULL DEFAULT 2"),
                ("extra_call_budget", "INTEGER NOT NULL DEFAULT 2"),
                ("extra_calls_used", "INTEGER NOT NULL DEFAULT 0"),
                ("actual_calls", "INTEGER NOT NULL DEFAULT 0"),
                ("actual_cost", "FLOAT NOT NULL DEFAULT 0"),
                ("max_paid_remediation_rounds", "INTEGER NOT NULL DEFAULT 2"),
                ("max_transport_retries", "INTEGER NOT NULL DEFAULT 2"),
                ("max_concurrency", "INTEGER NOT NULL DEFAULT 6"),
            ),
            "generation_jobs": (
                ("stage", "VARCHAR(40) NOT NULL DEFAULT 'queued'"),
                ("paid_remediation_rounds", "INTEGER NOT NULL DEFAULT 0"),
                ("lease_owner", "VARCHAR(120)"),
                ("lease_token", "VARCHAR(80)"),
                ("lease_expires_at", "DATETIME"),
                ("heartbeat_at", "DATETIME"),
                ("resolved_request_json", "JSON NOT NULL DEFAULT '{}'"),
                ("provider_snapshot_json", "JSON NOT NULL DEFAULT '{}'"),
                ("pending_action_id", "VARCHAR(48)"),
            ),
            "generation_attempts": (
                ("phase", "VARCHAR(40) NOT NULL DEFAULT 'succeeded'"),
                ("purpose", "VARCHAR(40) NOT NULL DEFAULT 'base'"),
                ("idempotency_key", "VARCHAR(160)"),
                ("request_hash", "VARCHAR(64)"),
                ("request_json", "JSON NOT NULL DEFAULT '{}'"),
                ("output_path", "TEXT"),
                ("output_hash", "VARCHAR(64)"),
                ("result_revision_id", "VARCHAR(48)"),
                ("billable", "BOOLEAN NOT NULL DEFAULT 1"),
                ("estimated_cost", "FLOAT"),
            ),
            "releases": (
                ("manifest_version", "INTEGER NOT NULL DEFAULT 1"),
                ("snapshot_hash", "VARCHAR(80)"),
            ),
            "deliveries": (),
        }
        with self.engine.begin() as connection:
            for table, additions in columns.items():
                existing = {
                    str(row[1])
                    for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").all()
                }
                for name, declaration in additions:
                    if name not in existing:
                        connection.exec_driver_sql(
                            f'ALTER TABLE "{table}" ADD COLUMN "{name}" {declaration}'
                        )
            for table, name, column in (
                ("provider_profiles", "ix_provider_profiles_is_active", "is_active"),
                ("generation_jobs", "ix_generation_jobs_stage", "stage"),
                ("generation_jobs", "ix_generation_jobs_lease_owner", "lease_owner"),
                (
                    "generation_jobs",
                    "ix_generation_jobs_lease_expires_at",
                    "lease_expires_at",
                ),
                (
                    "generation_jobs",
                    "ix_generation_jobs_pending_action_id",
                    "pending_action_id",
                ),
                ("generation_attempts", "ix_generation_attempts_phase", "phase"),
                (
                    "generation_attempts",
                    "ix_generation_attempts_idempotency_key",
                    "idempotency_key",
                ),
                ("deliveries", "ix_deliveries_project_id", "project_id"),
                ("deliveries", "ix_deliveries_release_id", "release_id"),
                ("deliveries", "ix_deliveries_status", "status"),
            ):
                if not connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table,),
                ).first():
                    continue
                connection.exec_driver_sql(
                    f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" ("{column}")'
                )
            defaults = connection.exec_driver_sql(
                "SELECT id FROM provider_routing_defaults WHERE id = 'global'"
            ).first()
            if defaults is None:
                first = connection.exec_driver_sql(
                    "SELECT id, text_model, image_model FROM provider_profiles "
                    "WHERE is_active = 1 ORDER BY created_at, id LIMIT 1"
                ).mappings().first()
                connection.exec_driver_sql(
                    "INSERT INTO provider_routing_defaults "
                    "(id, text_provider_profile_id, text_model, image_provider_profile_id, image_model, updated_at) "
                    "VALUES ('global', ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                    (
                        first["id"] if first else None,
                        first["text_model"] if first else None,
                        first["id"] if first else None,
                        first["image_model"] if first else None,
                    ),
                )

    def session(self) -> Generator[Session, None, None]:
        with self.sessions() as session:
            yield session
