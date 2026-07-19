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

    def session(self) -> Generator[Session, None, None]:
        with self.sessions() as session:
            yield session

