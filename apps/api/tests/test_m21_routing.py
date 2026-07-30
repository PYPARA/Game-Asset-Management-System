from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from game_assets_api import api as api_module
from game_assets_api.providers import (
    ErrorCategory,
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderError,
    ProviderResult,
    ProviderRuntimeConfig,
)

from .conftest import create_asset, create_project


def create_provider(
    client: TestClient,
    name: str,
    *,
    kind: str = "fake",
    text_model: str,
    image_model: str,
    concurrency: int = 2,
    pricing: dict[str, float] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/providers",
        json={
            "name": name,
            "kind": kind,
            "base_url": (
                f"https://{name.lower().replace(' ', '-')}.invalid/v1"
                if kind == "fake"
                else "https://provider.example/v1"
            ),
            "text_model": text_model,
            "image_model": image_model,
            "concurrency": concurrency,
            "pricing": pricing,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def text_task(
    task_id: str,
    asset_id: str,
    *,
    provider_id: str | None = None,
    model: str | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "kind": "text",
        "asset_id": asset_id,
        "prompt": task_id,
        "depends_on": depends_on or [],
        "schema": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    }
    if provider_id is not None:
        task["provider_profile_id"] = provider_id
    if model is not None:
        task["model"] = model
    return task


def image_task(
    task_id: str,
    asset_id: str,
    *,
    provider_id: str | None = None,
    model: str | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "kind": "image",
        "asset_id": asset_id,
        "prompt": task_id,
        "depends_on": depends_on or [],
        "width": 64,
        "height": 64,
    }
    if provider_id is not None:
        task["provider_profile_id"] = provider_id
    if model is not None:
        task["model"] = model
    return task


def wait_for_jobs(client: TestClient, job_ids: list[str], timeout: float = 8.0) -> list[dict[str, Any]]:
    terminal = {"candidate_ready", "awaiting_user", "failed", "cancelled"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = [client.get(f"/api/jobs/{job_id}").json() for job_id in job_ids]
        if all(job["status"] in terminal for job in jobs):
            return jobs
        time.sleep(0.02)
    raise AssertionError("jobs did not reach terminal state")


def test_provider_crud_defaults_and_model_cache_survive_refresh_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alpha = create_provider(
        client,
        "Alpha",
        text_model="alpha-text",
        image_model="alpha-image",
    )
    beta = create_provider(
        client,
        "Beta",
        text_model="beta-text",
        image_model="beta-image",
    )

    providers = client.get("/api/providers").json()
    assert [provider["name"] for provider in providers] == ["Alpha", "Beta"]
    updated = client.patch(
        f"/api/providers/{beta['id']}",
        json={"name": "Beta updated", "concurrency": 5, "max_retries": 4},
    )
    assert updated.status_code == 200, updated.text
    assert (updated.json()["name"], updated.json()["concurrency"]) == ("Beta updated", 5)

    defaults = client.put(
        "/api/provider-defaults",
        json={
            "text": {"provider_profile_id": alpha["id"], "model": "alpha-text"},
            "image": {"provider_profile_id": beta["id"], "model": "beta-image"},
        },
    )
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["text"]["provider_profile_id"] == alpha["id"]
    incompatible = client.put(
        "/api/provider-defaults",
        json={
            "text": {"provider_profile_id": alpha["id"], "model": "alpha-image"},
            "image": {"provider_profile_id": beta["id"], "model": "beta-image"},
        },
    )
    assert incompatible.status_code == 422

    refreshed = client.post(f"/api/providers/{alpha['id']}/models/refresh")
    assert refreshed.status_code == 200, refreshed.text
    assert {model["id"] for model in refreshed.json()["models"]} == {
        "alpha-text",
        "alpha-image",
    }
    classified = client.patch(
        f"/api/providers/{alpha['id']}/models",
        json={"models": [{"id": "alpha-vision-custom", "modalities": ["text", "image"]}]},
    )
    custom = next(model for model in classified.json()["models"] if model["id"] == "alpha-vision-custom")
    assert custom == {
        "id": "alpha-vision-custom",
        "modalities": ["image", "text"],
        "classification": "manual",
        "available": False,
    }
    cached_before = client.get(f"/api/providers/{alpha['id']}/models").json()

    class BrokenCatalog:
        async def discover_models(self) -> list[dict[str, Any]]:
            raise ProviderError("catalog offline", ErrorCategory.NETWORK)

    monkeypatch.setattr(api_module, "build_provider", lambda _profile, _vault: BrokenCatalog())
    failed = client.post(f"/api/providers/{alpha['id']}/models/refresh")
    assert failed.status_code == 502
    assert client.get(f"/api/providers/{alpha['id']}/models").json() == cached_before

    archived = client.post(f"/api/providers/{beta['id']}/archive")
    assert archived.json()["is_active"] is False
    assert client.get("/api/provider-defaults").json()["image"] is None
    assert all(
        provider["id"] != beta["id"]
        for provider in client.get("/api/providers", params={"include_archived": False}).json()
    )
    assert client.post(f"/api/providers/{beta['id']}/restore").json()["is_active"] is True


def test_task_routes_costs_and_provider_configuration_freeze_at_confirmation(
    client: TestClient,
    project_root: Any,
) -> None:
    project = create_project(client, project_root)
    text_asset = create_asset(client, project["id"], key="item.route-text", title="路由文字")
    image_asset = create_asset(
        client,
        project["id"],
        key="portrait.route-image",
        kind="media",
        subtype="portrait",
        title="路由图片",
    )
    alpha = create_provider(
        client,
        "Route Alpha",
        text_model="alpha-text",
        image_model="alpha-image",
        concurrency=1,
        pricing={"text_call": 0.2, "image_call": 0.7},
    )
    beta = create_provider(
        client,
        "Route Beta",
        text_model="beta-text",
        image_model="beta-image",
        concurrency=2,
        pricing={"text_call": 0.3, "image_call": 0.8},
    )
    client.put(
        "/api/provider-defaults",
        json={
            "text": {"provider_profile_id": alpha["id"], "model": "alpha-text"},
            "image": {"provider_profile_id": beta["id"], "model": "beta-image"},
        },
    )
    plan_response = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "name": "mixed routes",
            "max_concurrency": 3,
            "tasks": [
                text_task("write", text_asset["id"]),
                image_task("draw-default", image_asset["id"], depends_on=["write"]),
                image_task(
                    "draw-override",
                    image_asset["id"],
                    provider_id=alpha["id"],
                    model="alpha-image",
                ),
            ],
        },
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    assert plan["estimated_cost"] == pytest.approx(1.7)
    assert [
        (task["provider_profile_id"], task["model"])
        for task in plan["tasks"]
    ] == [
        (alpha["id"], "alpha-text"),
        (beta["id"], "beta-image"),
        (alpha["id"], "alpha-image"),
    ]

    client.put(
        "/api/provider-defaults",
        json={
            "text": {"provider_profile_id": beta["id"], "model": "beta-text"},
            "image": {"provider_profile_id": alpha["id"], "model": "alpha-image"},
        },
    )
    jobs_response = client.post(f"/api/generation-plans/{plan['id']}/confirm")
    assert jobs_response.status_code == 200, jobs_response.text
    jobs = jobs_response.json()
    assert [(job["provider_profile_id"], job["request"]["model"]) for job in jobs] == [
        (alpha["id"], "alpha-text"),
        (beta["id"], "beta-image"),
        (alpha["id"], "alpha-image"),
    ]
    alpha_snapshot = next(job["provider_snapshot"] for job in jobs if job["task_id"] == "write")
    assert alpha_snapshot["concurrency"] == 1
    assert alpha_snapshot["pricing"]["text_call"] == 0.2

    client.patch(
        f"/api/providers/{alpha['id']}",
        json={
            "base_url": "https://changed.invalid/v1",
            "text_model": "changed-text",
            "concurrency": 9,
            "pricing": {"text_call": 99},
        },
    )
    inspected = client.get(f"/api/generation-plans/{plan['id']}/inspect").json()
    frozen = next(job["provider_snapshot"] for job in inspected["jobs"] if job["task_id"] == "write")
    assert frozen == alpha_snapshot


def test_mixed_credentials_keep_cross_provider_dependency_waiting(
    client: TestClient,
    project_root: Any,
) -> None:
    project = create_project(client, project_root)
    text_asset = create_asset(client, project["id"], key="item.locked-upstream")
    image_asset = create_asset(
        client,
        project["id"],
        key="portrait.waiting-downstream",
        kind="media",
        subtype="portrait",
    )
    locked = create_provider(
        client,
        "Locked OpenAI",
        kind="openai_compatible",
        text_model="locked-text",
        image_model="locked-image",
    )
    ready = create_provider(
        client,
        "Ready Fake",
        text_model="ready-text",
        image_model="ready-image",
    )
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "tasks": [
                text_task(
                    "locked-root",
                    text_asset["id"],
                    provider_id=locked["id"],
                    model="locked-text",
                ),
                image_task(
                    "ready-child",
                    image_asset["id"],
                    provider_id=ready["id"],
                    model="ready-image",
                    depends_on=["locked-root"],
                ),
            ],
        },
    ).json()
    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    by_task = {job["task_id"]: job for job in jobs}
    assert by_task["locked-root"]["status"] == "credentials_locked"
    assert by_task["ready-child"]["status"] == "queued"
    time.sleep(0.08)
    assert client.get(f"/api/jobs/{by_task['ready-child']['id']}").json()["status"] == "queued"


@pytest.mark.asyncio
async def test_model_unavailable_does_not_issue_structured_text_fallback() -> None:
    provider = OpenAICompatibleProvider(
        ProviderRuntimeConfig(
            id="provider-test",
            kind="openai_compatible",
            base_url="https://provider.example/v1",
            text_model="default-text",
            image_model="default-image",
            quality="high",
            allow_private_network=False,
        ),
        "sk-test",
    )
    provider._chat = AsyncMock(  # type: ignore[method-assign]
        side_effect=ProviderError(
            "provider model is unavailable",
            ErrorCategory.VALIDATION,
            status_code=404,
        )
    )
    with pytest.raises(ProviderError, match="model is unavailable"):
        await provider.structured_text(
            prompt="test",
            schema={"type": "object"},
            model="removed-model",
            idempotency_key="attempt",
        )
    assert provider._chat.await_count == 1


class ModelUnavailableProvider:
    requires_credentials = False

    async def structured_text(self, **_kwargs: Any) -> ProviderResult:
        raise ProviderError(
            "provider model is unavailable",
            ErrorCategory.VALIDATION,
            status_code=404,
        )

    async def image(self, **_kwargs: Any) -> ProviderResult:
        raise ProviderError(
            "provider model is unavailable",
            ErrorCategory.VALIDATION,
            status_code=404,
        )


def test_runner_records_model_unavailable_and_waits_without_retry(
    client: TestClient,
    project_root: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "game_assets_api.runner.build_provider",
        lambda _profile, _vault: ModelUnavailableProvider(),
    )
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"], key="item.removed-model")
    profile = create_provider(
        client,
        "Removed Model",
        text_model="removed-text-model",
        image_model="removed-image-model",
    )
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "max_transport_retries": 4,
            "tasks": [
                text_task(
                    "removed-model-task",
                    asset["id"],
                    provider_id=profile["id"],
                    model="removed-text-model",
                )
            ],
        },
    ).json()
    [job] = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    [finished] = wait_for_jobs(client, [job["id"]])

    assert finished["status"] == "awaiting_user"
    assert finished["attempt_count"] == 1
    inspection = client.get(f"/api/generation-plans/{plan['id']}/inspect").json()
    unavailable = [
        event
        for event in inspection["events"]
        if event["event_type"] == "provider.model_unavailable"
    ]
    assert len(unavailable) == 1
    assert unavailable[0]["data"] == {
        "provider_profile_id": profile["id"],
        "model": "removed-text-model",
        "message": "provider model is unavailable",
    }
    assert len(inspection["attempts"]) == 1


class ConcurrencyTracker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.by_provider: dict[str, int] = {}
        self.max_by_provider: dict[str, int] = {}

    def enter(self, provider_id: str) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.by_provider[provider_id] = self.by_provider.get(provider_id, 0) + 1
            self.max_by_provider[provider_id] = max(
                self.max_by_provider.get(provider_id, 0),
                self.by_provider[provider_id],
            )

    def leave(self, provider_id: str) -> None:
        with self.lock:
            self.active -= 1
            self.by_provider[provider_id] -= 1


class SlowProvider:
    requires_credentials = False

    def __init__(self, profile: Any, tracker: ConcurrencyTracker):
        self.delegate = FakeProvider(profile)
        self.profile_id = profile.id
        self.tracker = tracker

    async def structured_text(self, **kwargs: Any) -> ProviderResult:
        self.tracker.enter(self.profile_id)
        try:
            await asyncio.sleep(0.12)
            return await self.delegate.structured_text(**kwargs)
        finally:
            self.tracker.leave(self.profile_id)

    async def image(self, **kwargs: Any) -> ProviderResult:
        self.tracker.enter(self.profile_id)
        try:
            await asyncio.sleep(0.12)
            return await self.delegate.image(**kwargs)
        finally:
            self.tracker.leave(self.profile_id)

    async def discover_models(self) -> list[dict[str, Any]]:
        return await self.delegate.discover_models()

    async def test_connection(self) -> list[str]:
        return await self.delegate.test_connection()


def test_plan_and_each_provider_apply_independent_concurrency_limits(
    client: TestClient,
    project_root: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ConcurrencyTracker()
    monkeypatch.setattr(
        "game_assets_api.runner.build_provider",
        lambda profile, _vault: SlowProvider(profile, tracker),
    )
    project = create_project(client, project_root)
    alpha = create_provider(
        client,
        "Concurrency Alpha",
        text_model="alpha-text",
        image_model="alpha-image",
        concurrency=1,
    )
    beta = create_provider(
        client,
        "Concurrency Beta",
        text_model="beta-text",
        image_model="beta-image",
        concurrency=2,
    )
    assets = [
        create_asset(client, project["id"], key=f"item.route-{index}", title=f"并发 {index}")
        for index in range(6)
    ]
    tasks = [
        text_task(
            f"alpha-{index}",
            assets[index]["id"],
            provider_id=alpha["id"],
            model="alpha-text",
        )
        for index in range(3)
    ] + [
        text_task(
            f"beta-{index}",
            assets[index]["id"],
            provider_id=beta["id"],
            model="beta-text",
        )
        for index in range(3, 6)
    ]
    plan = client.post(
        "/api/generation-plans",
        json={"project_id": project["id"], "max_concurrency": 3, "tasks": tasks},
    ).json()
    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    client.patch(f"/api/providers/{alpha['id']}", json={"concurrency": 8})
    finished = wait_for_jobs(client, [job["id"] for job in jobs])

    assert {job["status"] for job in finished} == {"candidate_ready"}
    assert tracker.max_active == 3
    assert tracker.max_by_provider == {alpha["id"]: 1, beta["id"]: 2}
