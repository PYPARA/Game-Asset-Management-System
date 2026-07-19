from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import create_asset, create_fake_provider, create_project


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "cancelled", "credentials_locked"}:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def test_structured_text_job_creates_schema_valid_candidate(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"], key="item.jade-seal", subtype="item", title="玉玺")
    provider = create_fake_provider(client)
    plan_response = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "write-item",
                    "kind": "text",
                    "asset_id": asset["id"],
                    "prompt": "生成玉玺定义",
                    "schema": {
                        "type": "object",
                        "required": ["name", "rarity"],
                        "properties": {
                            "name": {"type": "string"},
                            "rarity": {"enum": ["legendary"]},
                        },
                        "additionalProperties": False,
                    },
                }
            ],
        },
    )
    assert plan_response.status_code == 201, plan_response.text
    jobs = client.post(f"/api/generation-plans/{plan_response.json()['id']}/confirm").json()
    job = wait_for_job(client, jobs[0]["id"])
    assert job["status"] == "succeeded", job
    revision = client.get(f"/api/revisions/{job['result_revision_id']}").json()
    assert revision["content"] == {"name": "generated-name", "rarity": "legendary"}
    assert revision["provider_snapshot"]["requestId"] == "fake-text-request"


def test_image_job_runs_normalization_qa_review_and_atomic_publish(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.hero",
        kind="media",
        subtype="portrait",
        title="主角立绘",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw-hero",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "皇帝立绘",
                    "width": 128,
                    "height": 128,
                    "transparent": True,
                    "target_path": "public/assets/hero.webp",
                }
            ],
        },
    ).json()
    jobs = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()
    job = wait_for_job(client, jobs[0]["id"])
    assert job["status"] == "succeeded", job
    revision_id = job["result_revision_id"]
    renditions = client.get(f"/api/revisions/{revision_id}/renditions").json()
    assert len(renditions) == 1
    content = client.get(f"/api/renditions/{renditions[0]['id']}/content")
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/webp"
    assert content.content
    qa_runs = client.get("/api/qa-runs", params={"rendition_id": renditions[0]["id"]}).json()
    assert qa_runs[0]["verdict"] in {"pass", "warning"}
    approval = client.post(
        "/api/reviews", json={"revision_id": revision_id, "verdict": "approve"}
    )
    assert approval.status_code == 201, approval.text
    release = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "image-release"}
    )
    assert release.status_code == 201, release.text
    assert (project_root / "public" / "assets" / "hero.webp").is_file()


def test_dependency_dag_rejects_cycles(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"])
    provider = create_fake_provider(client)
    response = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {"id": "a", "kind": "image", "asset_id": asset["id"], "prompt": "a", "depends_on": ["b"]},
                {"id": "b", "kind": "image", "asset_id": asset["id"], "prompt": "b", "depends_on": ["a"]},
            ],
        },
    )
    assert response.status_code == 422
