from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from game_assets_api import storage as storage_module

from .conftest import create_asset, create_project


def test_project_asset_revision_review_release_flow(client: TestClient, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"])
    revision = client.post(
        "/api/revisions",
        json={"asset_id": asset["id"], "format": "json", "content": {"name": "姬宁"}},
    ).json()
    assert client.post(
        "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
    ).status_code == 201
    release = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "v0.1.0"}
    ).json()

    assert release["asset_count"] == 1
    manifest = json.loads((project_root / "release.json").read_text())
    assert manifest["format_version"] == 1
    assert manifest["assets"][0]["revision_id"] == revision["id"]
    assert (project_root / revision["file_path"]).is_file()
    assert list((project_root / "history/reviews").glob("*/*/*.json"))


def test_only_direct_root_project_contract_is_discovered(client: TestClient, tmp_path: Path) -> None:
    unsupported = tmp_path / "projects" / "unsupported"
    unsupported.mkdir(parents=True)
    (unsupported / "hidden-old-contract").mkdir()
    response = client.get("/api/projects")
    assert response.status_code == 200
    assert response.json() == []


def test_existing_project_uses_contract_id(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "projects" / "existing"
    root.mkdir(parents=True)
    (root / "project.yaml").write_text(
        "format_version: 1\nid: project_existing\nname: Existing\n"
    )
    response = client.get("/api/projects")
    assert response.status_code == 200, response.text
    assert response.json()[0]["id"] == "project_existing"


def test_imported_project_name_can_be_edited(client: TestClient, tmp_path: Path) -> None:
    root = tmp_path / "projects" / "existing"
    root.mkdir(parents=True)
    (root / "project.yaml").write_text(
        "format_version: 1\nid: project_existing\nname: Existing\n"
        "default_language: zh-CN\ncustom_setting: preserved\n"
    )
    assert client.get("/api/projects").status_code == 200

    response = client.patch(
        "/api/projects/project_existing", json={"name": "导入项目的新名称"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "导入项目的新名称"
    contract = yaml.safe_load((root / "project.yaml").read_text("utf-8"))
    assert contract["name"] == "导入项目的新名称"
    assert contract["id"] == "project_existing"
    assert contract["custom_setting"] == "preserved"
    assert client.get("/api/projects").json()[0]["name"] == "导入项目的新名称"


def test_collection_storage_has_no_per_asset_directories(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    first = create_asset(
        client, project["id"], key="content.event.first", kind="content", subtype="event"
    )
    second = create_asset(
        client, project["id"], key="content.event.second", kind="content", subtype="event"
    )
    collection = project_root / "catalog/content/events/general.json"
    payload = json.loads(collection.read_text())
    assert [item["key"] for item in payload["assets"]] == [first["key"], second["key"]]
    assert not any(path.is_dir() for path in (project_root / "catalog").rglob("content.event.*"))


def test_candidate_pointer_and_superseded_history_survive_rebuild(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(
        client, project["id"], key="content.event.test", kind="content", subtype="event"
    )
    first = client.post(
        "/api/revisions", json={"asset_id": asset["id"], "content": {"title": "第一版"}}
    ).json()
    second = client.post(
        "/api/revisions",
        json={
            "asset_id": asset["id"],
            "content": {"title": "第二版"},
            "parent_revision_id": first["id"],
        },
    ).json()
    assert client.post(f"/api/projects/{project['id']}/rebuild").status_code == 200
    revisions = client.get("/api/revisions", params={"asset_id": asset["id"]}).json()
    assert {row["id"]: row["review_status"] for row in revisions} == {
        second["id"]: "pending",
        first["id"]: "superseded",
    }


def test_relation_change_invalidates_dependent_approval(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    scene = create_asset(
        client, project["id"], key="content.scene.throne", kind="content", subtype="scene"
    )
    hero = create_asset(client, project["id"], key="entity.character.hero")
    assert client.post(
        "/api/relations",
        json={
            "project_id": project["id"],
            "source_asset_id": scene["id"],
            "target_asset_id": hero["id"],
            "relation_type": "depends_on",
        },
    ).status_code == 201
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


def test_release_preflight_is_fail_closed(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    healthy = create_asset(
        client,
        project["id"],
        key="content.scene.healthy",
        kind="content",
        subtype="scene",
    )
    stale = create_asset(
        client,
        project["id"],
        key="content.scene.stale",
        kind="content",
        subtype="scene",
    )
    dependency = create_asset(
        client,
        project["id"],
        key="entity.character.dependency",
    )
    for asset in (healthy, stale):
        revision = client.post(
            "/api/revisions",
            json={"asset_id": asset["id"], "content": {"title": asset["title"]}},
        ).json()
        assert client.post(
            "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
        ).status_code == 201
    assert client.post(
        "/api/relations",
        json={
            "project_id": project["id"],
            "source_asset_id": stale["id"],
            "target_asset_id": dependency["id"],
            "relation_type": "depends_on",
        },
    ).status_code == 201

    response = client.post(
        "/api/releases", json={"project_id": project["id"], "name": "must-fail"}
    )

    assert response.status_code == 409
    assert "content.scene.stale" in response.json()["detail"]
    assert not list((project_root / "releases").glob("*/manifest.json"))
    assert not (project_root / "release.json").exists()
    assert client.get(f"/api/assets/{healthy['id']}").json()["publication_status"] == "ready"
    assert client.get(f"/api/assets/{stale['id']}").json()["publication_status"] == "ready"


def test_release_file_failure_rolls_back_manifest_and_catalog(
    client: TestClient,
    project_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(client, project_root)
    asset = create_asset(client, project["id"])
    revision = client.post(
        "/api/revisions", json={"asset_id": asset["id"], "content": {"name": "stable"}}
    ).json()
    assert client.post(
        "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
    ).status_code == 201
    original_write = storage_module.atomic_write_json

    def fail_current_pointer(path: Path, content: object, *, immutable: bool = False) -> str:
        if path == project_root / "release.json":
            raise OSError("injected release pointer failure")
        return original_write(path, content, immutable=immutable)

    monkeypatch.setattr(storage_module, "atomic_write_json", fail_current_pointer)

    with pytest.raises(OSError, match="injected release pointer failure"):
        client.post(
            "/api/releases", json={"project_id": project["id"], "name": "broken-release"}
        )

    assert not list((project_root / "releases").glob("*/manifest.json"))
    assert not (project_root / "release.json").exists()
    collection = json.loads((project_root / "catalog/entities/characters.json").read_text())
    assert collection["assets"][0]["publication_status"] == "ready"


def test_security_headers_and_provider_key_are_not_persisted(
    client: TestClient, project_root: Path
) -> None:
    response = client.get("/api/health", headers={"Origin": "http://127.0.0.1:4173"})
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    provider = client.post(
        "/api/providers", json={"name": "Local", "base_url": "http://127.0.0.1:9999/v1"}
    ).json()
    secret = "sk-do-not-persist-this-secret"
    assert client.post(
        f"/api/providers/{provider['id']}/unlock", json={"api_key": secret}
    ).status_code == 200
    database_path = client.app.state.settings.database_url.removeprefix("sqlite:///")
    assert secret.encode() not in Path(database_path).read_bytes()
    assert secret not in "".join(
        path.read_text(errors="ignore") for path in project_root.rglob("*") if path.is_file()
    )
