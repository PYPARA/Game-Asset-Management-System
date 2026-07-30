from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from game_assets_api import storage as storage_module
from game_assets_api.main import create_app
from game_assets_api.settings import Settings

from .conftest import create_asset, create_fake_provider, create_project


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in {
            "candidate_ready",
            "awaiting_user",
            "succeeded",
            "qa_failed",
            "failed",
            "cancelled",
            "credentials_locked",
        }:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


def generate_and_approve_image(
    client: TestClient,
    *,
    project_id: str,
    provider_id: str,
    asset_id: str,
    task_id: str,
    target_path: str,
    width: int = 64,
    height: int = 64,
) -> dict:
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project_id,
            "provider_profile_id": provider_id,
            "tasks": [
                {
                    "id": task_id,
                    "kind": "image",
                    "asset_id": asset_id,
                    "prompt": task_id,
                    "width": width,
                    "height": height,
                    "target_path": target_path,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    job = wait_for_job(client, job_id)
    assert job["status"] == "candidate_ready", job
    approval = client.post(
        "/api/reviews",
        json={"revision_id": job["result_revision_id"], "verdict": "approve"},
    )
    assert approval.status_code == 201, approval.text
    return approval.json()


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
    assert job["status"] == "candidate_ready", job
    revision = client.get(f"/api/revisions/{job['result_revision_id']}").json()
    assert revision["content"] == {"name": "generated-name", "rarity": "legendary"}
    assert revision["provider_snapshot"]["request_id"] == "fake-text-request"


def test_image_approval_promotes_durable_artifacts_and_rebuilds_without_workspace(
    client: TestClient, project_root: Path
) -> None:
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
    assert job["status"] == "candidate_ready", job
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
    approval_data = approval.json()
    approved_revision_id = approval_data["revision_id"]
    assert approved_revision_id != revision_id
    assert {item["role"] for item in approval_data["artifacts"]} == {"source", "runtime"}

    approved_revision = client.get(f"/api/revisions/{approved_revision_id}").json()
    approved_rendition = approved_revision["content"]["rendition"]
    assert approved_rendition["source_path"].startswith("production/sources/")
    assert approved_rendition["normalized_path"].startswith("approved/objects/")
    assert not any(
        str(value).startswith("workspace/")
        for value in (
            approved_rendition["source_path"],
            approved_rendition["normalized_path"],
        )
    )
    artifacts = client.get(
        "/api/artifacts", params={"revision_id": approved_revision_id}
    ).json()
    assert len(artifacts) == 2
    assert all((project_root / artifact["path"]).is_file() for artifact in artifacts)

    shutil.rmtree(project_root / "workspace")
    rebuild = client.post(f"/api/projects/{project['id']}/rebuild")
    assert rebuild.status_code == 200, rebuild.text
    assert rebuild.json()["errors"] == []
    promoted_renditions = client.get(
        f"/api/revisions/{approved_revision_id}/renditions"
    ).json()
    assert len(promoted_renditions) == 1
    promoted_qa = client.get(
        "/api/qa-runs", params={"rendition_id": promoted_renditions[0]["id"]}
    ).json()[0]
    assert client.get(
        f"/api/renditions/{promoted_renditions[0]['id']}/content"
    ).status_code == 200

    release = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "image-release"}
    )
    assert release.status_code == 201, release.text
    manifest = json.loads((project_root / release.json()["manifest_path"]).read_text("utf-8"))
    assert manifest["assets"][0]["revision_id"] == approved_revision_id
    assert manifest["assets"][0]["renditions"][0]["path"].startswith(
        "approved/objects/"
    )
    assert not (project_root / "public" / "assets" / "hero.webp").exists()

    rebuilt_settings = Settings(
        projects_root=project_root.parent,
        state_dir=project_root.parent / "rebuilt-state",
        database_url=f"sqlite:///{project_root.parent / 'rebuilt.sqlite3'}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    with TestClient(create_app(rebuilt_settings)) as rebuilt_client:
        rebuilt_projects = rebuilt_client.get("/api/projects").json()
        assert [item["id"] for item in rebuilt_projects] == [project["id"]]
        rebuilt_releases = rebuilt_client.get(
            "/api/releases", params={"project_id": project["id"]}
        ).json()
        assert [item["id"] for item in rebuilt_releases] == [release.json()["id"]]
        rebuilt_artifacts = rebuilt_client.get(
            "/api/artifacts", params={"revision_id": approved_revision_id}
        ).json()
        assert {item["id"] for item in rebuilt_artifacts} == {
            item["id"] for item in artifacts
        }
        rebuilt_qa = rebuilt_client.get(
            "/api/qa-runs", params={"rendition_id": promoted_renditions[0]["id"]}
        ).json()
        assert rebuilt_qa[0]["id"] == promoted_qa["id"]
        assert rebuilt_qa[0]["created_at"] == promoted_qa["created_at"]
        rebuilt_reviews = rebuilt_client.get(
            "/api/reviews", params={"asset_id": asset["id"]}
        ).json()
        assert rebuilt_reviews[0]["revision_id"] == approved_revision_id
        assert rebuilt_reviews[0]["is_valid"] is True


def test_hard_qa_failure_is_not_reported_as_job_success(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "game_assets_api.services.inspect_image",
        lambda *_args, **_kwargs: (
            "fail",
            [{"name": "width", "passed": False, "value": 64, "expected": 128}],
        ),
    )
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.qa-failure",
        kind="media",
        subtype="portrait",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw-invalid",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "hard QA fail",
                    "width": 128,
                    "height": 128,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]

    job = wait_for_job(client, job_id)

    assert job["status"] == "awaiting_user"
    assert job["stage"] == "hard_qa"
    assert job["result_revision_id"]
    assert job["error_category"] == "validation"
    assert client.get(f"/api/assets/{asset['id']}").json()["generation_status"] == "awaiting_user"
    attempts = client.get(f"/api/jobs/{job_id}/attempts").json()
    assert attempts[0]["status"] == "succeeded"


def test_media_promotion_file_failure_keeps_formal_state_unchanged(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.transaction",
        kind="media",
        subtype="portrait",
    )
    provider = create_fake_provider(client)
    plan = client.post(
        "/api/generation-plans",
        json={
            "project_id": project["id"],
            "provider_profile_id": provider["id"],
            "tasks": [
                {
                    "id": "draw-transaction",
                    "kind": "image",
                    "asset_id": asset["id"],
                    "prompt": "transaction",
                    "width": 64,
                    "height": 64,
                }
            ],
        },
    ).json()
    job_id = client.post(f"/api/generation-plans/{plan['id']}/confirm").json()[0]["id"]
    candidate_revision_id = wait_for_job(client, job_id)["result_revision_id"]
    objects_before = set((project_root / "history/objects").glob("*/*.json"))
    reviews_before = set((project_root / "history/reviews").glob("*/*/*.json"))
    original_write = storage_module.atomic_write_json

    def fail_catalog_write(path: Path, content: object, *, immutable: bool = False) -> str:
        if project_root / "catalog" in path.parents and path.name == "portraits.json":
            raise OSError("injected catalog failure")
        return original_write(path, content, immutable=immutable)

    monkeypatch.setattr(storage_module, "atomic_write_json", fail_catalog_write)

    with pytest.raises(OSError, match="injected catalog failure"):
        client.post(
            "/api/reviews",
            json={"revision_id": candidate_revision_id, "verdict": "approve"},
        )

    assert set((project_root / "history/objects").glob("*/*.json")) == objects_before
    assert set((project_root / "history/reviews").glob("*/*/*.json")) == reviews_before
    descriptor = json.loads((project_root / "catalog/media/portraits.json").read_text())[
        "assets"
    ][0]
    assert descriptor["current_revision_id"] is None
    assert descriptor["latest_candidate_revision_id"] == candidate_revision_id
    persisted_asset = client.get(f"/api/assets/{asset['id']}").json()
    assert persisted_asset["current_revision_id"] is None
    assert client.get(f"/api/revisions/{candidate_revision_id}").json()[
        "review_status"
    ] == "pending"


def test_release_rejects_missing_or_corrupt_blobs_and_failed_hard_qa(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client,
        project["id"],
        key="portrait.release-preflight",
        kind="media",
        subtype="portrait",
    )
    provider = create_fake_provider(client)
    approval = generate_and_approve_image(
        client,
        project_id=project["id"],
        provider_id=provider["id"],
        asset_id=asset["id"],
        task_id="draw-preflight",
        target_path="public/assets/preflight.webp",
    )
    rendition = client.get(
        f"/api/revisions/{approval['revision_id']}/renditions"
    ).json()[0]
    runtime_path = project_root / rendition["normalized_path"]
    runtime_bytes = runtime_path.read_bytes()
    runtime_path.unlink()

    missing_blob = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "missing-blob"}
    )

    assert missing_blob.status_code == 409
    assert "runtime blob is missing" in missing_blob.json()["detail"]
    assert not list((project_root / "releases").glob("*/manifest.json"))

    runtime_path.write_bytes(runtime_bytes)
    approved_revision = client.get(f"/api/revisions/{approval['revision_id']}").json()
    source_path = project_root / approved_revision["content"]["rendition"]["source_path"]
    source_bytes = source_path.read_bytes()
    source_path.write_bytes(source_bytes + b"corrupt")

    corrupt_source = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "corrupt-source"}
    )

    assert corrupt_source.status_code == 409
    assert "source artifact blob hash or byte size is invalid" in corrupt_source.json()[
        "detail"
    ]
    assert not list((project_root / "releases").glob("*/manifest.json"))

    source_path.write_bytes(source_bytes)
    failed_qa = client.post(
        "/api/qa-runs",
        json={"rendition_id": rendition["id"], "expected_width": 65},
    )
    assert failed_qa.status_code == 201
    assert failed_qa.json()["verdict"] == "fail"

    qa_blocked = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "failed-qa"}
    )

    assert qa_blocked.status_code == 409
    assert "latest hard QA does not pass" in qa_blocked.json()["detail"]
    assert not list((project_root / "releases").glob("*/manifest.json"))


def test_release_rejects_case_insensitive_target_collision(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    provider = create_fake_provider(client)
    first = create_asset(
        client,
        project["id"],
        key="portrait.collision-first",
        kind="media",
        subtype="portrait",
    )
    second = create_asset(
        client,
        project["id"],
        key="portrait.collision-second",
        kind="media",
        subtype="portrait",
    )
    generate_and_approve_image(
        client,
        project_id=project["id"],
        provider_id=provider["id"],
        asset_id=first["id"],
        task_id="draw-collision-first",
        target_path="public/assets/Hero.webp",
    )
    generate_and_approve_image(
        client,
        project_id=project["id"],
        provider_id=provider["id"],
        asset_id=second["id"],
        task_id="draw-collision-second",
        target_path="public/assets/hero.webp",
    )

    response = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "collision"}
    )

    assert response.status_code == 409
    assert "target path collides" in response.json()["detail"]
    assert not list((project_root / "releases").glob("*/manifest.json"))


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
