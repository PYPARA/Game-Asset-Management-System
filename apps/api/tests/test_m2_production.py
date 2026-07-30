from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from game_assets_api import cli as cli_module
from game_assets_api import services as services_module
from game_assets_api.database import Database
from game_assets_api.main import create_app
from game_assets_api.providers import FakeProvider, ProviderResult
from game_assets_api.settings import Settings

from .conftest import create_asset, create_fake_provider, create_project


TERMINAL_JOB_STATUSES = {
    "candidate_ready",
    "awaiting_user",
    "failed",
    "cancelled",
    "credentials_locked",
}


def wait_for_job(client: TestClient, job_id: str, timeout: float = 8.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in TERMINAL_JOB_STATUSES:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def wait_for_attempt_phase(
    client: TestClient,
    job_id: str,
    phase: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        attempts = client.get(f"/api/jobs/{job_id}/attempts").json()
        if attempts and attempts[-1]["phase"] == phase:
            return attempts[-1]
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not persist attempt phase {phase}")


def create_provider(
    client: TestClient,
    *,
    concurrency: int = 6,
    pricing: dict[str, float] | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/providers",
        json={
            "name": "M2 deterministic fake",
            "kind": "fake",
            "base_url": "https://fake.invalid/v1",
            "text_model": "fake-text",
            "image_model": "fake-image",
            "concurrency": concurrency,
            "pricing": pricing,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def text_task(task_id: str, asset_id: str, *, prompt: str | None = None) -> dict[str, Any]:
    return {
        "id": task_id,
        "kind": "text",
        "asset_id": asset_id,
        "prompt": prompt or task_id,
        "schema": {
            "type": "object",
            "required": ["value"],
            "properties": {"value": {"type": "string"}},
            "additionalProperties": False,
        },
    }


class ConcurrencyTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.by_group: dict[str, int] = {}
        self.max_by_group: dict[str, int] = {}

    def enter(self, prompt: str) -> str:
        group = prompt.split("-", 1)[0]
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.by_group[group] = self.by_group.get(group, 0) + 1
            self.max_by_group[group] = max(
                self.max_by_group.get(group, 0), self.by_group[group]
            )
        return group

    def leave(self, group: str) -> None:
        with self._lock:
            self.active -= 1
            self.by_group[group] -= 1


class SlowFakeProvider:
    requires_credentials = False

    def __init__(self, profile: Any, tracker: ConcurrencyTracker):
        self._delegate = FakeProvider(profile)
        self._tracker = tracker

    async def _tracked(
        self,
        prompt: str,
        operation: Callable[[], Any],
    ) -> ProviderResult:
        group = self._tracker.enter(prompt)
        try:
            await asyncio.sleep(0.12)
            return await operation()
        finally:
            self._tracker.leave(group)

    async def structured_text(
        self,
        *,
        prompt: str,
        schema: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        return await self._tracked(
            prompt,
            lambda: self._delegate.structured_text(
                prompt=prompt,
                schema=schema,
                idempotency_key=idempotency_key,
            ),
        )

    async def image(
        self,
        *,
        prompt: str,
        width: int | None,
        height: int | None,
        reference: bytes | None = None,
        idempotency_key: str | None = None,
    ) -> ProviderResult:
        return await self._tracked(
            prompt,
            lambda: self._delegate.image(
                prompt=prompt,
                width=width,
                height=height,
                reference=reference,
                idempotency_key=idempotency_key,
            ),
        )

    async def test_connection(self) -> list[str]:
        return await self._delegate.test_connection()


def test_provider_and_plan_concurrency_are_hard_limits(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = ConcurrencyTracker()
    monkeypatch.setattr(
        "game_assets_api.runner.build_provider",
        lambda profile, _vault: SlowFakeProvider(profile, tracker),
    )
    project = create_project(client, project_root)
    provider = create_provider(client, concurrency=3)
    assets = [
        create_asset(
            client,
            project["id"],
            key=f"item.concurrent-{index}",
            subtype="item",
            title=f"并发资产 {index}",
        )
        for index in range(6)
    ]
    plans = []
    for group, indices, plan_limit in (("A", range(3), 1), ("B", range(3, 6), 2)):
        response = client.post(
            "/api/generation-plans",
            json={
                "project_id": project["id"],
                "provider_profile_id": provider["id"],
                "name": f"并发计划 {group}",
                "max_concurrency": plan_limit,
                "tasks": [
                    text_task(f"{group}-{index}", assets[index]["id"], prompt=f"{group}-{index}")
                    for index in indices
                ],
            },
        )
        assert response.status_code == 201, response.text
        plans.append(response.json())

    job_ids: list[str] = []
    for plan in plans:
        response = client.post(f"/api/generation-plans/{plan['id']}/confirm")
        assert response.status_code == 200, response.text
        job_ids.extend(job["id"] for job in response.json())
    finished = [wait_for_job(client, job_id) for job_id in job_ids]

    assert {job["status"] for job in finished} == {"candidate_ready"}
    assert tracker.max_active == 3
    assert tracker.max_by_group == {"A": 1, "B": 2}


def test_dag_injects_real_upstream_output_and_run_apis_are_cursor_based(
    client: TestClient,
    project_root: Path,
) -> None:
    project = create_project(client, project_root)
    upstream_asset = create_asset(
        client,
        project["id"],
        key="item.upstream",
        subtype="item",
        title="上游定义",
    )
    downstream_asset = create_asset(
        client,
        project["id"],
        key="item.downstream",
        subtype="item",
        title="下游定义",
    )
    provider = create_provider(client, pricing={"text_call": 0.25})
    plan_response = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "name": "真实 DAG 注入",
            "extra_call_budget": 1,
            "tasks": [
                {
                    "id": "source",
                    "kind": "text",
                    "asset_id": upstream_asset["id"],
                    "prompt": "生成上游",
                    "schema": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {"name": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
                {
                    "id": "consumer",
                    "kind": "text",
                    "asset_id": downstream_asset["id"],
                    "prompt": "消费上游",
                    "depends_on": ["source"],
                    "schema": {
                        "type": "object",
                        "required": ["prompt"],
                        "properties": {"prompt": {"type": "string"}},
                        "additionalProperties": False,
                    },
                },
            ],
        },
    )
    assert plan_response.status_code == 201, plan_response.text
    plan = plan_response.json()
    assert plan["estimated_calls"] == 2
    assert plan["estimated_cost"] == 0.5
    assert plan["suggested_extra_calls"] == 2
    assert plan["extra_call_budget"] == 1

    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    finished = {job["task_id"]: wait_for_job(client, job["id"]) for job in jobs}
    source_revision = client.get(
        f"/api/revisions/{finished['source']['result_revision_id']}"
    ).json()
    consumer_revision = client.get(
        f"/api/revisions/{finished['consumer']['result_revision_id']}"
    ).json()
    injected = finished["consumer"]["resolved_request"]["upstream_outputs"][0]

    assert injected == {
        "task_id": "source",
        "job_id": finished["source"]["id"],
        "revision_id": source_revision["id"],
        "content_hash": source_revision["content_hash"],
        "content": source_revision["content"],
    }
    assert "[UPSTREAM_OUTPUTS]" in consumer_revision["content"]["prompt"]
    assert source_revision["content_hash"] in consumer_revision["content"]["prompt"]

    inspection = client.get(f"/api/generation-plans/{plan['id']}/inspect")
    assert inspection.status_code == 200, inspection.text
    inspected = inspection.json()
    assert inspected["plan"]["status"] == "candidate_ready"
    assert len(inspected["jobs"]) == 2
    assert len(inspected["attempts"]) == 2
    assert [event["sequence"] for event in inspected["events"]] == sorted(
        event["sequence"] for event in inspected["events"]
    )

    events = client.get("/api/run-events", params={"plan_id": plan["id"]}).json()
    cursor = events[-1]["sequence"]
    assert client.get(
        "/api/run-events", params={"plan_id": plan["id"], "after": cursor}
    ).json() == []
    budget = client.patch(
        f"/api/generation-plans/{plan['id']}/budget",
        json={"extra_call_budget": 3},
    )
    assert budget.status_code == 200, budget.text
    assert budget.json()["extra_call_budget"] == 3
    new_events = client.get(
        "/api/run-events", params={"plan_id": plan["id"], "after": cursor}
    ).json()
    assert [event["event_type"] for event in new_events] == ["budget.changed"]


def test_image_evidence_includes_contact_sheets_and_comparisons(
    client: TestClient,
    project_root: Path,
) -> None:
    project = create_project(client, project_root)
    source_asset = create_asset(
        client,
        project["id"],
        key="portrait.evidence-source",
        kind="media",
        subtype="portrait",
        title="证据来源",
    )
    edit_asset = create_asset(
        client,
        project["id"],
        key="portrait.evidence-edit",
        kind="media",
        subtype="portrait",
        title="证据编辑",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "source",
                    "kind": "image",
                    "asset_id": source_asset["id"],
                    "prompt": "证据来源",
                    "width": 72,
                    "height": 72,
                    "transparent": True,
                },
                {
                    "id": "edit",
                    "kind": "image_edit",
                    "asset_id": edit_asset["id"],
                    "prompt": "基于来源编辑",
                    "width": 72,
                    "height": 72,
                    "transparent": True,
                    "depends_on": ["source"],
                    "reference_task_id": "source",
                },
            ],
        },
    ).json()
    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    assert {wait_for_job(client, job["id"])["status"] for job in jobs} == {
        "candidate_ready"
    }

    evidence = client.get("/api/run-evidence", params={"plan_id": plan["id"]}).json()
    kinds = {item["kind"] for item in evidence}
    assert {
        "candidate",
        "comparison_unavailable",
        "side_by_side",
        "overlay",
        "difference",
        "contact_sheet_dark",
        "contact_sheet_light",
        "contact_sheet_checkerboard",
    } <= kinds
    unavailable = next(item for item in evidence if item["kind"] == "comparison_unavailable")
    assert unavailable["path"] is None
    assert unavailable["metadata"]["reason"] == "task has no reference input"
    for item in evidence:
        if not item["path"]:
            continue
        content = client.get(f"/api/run-evidence/{item['id']}/content")
        assert content.status_code == 200, item
        assert content.headers["content-type"] == "image/webp"
        assert content.content

    candidate = next(item for item in evidence if item["kind"] == "candidate")
    (project_root / candidate["path"]).write_bytes(b"tampered")
    corrupted = client.get(f"/api/run-evidence/{candidate['id']}/content")
    assert corrupted.status_code == 409
    assert "hash" in corrupted.json()["detail"]


def test_qa_finding_can_be_repaired_by_registered_worker(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_inspect = services_module.inspect_image
    inspection_count = 0
    inspection_lock = threading.Lock()

    def fail_first_inspection(*args: Any, **kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
        nonlocal inspection_count
        with inspection_lock:
            inspection_count += 1
            current = inspection_count
        if current == 1:
            return (
                "fail",
                [{"name": "width", "passed": False, "value": 32, "expected": 64}],
            )
        return original_inspect(*args, **kwargs)

    monkeypatch.setattr(services_module, "inspect_image", fail_first_inspection)
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.worker-repair",
        kind="media",
        subtype="portrait",
        title="Worker 修复",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "待修复候选",
                    "width": 64,
                    "height": 64,
                    "transparent": True,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    failed_job = wait_for_job(client, job_id)
    assert failed_job["status"] == "awaiting_user"
    failed_revision_id = failed_job["result_revision_id"]
    findings = client.get("/api/findings", params={"job_id": job_id}).json()
    assert [(finding["code"], finding["suggested_action"]) for finding in findings] == [
        ("media.width_mismatch", "tool_repair")
    ]

    unknown = client.post(
        f"/api/jobs/{job_id}/remediations",
        json={
            "action": "tool_repair",
            "strategy": "not_registered",
            "reason": "验证未知 Worker 会被拒绝",
            "finding_ids": [findings[0]["id"]],
        },
    )
    assert unknown.status_code == 422
    assert "unknown media worker strategy" in unknown.json()["detail"]

    remediation = client.post(
        f"/api/jobs/{job_id}/remediations",
        json={
            "action": "tool_repair",
            "strategy": "normalize",
            "reason": "尺寸问题使用确定性归一化修复",
            "parameters": {"width": 64, "height": 64, "transparent": True},
            "finding_ids": [findings[0]["id"]],
        },
    )
    assert remediation.status_code == 201, remediation.text
    repaired_job = wait_for_job(client, job_id)
    assert repaired_job["status"] == "candidate_ready"
    assert repaired_job["result_revision_id"] != failed_revision_id
    repaired_revision = client.get(
        f"/api/revisions/{repaired_job['result_revision_id']}"
    ).json()
    assert repaired_revision["parent_revision_id"] == failed_revision_id
    assert repaired_revision["provider_snapshot"]["producer"] == "media_worker"
    attempts = client.get(f"/api/jobs/{job_id}/attempts").json()
    assert [attempt["billable"] for attempt in attempts] == [True, False]
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 1
    resolved_findings = client.get("/api/findings", params={"job_id": job_id}).json()
    assert resolved_findings[0]["resolved_at"] is not None
    actions = client.get("/api/remediations", params={"job_id": job_id}).json()
    assert actions[0]["status"] == "completed"


def test_each_qa_run_supersedes_previous_finding_occurrences(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspection_count = 0

    def evolving_inspection(*_args: Any, **_kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
        nonlocal inspection_count
        inspection_count += 1
        if inspection_count == 1:
            return (
                "fail",
                [
                    {"name": "width", "passed": False, "value": 32, "expected": 64},
                    {"name": "height", "passed": False, "value": 32, "expected": 64},
                ],
            )
        return (
            "fail",
            [
                {"name": "width", "passed": True, "value": 64, "expected": 64},
                {"name": "height", "passed": False, "value": 32, "expected": 64},
            ],
        )

    monkeypatch.setattr(services_module, "inspect_image", evolving_inspection)
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.finding-lifecycle",
        kind="media",
        subtype="portrait",
        title="Finding 生命周期",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "验证 Finding 生命周期",
                    "width": 64,
                    "height": 64,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    assert wait_for_job(client, job_id)["status"] == "awaiting_user"
    first_findings = client.get("/api/findings", params={"job_id": job_id}).json()
    remediation = client.post(
        f"/api/jobs/{job_id}/remediations",
        json={
            "action": "tool_repair",
            "strategy": "normalize",
            "reason": "重新执行尺寸归一化并复验",
            "finding_ids": [finding["id"] for finding in first_findings],
        },
    )
    assert remediation.status_code == 201, remediation.text
    assert wait_for_job(client, job_id)["status"] == "awaiting_user"

    findings = client.get("/api/findings", params={"job_id": job_id}).json()
    width_findings = [item for item in findings if item["code"] == "media.width_mismatch"]
    height_findings = [item for item in findings if item["code"] == "media.height_mismatch"]
    assert len(width_findings) == 1
    assert width_findings[0]["resolved_at"] is not None
    assert [item["occurrence"] for item in height_findings] == [1, 2]
    assert height_findings[0]["resolved_at"] is not None
    assert height_findings[1]["resolved_at"] is None


def test_budget_and_per_asset_paid_remediation_limits_are_independent(
    client: TestClient,
    project_root: Path,
) -> None:
    project = create_project(client, project_root)
    provider = create_fake_provider(client)
    assets = [
        create_asset(
            client,
            project["id"],
            key=f"portrait.budget-{index}",
            kind="media",
            subtype="portrait",
            title=f"预算资产 {index}",
        )
        for index in range(3)
    ]
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "extra_call_budget": 1,
            "max_paid_remediation_rounds": 3,
            "tasks": [
                {
                    "id": f"draw-{index}",
                    "kind": "image",
                    "asset_id": assets[index]["id"],
                    "prompt": f"预算 {index}",
                    "width": 48,
                    "height": 48,
                }
                for index in range(2)
            ],
        },
    ).json()
    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    finished = [wait_for_job(client, job["id"]) for job in jobs]
    first = client.post(
        f"/api/jobs/{finished[0]['id']}/remediations",
        json={
            "action": "regenerate",
            "strategy": "prompt-tightening",
            "reason": "首次付费返工",
            "parameters": {"prompt_suffix": "加强轮廓"},
            "expected_additional_calls": 1,
        },
    )
    assert first.status_code == 201, first.text
    exhausted = client.post(
        f"/api/jobs/{finished[1]['id']}/remediations",
        json={
            "action": "regenerate",
            "strategy": "alternate-composition",
            "reason": "计划预算已满时不得入队",
            "expected_additional_calls": 1,
        },
    )
    assert exhausted.status_code == 409
    assert exhausted.json()["detail"] == "plan extra-call budget is exhausted"
    wait_for_job(client, finished[0]["id"])
    updated_plan = client.get(f"/api/generation-plans/{plan['id']}").json()
    assert updated_plan["extra_calls_used"] == 1
    assert updated_plan["actual_calls"] == 3
    too_low = client.patch(
        f"/api/generation-plans/{plan['id']}/budget",
        json={"extra_call_budget": 0},
    )
    assert too_low.status_code == 409

    round_plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "extra_call_budget": 3,
            "max_paid_remediation_rounds": 1,
            "tasks": [
                {
                    "id": "round-limited",
                    "kind": "image",
                    "asset_id": assets[2]["id"],
                    "prompt": "单资产轮次",
                    "width": 48,
                    "height": 48,
                }
            ],
        },
    ).json()
    round_job_id = client.post(
        f"/api/generation-plans/{round_plan['id']}/confirm"
    ).json()[0]["id"]
    wait_for_job(client, round_job_id)
    accepted = client.post(
        f"/api/jobs/{round_job_id}/remediations",
        json={
            "action": "regenerate",
            "strategy": "first-round",
            "reason": "允许的唯一付费轮次",
            "expected_additional_calls": 1,
        },
    )
    assert accepted.status_code == 201, accepted.text
    wait_for_job(client, round_job_id)
    round_exhausted = client.post(
        f"/api/jobs/{round_job_id}/remediations",
        json={
            "action": "image_edit",
            "strategy": "second-round",
            "reason": "超过单资产上限",
            "expected_additional_calls": 1,
        },
    )
    assert round_exhausted.status_code == 409
    assert round_exhausted.json()["detail"] == (
        "asset paid-remediation round limit is exhausted"
    )
    limited_job = client.get(f"/api/jobs/{round_job_id}").json()
    assert limited_job["paid_remediation_rounds"] == 1


@pytest.mark.parametrize("crash_phase", ["output_received", "staged"])
def test_restart_recovers_persisted_output_without_another_provider_call(
    tmp_path: Path,
    crash_phase: str,
) -> None:
    settings = Settings(
        projects_root=tmp_path / "projects",
        state_dir=tmp_path / "state",
        database_url=f"sqlite:///{tmp_path / 'm2-recovery.sqlite3'}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    project_root = settings.projects_root / "sample-game"
    project_root.mkdir(parents=True)
    with TestClient(create_app(settings)) as first_client:
        project = create_project(first_client, project_root)
        asset = create_asset(
            first_client,
            project["id"],
            key=f"item.recovery-{crash_phase}",
            subtype="item",
            title="恢复测试",
        )
        provider = create_fake_provider(first_client)
        plan = first_client.post(
            "/api/generation-plans",
            json={
                "project_id": project["id"],
                "provider_profile_id": provider["id"],
                "tasks": [text_task("recover", asset["id"])],
            },
        ).json()
        runner = first_client.app.state.runner

        if crash_phase == "output_received":

            async def stop_after_output(**_kwargs: Any) -> None:
                await asyncio.Event().wait()

            runner._stage_and_complete = stop_after_output
        else:

            async def stop_after_staging(**kwargs: Any) -> None:
                await asyncio.to_thread(
                    runner._stage_result,
                    kwargs["job_id"],
                    kwargs["lease_token"],
                    kwargs["attempt_id"],
                    kwargs["attempt_number"],
                    kwargs["result"],
                    kwargs["schema"],
                )
                await asyncio.Event().wait()

            runner._stage_and_complete = stop_after_staging

        job_id = first_client.post(
            f"/api/generation-plans/{plan['id']}/confirm"
        ).json()[0]["id"]
        wait_for_attempt_phase(first_client, job_id, crash_phase)
        assert first_client.get(f"/api/generation-plans/{plan['id']}").json()[
            "actual_calls"
        ] == 1

    with TestClient(create_app(settings)) as recovered_client:
        recovered = wait_for_job(recovered_client, job_id)
        assert recovered["status"] == "candidate_ready"
        attempts = recovered_client.get(f"/api/jobs/{job_id}/attempts").json()
        assert len(attempts) == 1
        assert attempts[0]["phase"] == "succeeded"
        recovered_plan = recovered_client.get(f"/api/generation-plans/{plan['id']}").json()
        assert recovered_plan["actual_calls"] == 1
        events = recovered_client.get(
            "/api/run-events", params={"plan_id": plan["id"]}
        ).json()
        recovery_event = next(event for event in events if event["event_type"] == "run.recovered")
        assert recovery_event["data"]["recoverable_output"] is True
        assert recovery_event["data"]["previous_runner"].startswith("runner-")


def test_unknown_delivery_after_restart_requires_explicit_action(tmp_path: Path) -> None:
    settings = Settings(
        projects_root=tmp_path / "projects",
        state_dir=tmp_path / "state",
        database_url=f"sqlite:///{tmp_path / 'm2-unknown.sqlite3'}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    project_root = settings.projects_root / "sample-game"
    project_root.mkdir(parents=True)
    with TestClient(create_app(settings)) as first_client:
        project = create_project(first_client, project_root)
        asset = create_asset(
            first_client,
            project["id"],
            key="item.unknown-delivery",
            subtype="item",
            title="未知交付",
        )
        provider = create_fake_provider(first_client)
        plan = first_client.post(
            "/api/generation-plans",
            json={
                "project_id": project["id"],
                "provider_profile_id": provider["id"],
                "tasks": [text_task("unknown", asset["id"])],
            },
        ).json()
        runner = first_client.app.state.runner

        async def never_returns(*_args: Any, **_kwargs: Any) -> ProviderResult:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        runner._invoke = never_returns
        job_id = first_client.post(
            f"/api/generation-plans/{plan['id']}/confirm"
        ).json()[0]["id"]
        wait_for_attempt_phase(first_client, job_id, "dispatched")
        assert first_client.get(f"/api/generation-plans/{plan['id']}").json()[
            "actual_calls"
        ] == 1

    with TestClient(create_app(settings)) as recovered_client:
        job = recovered_client.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "awaiting_user"
        assert "unknown" in job["error_message"]
        assert len(recovered_client.get(f"/api/jobs/{job_id}/attempts").json()) == 1
        assert recovered_client.get(f"/api/generation-plans/{plan['id']}").json()[
            "actual_calls"
        ] == 1
        unsafe_resume = recovered_client.post(f"/api/jobs/{job_id}/resume")
        assert unsafe_resume.status_code == 409
        plan_resume = recovered_client.post(f"/api/generation-plans/{plan['id']}/resume")
        assert plan_resume.status_code == 200
        assert plan_resume.json()[0]["status"] == "awaiting_user"

        retry = recovered_client.post(
            f"/api/jobs/{job_id}/remediations",
            json={
                "action": "retry",
                "strategy": "manual-confirmed-retry",
                "reason": "人工确认未知调用后显式重试",
            },
        )
        assert retry.status_code == 201, retry.text
        completed = wait_for_job(recovered_client, job_id)
        assert completed["status"] == "candidate_ready"
        attempts = recovered_client.get(f"/api/jobs/{job_id}/attempts").json()
        assert [attempt["purpose"] for attempt in attempts] == ["base", "retry"]
        assert recovered_client.get(f"/api/generation-plans/{plan['id']}").json()[
            "actual_calls"
        ] == 2


def test_stale_remediation_input_is_rejected_before_worker_runs(
    client: TestClient,
    project_root: Path,
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.stale-action",
        kind="media",
        subtype="portrait",
        title="过期动作",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "过期输入",
                    "width": 40,
                    "height": 40,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    assert wait_for_job(client, job_id)["status"] == "candidate_ready"

    runner = client.app.state.runner
    client.portal.call(runner.stop)
    action = client.post(
        f"/api/jobs/{job_id}/remediations",
        json={
            "action": "tool_repair",
            "strategy": "normalize",
            "reason": "动作创建后输入被替换",
        },
    )
    assert action.status_code == 201, action.text
    with client.app.state.database.sessions() as session:
        from game_assets_api.models import GenerationJob

        persisted_job = session.get(GenerationJob, job_id)
        assert persisted_job is not None
        persisted_job.resolved_request_json = {
            **persisted_job.resolved_request_json,
            "external_change": True,
        }
        session.commit()
    assert client.portal.call(runner.run_once) is True

    rejected_job = client.get(f"/api/jobs/{job_id}").json()
    assert rejected_job["status"] == "awaiting_user"
    assert rejected_job["pending_action_id"] is None
    rejected_action = client.get("/api/remediations", params={"job_id": job_id}).json()[0]
    assert rejected_action["status"] == "rejected"
    assert len(client.get(f"/api/jobs/{job_id}/attempts").json()) == 1


def test_locked_provider_job_resume_stays_fail_closed(
    client: TestClient,
    project_root: Path,
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"], key="item.locked-provider")
    provider_response = client.post(
        "/api/providers",
        json={
            "name": "Locked provider",
            "kind": "openai_compatible",
            "base_url": "https://provider.invalid/v1",
            "text_model": "text-model",
            "image_model": "image-model",
        },
    )
    assert provider_response.status_code == 201, provider_response.text
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider_response.json()["id"],
            "tasks": [text_task("locked", asset["id"])],
        },
    ).json()
    job = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]
    assert job["status"] == "credentials_locked"
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["status"] == (
        "credentials_locked"
    )

    resume = client.post(f"/api/jobs/{job['id']}/resume")
    assert resume.status_code == 200, resume.text
    assert resume.json()["status"] == "credentials_locked"


def test_lease_configuration_rejects_heartbeat_after_expiry(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="heartbeat interval must be shorter"):
        Settings(
            projects_root=tmp_path / "projects",
            state_dir=tmp_path / "state",
            job_lease_seconds=5,
            job_heartbeat_interval=5,
        )


def test_legacy_sqlite_schema_upgrades_without_reset(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE generation_plans (
                id VARCHAR(36) PRIMARY KEY,
                project_id VARCHAR(36) NOT NULL,
                provider_profile_id VARCHAR(36) NOT NULL,
                status VARCHAR(40) NOT NULL,
                tasks_json JSON NOT NULL,
                estimated_calls INTEGER NOT NULL,
                estimated_cost FLOAT,
                confirmed_at DATETIME,
                created_at DATETIME NOT NULL
            );
            CREATE TABLE generation_jobs (
                id VARCHAR(36) PRIMARY KEY,
                plan_id VARCHAR(36) NOT NULL,
                project_id VARCHAR(36) NOT NULL,
                provider_profile_id VARCHAR(36) NOT NULL,
                task_id VARCHAR(160) NOT NULL,
                task_kind VARCHAR(40) NOT NULL,
                request_json JSON NOT NULL,
                status VARCHAR(40) NOT NULL,
                progress FLOAT NOT NULL,
                result_revision_id VARCHAR(36),
                error_category VARCHAR(40),
                error_message TEXT,
                cancel_requested BOOLEAN NOT NULL,
                attempt_count INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                CONSTRAINT uq_job_plan_task UNIQUE (plan_id, task_id)
            );
            CREATE TABLE generation_attempts (
                id VARCHAR(36) PRIMARY KEY,
                job_id VARCHAR(36) NOT NULL,
                number INTEGER NOT NULL,
                status VARCHAR(40) NOT NULL,
                request_id VARCHAR(240),
                error_category VARCHAR(40),
                error_message TEXT,
                started_at DATETIME NOT NULL,
                completed_at DATETIME
            );
            INSERT INTO generation_plans VALUES (
                'plan-legacy', 'project-legacy', 'provider-legacy', 'draft', '[]', 0,
                NULL, NULL, '2026-07-01 00:00:00'
            );
            INSERT INTO generation_jobs VALUES (
                'job-legacy', 'plan-legacy', 'project-legacy', 'provider-legacy',
                'task-legacy', 'text', '{}', 'queued', 0, NULL, NULL, NULL, 0, 0,
                '2026-07-01 00:00:00', '2026-07-01 00:00:00'
            );
            INSERT INTO generation_attempts VALUES (
                'attempt-legacy', 'job-legacy', 1, 'succeeded', NULL, NULL, NULL,
                '2026-07-01 00:00:00', '2026-07-01 00:00:01'
            );
            """
        )

    settings = Settings(
        projects_root=tmp_path / "projects",
        state_dir=tmp_path / "state",
        database_url=f"sqlite:///{database_path}",
    )
    database = Database(settings)
    database.create_schema()
    with database.engine.connect() as connection:
        plan_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(generation_plans)")
        }
        job_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(generation_jobs)")
        }
        attempt_columns = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA table_info(generation_attempts)")
        }
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        indexes = {
            row[1]
            for row in connection.exec_driver_sql("PRAGMA index_list(generation_jobs)")
        }
        legacy = connection.exec_driver_sql(
            "SELECT name, extra_call_budget, actual_calls FROM generation_plans "
            "WHERE id = 'plan-legacy'"
        ).one()

    assert {"name", "extra_call_budget", "actual_calls", "max_concurrency"} <= plan_columns
    assert {"stage", "lease_token", "heartbeat_at", "pending_action_id"} <= job_columns
    assert {"phase", "idempotency_key", "output_hash", "billable"} <= attempt_columns
    assert {"run_events", "production_findings", "run_evidence", "remediation_actions"} <= tables
    assert "ix_generation_jobs_lease_expires_at" in indexes
    assert legacy == ("未命名生成计划", 2, 0)


def test_gams_cli_inspects_and_safely_resumes_run(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="item.cli-run",
        subtype="item",
        title="CLI 运行",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "name": "CLI 验收",
            "tasks": [text_task("cli", asset["id"])],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    wait_for_job(client, job_id)
    tmp_path = project_root.parent.parent
    monkeypatch.setenv("GAME_ASSETS_PROJECTS_ROOT", str(project_root.parent))
    monkeypatch.setenv("GAME_ASSETS_STATE_DIR", str(tmp_path / "api-state"))
    monkeypatch.setenv("GAME_ASSETS_DATABASE_URL", f"sqlite:///{tmp_path / 'test.sqlite3'}")

    with pytest.raises(SystemExit) as inspect_exit:
        cli_module.main(["run", "inspect", plan["id"], "--json"])
    assert inspect_exit.value.code == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["plan"]["id"] == plan["id"]
    assert inspected["jobs"][0]["status"] == "candidate_ready"

    with pytest.raises(SystemExit) as resume_exit:
        cli_module.main(["run", "resume", plan["id"], "--json"])
    assert resume_exit.value.code == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed[0]["status"] == "candidate_ready"
