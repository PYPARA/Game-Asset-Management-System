"""M6 release acceptance helpers.

The production code already contains the individual safety guarantees used by
M0--M5.  This module deliberately keeps the release rehearsal in one place so
that it can be repeated before shipping without contacting a provider by
default.  The protocol fixture below speaks the same HTTP endpoints as an
OpenAI-compatible provider; it is not a fake ``GenerationProvider`` and is
therefore useful for exercising request/response classification and retry
boundaries.

``run_m6_acceptance`` returns a JSON-serialisable report.  A caller may pass a
real provider URL and key to run the optional live contract check.  Credentials
are never included in the report.
"""

from __future__ import annotations

import asyncio
import base64
import errno
import io
import json
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from .database import Database
from . import delivery as delivery_module
from .delivery import DeliveryError, apply_export
from .domain import ErrorCategory, ProviderKind
from .main import create_app
from .models import (
    Delivery,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    ProviderProfile,
    new_id,
    utcnow,
)
from .providers import OpenAICompatibleProvider, ProviderError, ProviderRuntimeConfig
from .providers import CredentialVault
from .runner import JobRunner
from .services import discover_projects
from .settings import Settings
from .storage import ProjectStore, atomic_write_yaml


M6_REPORT_FORMAT = 1
M6_CHECKS = (
    "provider.success",
    "provider.rate_limit",
    "provider.server_error",
    "provider.auth",
    "provider.quota",
    "provider.content_policy",
    "provider.bad_response",
    "provider.concurrent_requests",
    "recovery.disk_full",
    "recovery.delivery_interruption",
    "recovery.service_crash",
    "recovery.worker_crash",
    "recovery.sqlite_rebuild",
    "recovery.project_scan",
    "format.project_v1",
    "format.release_v1_v2",
    "provider.live_contract",
)


@dataclass(slots=True)
class AcceptanceCheck:
    """One immutable-ish check result used in the generated receipt."""

    check_id: str
    status: Literal["passed", "failed", "skipped"]
    started_at: str
    ended_at: str
    duration_ms: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms, 3),
            "details": self.details,
            **({"error": self.error} if self.error else {}),
        }


@dataclass(slots=True)
class _FixtureState:
    scenario: str
    delay_seconds: float = 0.0
    requests: int = 0
    active: int = 0
    max_active: int = 0
    paths: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def enter(self, path: str) -> None:
        with self.lock:
            self.requests += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.paths.append(path)

    def leave(self) -> None:
        with self.lock:
            self.active = max(0, self.active - 1)


class _ProviderFixtureHandler(BaseHTTPRequestHandler):
    """Small HTTP server for the OpenAI-compatible request contract."""

    server: "_ProviderFixtureServer"

    def log_message(self, _format: str, *_args: Any) -> None:
        # Acceptance output is the report, not an HTTP access log.
        return

    @property
    def state(self) -> _FixtureState:
        return self.server.state

    def _write(self, status: int, payload: Any, *, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _failure(self) -> bool:
        scenario = self.state.scenario
        if scenario == "success" or scenario == "bad_response":
            return False
        mapping: dict[str, tuple[int, dict[str, Any], dict[str, str]]] = {
            "rate_limit": (
                429,
                {"error": {"type": "rate_limit", "message": "too many requests"}},
                {"Retry-After": "0.01"},
            ),
            "server": (
                503,
                {"error": {"type": "server_error", "message": "temporarily unavailable"}},
                {},
            ),
            "auth": (
                401,
                {"error": {"type": "invalid_api_key", "message": "invalid key"}},
                {},
            ),
            "quota": (
                429,
                {"error": {"code": "insufficient_quota", "message": "quota exceeded"}},
                {},
            ),
            "content_policy": (
                400,
                {"error": {"code": "moderation_blocked", "message": "safety system"}},
                {},
            ),
        }
        status, payload, headers = mapping[scenario]
        self._write(status, payload, headers=headers)
        return True

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.state.enter(self.path)
        try:
            if self.state.delay_seconds:
                time.sleep(self.state.delay_seconds)
            if self._failure():
                return
            if not self.path.rstrip("/").endswith("/models"):
                self._write(404, {"error": {"message": "not found"}})
                return
            self._write(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "fixture-text", "type": "model", "modalities": ["text"]},
                        {"id": "fixture-image", "type": "model", "modalities": ["image"]},
                    ],
                },
            )
        finally:
            self.state.leave()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self.state.enter(self.path)
        try:
            if self.state.delay_seconds:
                time.sleep(self.state.delay_seconds)
            if self._failure():
                return
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            if self.path.rstrip("/").endswith("/chat/completions"):
                if self.state.scenario == "bad_response":
                    self._write(200, {"choices": []})
                    return
                self._write(
                    200,
                    {
                        "id": "fixture-chat-request",
                        "choices": [{"message": {"role": "assistant", "content": '{"value":"ok"}'}}],
                    },
                )
                return
            if self.path.rstrip("/").endswith(("/images/generations", "/images/edits")):
                if self.state.scenario == "bad_response":
                    self._write(200, {"data": [{}]})
                    return
                self._write(
                    200,
                    {"created": int(time.time()), "data": [{"b64_json": _PNG_B64}]},
                )
                return
            self._write(404, {"error": {"message": "not found"}})
        finally:
            self.state.leave()


class _ProviderFixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: _FixtureState):
        super().__init__(("127.0.0.1", 0), _ProviderFixtureHandler)
        self.state = state


@contextmanager
def provider_fixture(
    scenario: str = "success", *, delay_seconds: float = 0.0
) -> Iterator[tuple[str, _FixtureState]]:
    """Yield a loopback OpenAI-compatible server and its request counters."""

    state = _FixtureState(scenario=scenario, delay_seconds=delay_seconds)
    server = _ProviderFixtureServer(state)
    thread = threading.Thread(target=server.serve_forever, name="gams-m6-provider", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _png_b64() -> str:
    image = Image.new("RGBA", (8, 8), (72, 110, 220, 255))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


_PNG_B64 = _png_b64()


def _provider(base_url: str, *, api_key: str = "m6-fixture-key") -> OpenAICompatibleProvider:
    profile = ProviderRuntimeConfig(
        id="m6-acceptance-provider",
        kind=ProviderKind.OPENAI_COMPATIBLE.value,
        base_url=base_url,
        text_model="fixture-text",
        image_model="fixture-image",
        quality="standard",
        allow_private_network=False,
    )
    return OpenAICompatibleProvider(profile, api_key)


def _check(
    check_id: str,
    operation: Callable[[], dict[str, Any] | None],
    *,
    skipped: str | None = None,
) -> AcceptanceCheck:
    started = datetime.now(UTC)
    monotonic = time.perf_counter()
    if skipped:
        ended = datetime.now(UTC)
        return AcceptanceCheck(
            check_id,
            "skipped",
            started.isoformat(),
            ended.isoformat(),
            (time.perf_counter() - monotonic) * 1000,
            {"reason": skipped},
        )
    try:
        details = operation() or {}
        status: Literal["passed", "failed"] = "passed"
        error = None
    except Exception as exc:  # noqa: BLE001 - report every failed drill
        details = {}
        status = "failed"
        error = f"{type(exc).__name__}: {exc}"
    ended = datetime.now(UTC)
    return AcceptanceCheck(
        check_id,
        status,
        started.isoformat(),
        ended.isoformat(),
        (time.perf_counter() - monotonic) * 1000,
        details,
        error,
    )


async def _provider_case(scenario: str, base_url: str) -> dict[str, Any]:
    provider = _provider(base_url)
    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    if scenario == "success":
        models = await provider.discover_models()
        text = await provider.structured_text(prompt="M6 protocol check", schema=schema, model="fixture-text")
        image = await provider.image(prompt="M6 image check", width=8, height=8, model="fixture-image")
        edited = await provider.image(
            prompt="M6 edit check",
            width=8,
            height=8,
            model="fixture-image",
            reference=base64.b64decode(_PNG_B64),
        )
        if not models or text.value.get("value") != "ok" or not image.value or not edited.value:
            raise AssertionError("successful provider response did not satisfy the contract")
        return {
            "models": [str(item["id"]) for item in models],
            "text_bytes": len(json.dumps(text.value).encode("utf-8")),
            "image_bytes": len(image.value),
            "edit_bytes": len(edited.value),
        }
    expected = {
        "rate_limit": ErrorCategory.RATE_LIMIT,
        "server": ErrorCategory.SERVER,
        "auth": ErrorCategory.AUTH,
        "quota": ErrorCategory.QUOTA,
        "content_policy": ErrorCategory.CONTENT_POLICY,
    }.get(scenario, ErrorCategory.EMPTY)
    try:
        if scenario == "bad_response":
            await provider.structured_text(prompt="bad", schema=schema, model="fixture-text")
        else:
            await provider.structured_text(prompt=scenario, schema=schema, model="fixture-text")
    except ProviderError as exc:
        if exc.category != expected:
            raise AssertionError(f"expected {expected.value}, got {exc.category.value}") from exc
        return {
            "category": exc.category.value,
            "status_code": exc.status_code,
            "retryable": exc.retryable,
            "request_id_present": bool(exc.request_id),
        }
    raise AssertionError("provider call unexpectedly succeeded")


def run_provider_fault_matrix() -> list[AcceptanceCheck]:
    """Exercise the provider's real HTTP adapter against each fault class."""

    checks: list[AcceptanceCheck] = []
    for scenario in ("success", "rate_limit", "server", "auth", "quota", "content_policy", "bad_response"):
        check_id = {
            "server": "provider.server_error",
            "bad_response": "provider.bad_response",
        }.get(scenario, f"provider.{scenario}")

        def operation(scenario: str = scenario) -> dict[str, Any]:
            with provider_fixture(scenario) as (base_url, state):
                details = asyncio.run(_provider_case(scenario, base_url))
                details["requests"] = state.requests
                details["paths"] = sorted(set(state.paths))
                return details

        checks.append(_check(check_id, operation))
    return checks


def run_provider_concurrency(*, request_count: int = 8) -> AcceptanceCheck:
    """Verify concurrent calls use independent HTTP requests and complete."""

    def operation() -> dict[str, Any]:
        with provider_fixture("success", delay_seconds=0.02) as (base_url, state):
            provider = _provider(base_url)

            async def run() -> list[Any]:
                return await asyncio.gather(
                    *[
                        provider.image(
                            prompt=f"concurrency-{index}",
                            width=8,
                            height=8,
                            model="fixture-image",
                            idempotency_key=f"m6-concurrency-{index}",
                        )
                        for index in range(request_count)
                    ]
                )

            results = asyncio.run(run())
            if len(results) != request_count or any(not result.value for result in results):
                raise AssertionError("not every concurrent provider request completed")
            if state.max_active < 2:
                raise AssertionError("fixture observed no overlapping provider requests")
            return {
                "request_count": request_count,
                "completed": len(results),
                "max_in_flight": state.max_active,
                "paths": sorted(set(state.paths)),
            }

    return _check("provider.concurrent_requests", operation)


async def run_live_provider_contract(base_url: str, api_key: str) -> dict[str, Any]:
    """Run a minimal, non-secret contract check against a configured provider."""

    provider = _provider(base_url, api_key=api_key)
    models = await provider.discover_models()
    text_model = next(
        (
            str(item["id"])
            for item in models
            if any(token in {"text", "chat"} for token in _modalities(item))
        ),
        "",
    )
    if not text_model:
        # Many OpenAI-compatible vendors omit modality metadata.  Their
        # ``/models`` response is still useful; use the first advertised model
        # for the text contract and let the provider return a structured error
        # if that model is genuinely incompatible.
        text_model = next((str(item["id"]) for item in models if item.get("id")), "")
    if not text_model:
        raise ProviderError("live provider has no text/chat model", ErrorCategory.VALIDATION)
    schema = {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
    result = await provider.structured_text(
        prompt="Return JSON with a short value for an M6 connectivity check.",
        schema=schema,
        model=text_model,
    )
    return {
        "models_seen": len(models),
        "text_model": text_model,
        "response_valid": isinstance(result.value, dict) and isinstance(result.value.get("value"), str),
    }


def _modalities(model: dict[str, Any]) -> set[str]:
    values = model.get("modalities") or model.get("input_modalities") or model.get("capabilities") or []
    if isinstance(values, str):
        return {values.lower()}
    if isinstance(values, list):
        return {str(value).lower() for value in values}
    return set()


def _create_fixture_project(client: TestClient) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    response = client.post(
        "/api/projects",
        json={"directory_name": "m6-fixture", "name": "M6 Fixture", "default_language": "zh-CN"},
    )
    response.raise_for_status()
    project = response.json()
    response = client.post(
        "/api/assets",
        json={
            "project_id": project["id"],
            "key": "content.m6.recovery",
            "kind": "content",
            "subtype": "scene",
            "title": "M6 Recovery",
        },
    )
    response.raise_for_status()
    asset = response.json()
    response = client.post(
        "/api/revisions",
        json={"asset_id": asset["id"], "format": "json", "content": {"title": "first"}},
    )
    response.raise_for_status()
    revision = response.json()
    response = client.post("/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"})
    response.raise_for_status()
    response = client.post(
        "/api/releases",
        json={"project_id": project["id"], "name": "m6-first", "format_version": 2},
    )
    response.raise_for_status()
    refreshed_asset = client.get(f"/api/assets/{asset['id']}")
    refreshed_asset.raise_for_status()
    return project, refreshed_asset.json(), response.json()


def _write_failing_validation(project_root: Path) -> None:
    store = ProjectStore(project_root)
    contract = store.read_yaml(project_root / "project.yaml")
    export = dict(contract.get("export") or {})
    export["validation_commands"] = [
        {"label": "M6 injected interruption", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}
    ]
    contract["export"] = export
    atomic_write_yaml(project_root / "project.yaml", contract)


def run_recovery_drills() -> list[AcceptanceCheck]:
    """Exercise Project/SQLite/Delivery recovery in a disposable fixture."""

    checks: list[AcceptanceCheck] = []
    with tempfile.TemporaryDirectory(prefix="gams-m6-") as temporary:
        root = Path(temporary)
        settings = Settings(
            projects_root=root / "projects",
            state_dir=root / "state",
            database_url=f"sqlite:///{(root / 'state' / 'index.sqlite3').as_posix()}",
            frontend_dist=None,
            job_poll_interval=0.01,
        )
        game_root = root / "checkout"
        game_root.mkdir()
        db_path = root / "state" / "index.sqlite3"

        with TestClient(create_app(settings)) as client:
            project, asset, first_release = _create_fixture_project(client)
            project_root = Path(project["root_path"])
            response = client.put(
                f"/api/projects/{project['id']}/export-config", json={"game_root": str(game_root)}
            )
            response.raise_for_status()
            response = client.post(
                "/api/exports/apply", json={"project_id": project["id"], "release_id": first_release["id"]}
            )
            response.raise_for_status()
            old_lock = (game_root / "gams-lock.json").read_bytes()
            old_registry = (game_root / "src/generated/content/registry.ts").read_bytes()

            # Create a second Release so a failed Delivery has a real target to restore.
            revision = client.post(
                "/api/revisions",
                json={"asset_id": asset["id"], "format": "json", "content": {"title": "second"}},
            )
            revision.raise_for_status()
            response = client.post("/api/reviews", json={"revision_id": revision.json()["id"], "verdict": "approve"})
            response.raise_for_status()
            response = client.post(
                "/api/releases",
                json={"project_id": project["id"], "name": "m6-second", "format_version": 2},
            )
            response.raise_for_status()
            second_release = response.json()

            def disk_full() -> dict[str, Any]:
                original = delivery_module._atomic_checkout_write
                injected = {"value": False}

                def fail_once(checkout: Path, relative: str, content: bytes) -> None:
                    if not injected["value"] and relative != "gams-lock.json":
                        injected["value"] = True
                        raise OSError(errno.ENOSPC, "M6 injected disk full")
                    original(checkout, relative, content)

                with patch.object(delivery_module, "_atomic_checkout_write", side_effect=fail_once):
                    try:
                        with Database(settings).sessions() as session:
                            apply_export(
                                session,
                                project_id=project["id"],
                                release_id=second_release["id"],
                                game_root=str(game_root),
                                run_commands=False,
                            )
                    except DeliveryError as exc:
                        if "restored" not in str(exc):
                            raise AssertionError(f"disk-full delivery did not restore: {exc}") from exc
                    else:
                        raise AssertionError("disk-full delivery unexpectedly succeeded")
                if (game_root / "gams-lock.json").read_bytes() != old_lock:
                    raise AssertionError("disk-full drill changed the previous lock")
                if (game_root / "src/generated/content/registry.ts").read_bytes() != old_registry:
                    raise AssertionError("disk-full drill changed the previous managed file")
                return {"error": "ENOSPC", "previous_lock_preserved": True}

            checks.append(_check("recovery.disk_full", disk_full))

            def delivery_interruption() -> dict[str, Any]:
                _write_failing_validation(project_root)
                try:
                    response = client.post(
                        "/api/exports/apply",
                        json={"project_id": project["id"], "release_id": second_release["id"]},
                    )
                    if response.status_code != 409:
                        raise AssertionError(f"expected validation failure, got {response.status_code}")
                finally:
                    # The failed command is part of the evidence; restore a valid
                    # contract before the SQLite rebuild check.
                    contract = ProjectStore(project_root).read_yaml(project_root / "project.yaml")
                    contract["export"]["validation_commands"] = []
                    atomic_write_yaml(project_root / "project.yaml", contract)
                if (game_root / "gams-lock.json").read_bytes() != old_lock:
                    raise AssertionError("interrupted delivery changed the previous lock")
                return {
                    "previous_lock_preserved": True,
                    "delivery_status": client.get(
                        "/api/deliveries", params={"project_id": project["id"]}
                    ).json()[0]["status"],
                }

            checks.append(_check("recovery.delivery_interruption", delivery_interruption))

            def project_scan() -> dict[str, Any]:
                scan = client.post(f"/api/projects/{project['id']}/scan")
                scan.raise_for_status()
                payload = scan.json()
                if payload["errors"]:
                    raise AssertionError("; ".join(payload["errors"]))
                return {
                    "assets_indexed": payload["assets_indexed"],
                    "revisions_indexed": payload["revisions_indexed"],
                }

            checks.append(_check("recovery.project_scan", project_scan))

        def persisted_run_recovery(status: str, task_id: str, check_id: str) -> AcceptanceCheck:
            """Re-run the Runner recovery sweep as a service/worker restart."""

            def operation() -> dict[str, Any]:
                database = Database(settings)
                database.create_schema()
                with database.sessions() as session:
                    provider = ProviderProfile(
                        id=new_id(),
                        name=f"M6 {check_id}",
                        kind="fake",
                        base_url="https://fake.invalid/v1",
                        text_model="fake-text",
                        image_model="fake-image",
                        quality="standard",
                        concurrency=1,
                        max_retries=0,
                        allow_private_network=False,
                        pricing={"text": 0.0, "image": 0.0},
                        is_active=True,
                        models_json=[],
                    )
                    session.add(provider)
                    session.flush()
                    plan = GenerationPlan(
                        id=new_id(),
                        project_id=project["id"],
                        provider_profile_id=provider.id,
                        name=f"M6 {check_id}",
                        status="running",
                        tasks_json=[],
                        estimated_calls=1,
                        estimated_cost=0.0,
                        actual_calls=1,
                        actual_cost=0.0,
                        max_transport_retries=0,
                        confirmed_at=utcnow(),
                    )
                    session.add(plan)
                    session.flush()
                    job = GenerationJob(
                        id=new_id(),
                        plan_id=plan.id,
                        project_id=project["id"],
                        provider_profile_id=provider.id,
                        task_id=task_id,
                        task_kind="text",
                        request_json={"asset_id": asset["id"], "kind": "text", "prompt": task_id},
                        status=status,
                        stage="output_received",
                        progress=0.7,
                        result_revision_id=asset.get("current_revision_id"),
                        attempt_count=1,
                        resolved_request_json={"asset_id": asset["id"]},
                        provider_snapshot_json={"name": provider.name, "model": "fake-text"},
                    )
                    session.add(job)
                    session.flush()
                    session.add(
                        GenerationAttempt(
                            id=new_id(),
                            job_id=job.id,
                            number=1,
                            status="succeeded",
                            phase="succeeded",
                            purpose="base",
                            request_json=job.request_json,
                            result_revision_id=asset.get("current_revision_id"),
                            billable=True,
                            estimated_cost=0.0,
                            started_at=utcnow(),
                            completed_at=utcnow(),
                        )
                    )
                    session.commit()
                    job_id = job.id

                runner = JobRunner(database.sessions, CredentialVault(), settings)
                runner.recover_interrupted_jobs()
                with database.sessions() as session:
                    recovered = session.get(GenerationJob, job_id)
                    if recovered is None or recovered.status != "candidate_ready":
                        raise AssertionError(
                            f"{check_id} did not recover to candidate_ready: "
                            f"{recovered.status if recovered else 'missing'}"
                        )
                    if recovered.attempt_count != 1:
                        raise AssertionError("recovery created a duplicate provider attempt")
                    return {
                        "job_id": job_id,
                        "status": recovered.status,
                        "attempt_count": recovered.attempt_count,
                        "provider_calls": 1,
                    }

            return _check(check_id, operation)

        checks.append(
            persisted_run_recovery(
                "output_received", "m6-service-crash", "recovery.service_crash"
            )
        )
        checks.append(
            persisted_run_recovery(
                "remediating", "m6-worker-crash", "recovery.worker_crash"
            )
        )

        # Recreate the index from Project history after the API process closes.
        def sqlite_rebuild() -> dict[str, Any]:
            if not db_path.exists():
                raise AssertionError("SQLite fixture was not created")
            # The explicit unlink is the destructive part of this drill; the
            # database is disposable and all Project files remain untouched.
            db_path.unlink()
            rebuilt = Database(settings)
            rebuilt.create_schema()
            with rebuilt.sessions() as session:
                projects, errors = discover_projects(session, settings.projects_root, scan=True)
                if errors:
                    raise AssertionError("scan errors after SQLite rebuild: " + "; ".join(errors))
                rebuilt_project = next(item for item in projects if item.id == project["id"])
                deliveries = list(session.scalars(select(Delivery).where(Delivery.project_id == rebuilt_project.id)).all())
                if len(deliveries) < 2:
                    raise AssertionError(f"expected delivery history after rebuild, got {len(deliveries)}")
                return {"deliveries_rebuilt": len(deliveries), "project_id": rebuilt_project.id}

        checks.append(_check("recovery.sqlite_rebuild", sqlite_rebuild))
    return checks


def run_format_checks() -> list[AcceptanceCheck]:
    """Keep the supported Project and Release versions explicit in the receipt."""

    def project_v1() -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="gams-m6-format-") as temporary:
            root = Path(temporary)
            store = ProjectStore(root)
            store.initialize(project_id="m6-format", name="M6 format")
            contract = store.read_yaml(root / "project.yaml")
            if contract.get("format_version") != 1 or store.export_contract().get("format_version") != 1:
                raise AssertionError("Project v1 contract was not accepted")
            return {"project_format": contract["format_version"], "export_format": store.export_contract()["format_version"]}

    def release_versions() -> dict[str, Any]:
        # Manifest v1 remains readable by the storage scanner; v2 additionally
        # requires a snapshot hash.  This is intentionally a pure contract check
        # and does not create a mutable release pointer.
        from .storage import canonical_json, sha256_bytes

        assets = [{"key": "content.m6", "revision_id": "rev", "content_hash": "sha256:x"}]
        payload = {"format_version": 2, "project_id": "m6-format", "assets": assets}
        snapshot = f"sha256:{sha256_bytes(canonical_json(payload))}"
        if not snapshot.startswith("sha256:"):
            raise AssertionError("v2 snapshot hash was not generated")
        return {"readable_manifest_versions": [1, 2], "v2_snapshot_prefix": snapshot[:15]}

    return [_check("format.project_v1", project_v1), _check("format.release_v1_v2", release_versions)]


def run_m6_acceptance(
    *,
    live_provider_url: str | None = None,
    live_provider_api_key: str | None = None,
    require_live_provider: bool = False,
) -> dict[str, Any]:
    """Run the complete offline release rehearsal and return its receipt."""

    checks = run_provider_fault_matrix()
    checks.append(run_provider_concurrency())
    checks.extend(run_recovery_drills())
    checks.extend(run_format_checks())
    if live_provider_url and live_provider_api_key:
        checks.append(
            _check(
                "provider.live_contract",
                lambda: asyncio.run(run_live_provider_contract(live_provider_url, live_provider_api_key)),
            )
        )
    else:
        checks.append(
            _check(
                "provider.live_contract",
                lambda: None,
                skipped="set live_provider_url and live_provider_api_key to run against a real provider",
            )
        )

    serialized = [check.as_dict() for check in checks]
    passed = sum(item["status"] == "passed" for item in serialized)
    failed = sum(item["status"] == "failed" for item in serialized)
    skipped = sum(item["status"] == "skipped" for item in serialized)
    if require_live_provider and not (live_provider_url and live_provider_api_key):
        for item in serialized:
            if item["id"] == "provider.live_contract" and item["status"] == "skipped":
                item["status"] = "failed"
                item["error"] = "a live provider URL and API key are required"
                failed += 1
                skipped -= 1
                break
    return {
        "format_version": M6_REPORT_FORMAT,
        "milestone": "M6",
        "generated_at": datetime.now(UTC).isoformat(),
        "runner": "gams-m6-acceptance/1",
        "provider": {
            "offline_fixture": True,
            "live_provider_configured": bool(live_provider_url and live_provider_api_key),
            "api_key_recorded": False,
        },
        "checks": serialized,
        "summary": {
            "total": len(serialized),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "ok": failed == 0,
        },
    }


def write_report(report: dict[str, Any], output: str | Path) -> Path:
    """Atomically write a receipt without ever serialising credentials."""

    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    from .storage import atomic_write_json

    atomic_write_json(target, report)
    return target


__all__ = [
    "M6_CHECKS",
    "run_m6_acceptance",
    "run_provider_concurrency",
    "run_provider_fault_matrix",
    "run_recovery_drills",
    "run_live_provider_contract",
    "write_report",
]
