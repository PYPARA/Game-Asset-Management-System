from __future__ import annotations

import ipaddress
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_PROJECTS_ROOT = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "Game-Projects"
)
DEFAULT_STATE_DIR = (
    Path.home() / "Library" / "Application Support" / "Game-Asset-Management-System"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GAME_ASSETS_", extra="ignore")

    projects_root: Path = Field(default_factory=lambda: DEFAULT_PROJECTS_ROOT)
    state_dir: Path = Field(default_factory=lambda: DEFAULT_STATE_DIR)
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
        # Vite preview commonly selects the next free port when 4173 is busy.
        # Keep the local browser workbench usable in that case as well.
        "http://127.0.0.1:4174",
        "http://localhost:4174",
        "http://127.0.0.1:8787",
        "http://localhost:8787",
    ]
    job_poll_interval: float = 0.15
    job_lease_seconds: float = 30.0
    job_heartbeat_interval: float = 5.0
    log_level: str = "info"
    # Legacy JSON adapter override. When empty, the pinned official Codex SDK
    # starts and manages its bundled App Server runtime. ``codex_bin`` is only
    # an explicit runtime override; normal installs reuse the local Codex login.
    codex_command: str | None = None
    codex_bin: str | None = None
    codex_timeout_seconds: float = Field(default=45.0, ge=1.0, le=600.0)
    agent_budget: int = Field(default=1, ge=1, le=20)
    planning_external_worker: bool = False  # CLI enables the separate worker; embedded mode is for tests/integrations.
    planning_agent_budget: int = Field(default=8, ge=1, le=20)

    @field_validator("job_lease_seconds")
    @classmethod
    def valid_lease(cls, value: float) -> float:
        if value < 1.0:
            raise ValueError("job lease must be at least one second")
        return value

    @field_validator("job_heartbeat_interval")
    @classmethod
    def valid_heartbeat(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("job heartbeat interval must be positive")
        return value

    @model_validator(mode="after")
    def heartbeat_precedes_lease_expiry(self) -> "Settings":
        if self.job_heartbeat_interval >= self.job_lease_seconds:
            raise ValueError("job heartbeat interval must be shorter than the job lease")
        return self

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
        return f"sqlite:///{(self.state_dir / 'index.sqlite3').as_posix()}"

    def prepare(self) -> None:
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
