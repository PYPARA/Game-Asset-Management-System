from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from game_assets_api import providers as pm
from game_assets_api import runner as rm
from game_assets_api.dispatch_repair import reconcile_local_failures
from game_assets_api.domain import ErrorCategory
from game_assets_api.models import GenerationAttempt, GenerationPlan, RunEvent
from game_assets_api.providers import ProviderError
from .conftest import create_project, create_asset, create_fake_provider
from .test_m2_production import text_task, wait_for_job


def dns(monkeypatch, addresses):
    monkeypatch.setattr(pm.socket, "getaddrinfo", lambda *a, **k: [
        (pm.socket.AF_INET, pm.socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses])


@pytest.mark.parametrize("addresses,allowed", [
    (["198.18.0.68"], True), (["198.19.255.254", "8.8.8.8"], True),
    (["198.18.0.68", "10.0.0.1"], False), (["169.254.169.254"], False),
    (["::1"], False), (["fc00::1"], False),
])
def test_fake_ip_scope(monkeypatch, addresses, allowed):
    dns(monkeypatch, addresses)
    if allowed:
        assert pm.guard_resolved_host("https://provider.example/v1", allow_private_network=False) == "fake_ip_compatible"
    else:
        with pytest.raises(ProviderError) as caught:
            pm.guard_resolved_host("https://provider.example/v1", allow_private_network=False)
        assert caught.value.error_code == "provider_address_blocked"
    with pytest.raises(ValueError):
        pm.validate_base_url("https://198.18.0.68/v1", allow_private_network=False)


async def test_dns_timeout_does_not_block_event_loop(monkeypatch):
    def slow(*a, **k):
        time.sleep(.12)
        return []
    monkeypatch.setattr(pm.socket, "getaddrinfo", slow)
    ticks = []
    async def heartbeat():
        await asyncio.sleep(.005)
        ticks.append(True)
    with pytest.raises(ProviderError) as caught:
        await asyncio.gather(pm.check_provider_network("https://provider.example", allow_private_network=False, timeout=.03), heartbeat())
    assert caught.value.error_code == "provider_dns_timeout"
    assert ticks


def configured_job(client, project_root):
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"], key="item.dispatch", subtype="item")
    provider = create_fake_provider(client)
    plan = client.post("/api/generation-plans", json={"project_id":project["id"],
        "provider_profile_id":provider["id"], "tasks":[text_task("dispatch", asset["id"])]}).json()
    job = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]
    return plan, job


def test_local_failure_accounting_recovery_and_wait_dedup(client, project_root, monkeypatch):
    original = rm.build_provider
    def blocked(*a, **k):
        raise ProviderError("blocked before send", ErrorCategory.VALIDATION,
                            error_code="provider_address_blocked")
    monkeypatch.setattr(rm, "build_provider", blocked)
    plan, job = configured_job(client, project_root)
    wait_for_job(client, job["id"])
    attempts = client.get(f"/api/jobs/{job['id']}/attempts").json()
    first = attempts[0]
    assert first["dispatch_state"] == "not_sent" and not first["billable"]
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 0
    payload = {"action":"await_user", "strategy":"manual-handoff", "reason":"保留现场"}
    r1 = client.post(f"/api/jobs/{job['id']}/remediations", json=payload)
    r2 = client.post(f"/api/jobs/{job['id']}/remediations", json=payload)
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]
    assert client.get(f"/api/jobs/{job['id']}").json()["error_message"] == "blocked before send"
    monkeypatch.setattr(rm, "build_provider", original)
    retry = client.post(f"/api/jobs/{job['id']}/remediations", json={
        "action":"retry", "strategy":"same-request", "reason":"继续执行已确认任务"})
    assert retry.status_code == 201, retry.text
    completed = wait_for_job(client, job["id"])
    assert completed["status"] == "candidate_ready"
    attempts = client.get(f"/api/jobs/{job['id']}/attempts").json()
    assert attempts[1]["dispatch_state"] == "response_received"
    assert attempts[1]["idempotency_key"] == first["idempotency_key"]
    assert attempts[1]["request_hash"] == first["request_hash"]
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 1


def test_sent_timeout_is_not_automatically_retried(client, project_root, monkeypatch):
    async def timeout(self, **kwargs):
        raise ProviderError("response timeout", ErrorCategory.NETWORK)
    monkeypatch.setattr(pm.FakeProvider, "structured_text", timeout)
    plan, job = configured_job(client, project_root)
    wait_for_job(client, job["id"])
    attempts = client.get(f"/api/jobs/{job['id']}/attempts").json()
    assert len(attempts) == 1 and attempts[0]["dispatch_state"] == "dispatch_started"
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 1
    response = client.post(f"/api/jobs/{job['id']}/remediations", json={
        "action":"retry", "strategy":"same-request", "reason":"重试"})
    assert response.status_code == 409


def test_legacy_repair_is_evidence_bound_and_idempotent(client, project_root, monkeypatch):
    message = "provider hostname resolves to a private or reserved address (198.18.0.68); enable private network access explicitly"
    def blocked(*a, **k):
        raise ProviderError(message, ErrorCategory.VALIDATION)
    monkeypatch.setattr(rm, "build_provider", blocked)
    plan, job = configured_job(client, project_root)
    wait_for_job(client, job["id"])
    with client.app.state.database.sessions() as session:
        attempt = session.scalar(select(GenerationAttempt).where(GenerationAttempt.job_id == job["id"]))
        old = session.get(GenerationPlan, plan["id"])
        attempt.dispatch_state = "legacy_unknown"
        attempt.billable = True
        attempt.estimated_cost = .5
        old.actual_calls = 1
        old.actual_cost = .5
        session.add(RunEvent(project_id=old.project_id, plan_id=old.id, job_id=job["id"],
                             attempt_id=attempt.id, event_type="provider.call_started", data_json={}))
        session.commit()
        assert reconcile_local_failures(session, old) == [attempt.id]
        assert reconcile_local_failures(session, old) == []
        assert old.actual_calls == 0 and old.actual_cost == 0
        assert attempt.dispatch_state == "not_sent"


async def test_json_and_multipart_use_same_network_guard_and_dispatch_order(monkeypatch):
    import base64
    dns(monkeypatch, ["198.18.0.68"])
    trace = []
    async def send(self, request, **kwargs):
        trace.append(("http", request.url.path, request.headers.get("content-type")))
        return httpx.Response(200, request=request,
                              json={"data":[{"b64_json":base64.b64encode(b"image").decode()}]})
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    profile = pm.ProviderRuntimeConfig(id="test", kind="openai_compatible",
        base_url="https://provider.example/v1", text_model="text", image_model="image",
        quality="standard", allow_private_network=False)
    provider = pm.OpenAICompatibleProvider(profile, None)
    provider.on_dispatch = lambda policy: trace.append(("dispatch", policy))
    provider.on_response = lambda: trace.append(("response",))
    for reference in (None, b"reference"):
        result = await provider.image(prompt="test", width=32, height=32, model="image", reference=reference)
        assert result.value == b"image"
    assert [item[0] for item in trace] == ["dispatch", "http", "response"] * 2
    assert trace[0][1] == trace[3][1] == "fake_ip_compatible"
    assert trace[1][1] == "/v1/images/generations"
    assert trace[4][1] == "/v1/images/edits"
    assert trace[4][2].startswith("multipart/form-data;")
    trace.clear()
    dns(monkeypatch, ["198.18.0.68", "10.0.0.8"])
    with pytest.raises(ProviderError):
        await provider.image(prompt="test", width=32, height=32, model="image", reference=b"ref")
    assert trace == []


def test_http_image_to_candidate_pipeline(client, project_root, monkeypatch):
    import base64
    import io
    from PIL import Image
    image = io.BytesIO()
    Image.new("RGBA", (64, 64), (200, 100, 50, 128)).save(image, format="PNG")
    calls = []
    dns(monkeypatch, ["198.18.0.68"])
    async def send(self, request, **kwargs):
        calls.append(str(request.url))
        return httpx.Response(200, request=request, headers={"x-request-id":"mock-image-1"},
            json={"data":[{"b64_json":base64.b64encode(image.getvalue()).decode()}]})
    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"], key="portrait.dispatch", kind="media", subtype="portrait")
    provider = create_fake_provider(client)
    def build(profile, vault):
        return pm.OpenAICompatibleProvider(pm.ProviderRuntimeConfig(id=profile.id,
            kind="openai_compatible", base_url="https://provider.example/v1", text_model="text",
            image_model="fake-image", quality="standard", allow_private_network=False), None)
    monkeypatch.setattr(rm, "build_provider", build)
    plan = client.post("/api/generation-plans", json={"project_id":project["id"],
        "provider_profile_id":provider["id"], "tasks":[{"id":"portrait", "kind":"image",
        "asset_id":asset["id"], "prompt":"portrait", "width":64, "height":64,
        "transparent":True, "target_path":"public/portrait.webp"}]}).json()
    job = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]
    completed = wait_for_job(client, job["id"])
    assert completed["status"] == "candidate_ready", completed
    inspected = client.get(f"/api/generation-plans/{plan['id']}/inspect").json()
    assert inspected["plan"]["actual_calls"] == 1
    assert inspected["jobs"][0]["blocking_reason"] is None
    assert inspected["jobs"][0]["result_revision_id"]
    assert inspected["attempts"][0]["network_policy"] == "fake_ip_compatible"
    assert inspected["evidence"]
    assert len(calls) == 1


def test_duplicate_recovery_is_idempotent_even_after_completion(client, project_root, monkeypatch):
    original = rm.build_provider
    monkeypatch.setattr(rm, "build_provider", lambda *a, **k: (_ for _ in ()).throw(
        ProviderError("local preparation failed", ErrorCategory.VALIDATION)))
    plan, job = configured_job(client, project_root)
    wait_for_job(client, job["id"])
    monkeypatch.setattr(rm, "build_provider", original)
    body = {"action":"retry", "strategy":"same-request", "reason":"continue", "idempotency_key":"fixed-operation"}
    first = client.post(f"/api/jobs/{job['id']}/remediations", json=body)
    assert first.status_code == 201
    assert wait_for_job(client, job["id"])["status"] == "candidate_ready"
    second = client.post(f"/api/jobs/{job['id']}/remediations", json=body)
    assert second.status_code == 201 and first.json()["id"] == second.json()["id"]
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 1
    changed = client.post(f"/api/jobs/{job['id']}/remediations", json={**body, "reason":"different"})
    assert changed.status_code == 409


def test_two_workers_claim_once_and_stale_lease_cannot_dispatch(client, project_root):
    from concurrent.futures import ThreadPoolExecutor
    runner = client.app.state.runner
    client.portal.call(runner.stop)
    plan, job = configured_job(client, project_root)
    other = rm.JobRunner(runner.sessions, runner.vault, runner.settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(worker._claim_ready_jobs, limit=1) for worker in (runner, other)]
        claims = [future.result() for future in futures]
    assert sum(len(claim) for claim in claims) == 1
    owner = runner if claims[0] else other
    job_id, token = (claims[0] or claims[1])[0]
    attempt_id, _, _ = owner._start_provider_attempt(job_id=job_id, lease_token=token,
        request={"kind":"text"}, request_hash="stale-test", idempotency_key="stale-key", purpose="base")
    with pytest.raises(ProviderError):
        owner._mark_dispatch(attempt_id, "stale-lease", "public_network")
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 0
    client.portal.call(owner._execute_claimed_job, job_id, token)
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "candidate_ready"
    assert client.get(f"/api/generation-plans/{plan['id']}").json()["actual_calls"] == 1
