"""Isolated Codex/App Server boundary.

The API intentionally does not import an experimental SDK at module import time.
Deployments can provide a pinned stdio JSON command through ``GAME_ASSETS_CODEX_COMMAND``;
otherwise the adapter is explicitly unavailable and the caller enters manual mode.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class CodexAdapterError(RuntimeError):
    """Base error raised by the isolated adapter."""


class CodexUnavailable(CodexAdapterError):
    pass


class CodexProtocolError(CodexAdapterError):
    pass


class CodexAdapter(Protocol):
    name: str
    version: str

    async def diagnose(
        self,
        context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]: ...


@dataclass(slots=True)
class UnavailableCodexAdapter:
    name: str = "unavailable"
    version: str = "none"

    async def diagnose(
        self,
        _context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        raise CodexUnavailable("Codex SDK/App Server is not configured")


@dataclass(slots=True)
class JsonCommandCodexAdapter:
    """Run a pinned local stdio adapter with a JSON-in/JSON-out contract.

    The subprocess receives only the redacted context and a small protocol
    envelope.  It runs from a disposable workspace and never receives API keys,
    the database URL, or a write token for the project.
    """

    command: tuple[str, ...]
    timeout_seconds: float = 45.0
    name: str = "codex-stdio"
    version: str = "json-stdio-v1"

    async def diagnose(
        self,
        context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not self.command:
            raise CodexUnavailable("Codex command is empty")
        cwd = work_dir if work_dir and work_dir.is_dir() else None
        # Do not forward the parent process' secrets to an Agent command.  The
        # command may use its own OS-level login managed by the Codex runtime.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
            and not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET"))
        }
        env["GAMS_AGENT_READ_ONLY"] = "1"
        payload = json.dumps(
            {"protocol_version": 1, "budget": budget, "context": context},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise CodexUnavailable(f"unable to start Codex adapter: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodexUnavailable("Codex adapter timed out") from exc
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:500]
            raise CodexUnavailable(
                f"Codex adapter exited with status {process.returncode}"
                + (f": {detail}" if detail else "")
            )
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexProtocolError("Codex adapter did not return JSON") from exc
        if not isinstance(value, dict):
            raise CodexProtocolError("Codex adapter response must be an object")
        return value


def build_codex_adapter(
    command: str | None,
    *,
    timeout_seconds: float = 45.0,
) -> CodexAdapter:
    if not command or not command.strip():
        return UnavailableCodexAdapter()
    try:
        argv = tuple(shlex.split(command))
    except ValueError as exc:
        return UnavailableCodexAdapter()
    if not argv:
        return UnavailableCodexAdapter()
    if Path(argv[0]).name.lower() in {"git", "git.exe"}:
        return UnavailableCodexAdapter()
    return JsonCommandCodexAdapter(argv, timeout_seconds=timeout_seconds)
