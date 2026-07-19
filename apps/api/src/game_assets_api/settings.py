from __future__ import annotations

import ipaddress
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GAME_ASSETS_", extra="ignore")

    data_dir: Path = Field(default_factory=lambda: Path(__file__).parents[2] / ".data")
    frontend_dist: Path | None = Field(
        default_factory=lambda: Path(__file__).resolve().parents[3] / "web" / "dist"
    )
    database_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    ]
    job_poll_interval: float = 0.15
    log_level: str = "info"

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        try:
            if not ipaddress.ip_address(value).is_loopback:
                raise ValueError("the API must bind to a loopback address")
        except ValueError as exc:
            if value != "localhost":
                raise ValueError("the API must bind to 127.0.0.1, ::1, or localhost") from exc
        return value

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'index.sqlite3').as_posix()}"

    def prepare(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
