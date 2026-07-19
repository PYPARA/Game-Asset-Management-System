from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from .conftest import create_asset, create_project


def test_project_asset_revision_review_release_flow(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"])
    revision_response = client.post(
        "/api/revisions",
        json={
            "asset_id": asset["id"],
            "format": "json",
            "content": {"name": "姬宁", "role": "emperor"},
        },
    )
    assert revision_response.status_code == 201, revision_response.text
    revision = revision_response.json()

    review_response = client.post(
        "/api/reviews",
        json={"revision_id": revision["id"], "verdict": "approve", "notes": "通过"},
    )
    assert review_response.status_code == 201, review_response.text

    release_response = client.post(
        "/api/releases",
        json={"project_id": project["id"], "name": "v0.1.0"},
    )
    assert release_response.status_code == 201, release_response.text
    release = release_response.json()
    assert release["asset_count"] == 1
    manifest = json.loads((project_root / ".game-assets" / "runtime-manifest.json").read_text())
    assert manifest["assets"][0]["key"] == "character.hero"
    assert manifest["assets"][0]["revisionId"] == revision["id"]

    revision_file = project_root / revision["file_path"]
    assert revision_file.is_file()
    assert list((project_root / ".game-assets" / "assets" / "entity" / "character.hero" / "reviews").glob("*.json"))


def test_relation_change_invalidates_dependent_approval(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    scene = create_asset(client, project["id"], key="scene.throne", kind="content", subtype="scene")
    hero = create_asset(client, project["id"], key="character.hero")
    relation = client.post(
        "/api/relations",
        json={
            "project_id": project["id"],
            "source_asset_id": scene["id"],
            "target_asset_id": hero["id"],
            "relation_type": "depends_on",
        },
    )
    assert relation.status_code == 201

    scene_revision = client.post(
        "/api/revisions", json={"asset_id": scene["id"], "content": {"text": "朝会"}}
    ).json()
    assert client.post(
        "/api/reviews", json={"revision_id": scene_revision["id"], "verdict": "approve"}
    ).status_code == 201
    hero_revision = client.post(
        "/api/revisions", json={"asset_id": hero["id"], "content": {"name": "新皇帝"}}
    ).json()
    assert client.post(
        "/api/reviews", json={"revision_id": hero_revision["id"], "verdict": "approve"}
    ).status_code == 201

    reviews = client.get("/api/reviews", params={"asset_id": scene["id"]}).json()
    assert reviews[0]["is_valid"] is False
    release = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "blocked-dependency"}
    ).json()
    assert release["asset_count"] == 1


def test_security_headers_cors_and_provider_key_not_persisted(client: TestClient, project_root: Path) -> None:
    response = client.get("/api/health", headers={"Origin": "http://127.0.0.1:4173"})
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    provider = client.post(
        "/api/providers",
        json={"name": "Local", "base_url": "http://127.0.0.1:9999/v1"},
    ).json()
    secret = "sk-do-not-persist-this-secret"
    assert client.post(
        f"/api/providers/{provider['id']}/unlock", json={"api_key": secret}
    ).status_code == 200
    assert client.get(f"/api/providers/{provider['id']}").json()["is_unlocked"] is True
    database_path = client.app.state.settings.database_url.removeprefix("sqlite:///")
    assert secret.encode() not in Path(database_path).read_bytes()
    assert secret not in "".join(path.read_text(errors="ignore") for path in project_root.rglob("*") if path.is_file())


def test_private_http_provider_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/providers",
        json={"name": "Unsafe", "base_url": "http://192.168.1.2:8080/v1"},
    )
    assert response.status_code == 422


def test_current_revision_is_approved_pointer_and_candidates_are_separate(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"])
    first = client.post(
        "/api/revisions", json={"asset_id": asset["id"], "content": {"version": 1}}
    ).json()
    view = client.get(f"/api/assets/{asset['id']}").json()
    assert view["current_revision_id"] is None
    assert view["latest_candidate_revision_id"] == first["id"]
    assert client.get(f"/api/assets/{asset['id']}/candidates").json()[0]["id"] == first["id"]

    client.post("/api/reviews", json={"revision_id": first["id"], "verdict": "approve"})
    second = client.post(
        "/api/revisions", json={"asset_id": asset["id"], "content": {"version": 2}}
    ).json()
    view = client.get(f"/api/assets/{asset['id']}").json()
    assert view["current_revision_id"] == first["id"]
    assert view["latest_candidate_revision_id"] == second["id"]


def test_emperor_import_defaults_to_preview_and_requires_valid_source(client: TestClient) -> None:
    response = client.post(
        "/api/importers/emperor/import",
        json={"source_root": "/definitely/missing/emperor", "destination_path": None},
    )
    assert response.status_code == 422
