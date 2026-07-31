from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from game_assets_api.main import create_app
from game_assets_api.settings import Settings

from .conftest import create_asset, create_project


def _approved_content(client: TestClient, project_id: str, *, key: str, title: str) -> dict:
    asset = create_asset(
        client,
        project_id,
        key=key,
        kind="content",
        subtype="scene",
        title=title,
    )
    revision = client.post(
        "/api/revisions",
        json={"asset_id": asset["id"], "format": "json", "content": {"title": title}},
    ).json()
    response = client.post(
        "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
    )
    assert response.status_code == 201, response.text
    return asset


def _v2_release(client: TestClient, project_id: str, name: str) -> dict:
    response = client.post(
        "/api/releases",
        json={"project_id": project_id, "name": name, "format_version": 2},
    )
    assert response.status_code == 201, response.text
    release = response.json()
    assert release["manifest_version"] == 2
    assert release["snapshot_hash"].startswith("sha256:")
    return release


def _bind_checkout(client: TestClient, project_id: str, game_root: Path) -> None:
    response = client.put(
        f"/api/projects/{project_id}/export-config", json={"game_root": str(game_root)}
    )
    assert response.status_code == 200, response.text


def test_release_v2_snapshot_preflight_and_delivery_idempotency(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    project = create_project(client, project_root)
    _approved_content(client, project["id"], key="content.delivery.scene", title="交付场景")
    release = _v2_release(client, project["id"], "delivery-v2")
    game_root = tmp_path / "game"
    game_root.mkdir()
    _bind_checkout(client, project["id"], game_root)

    manifest = json.loads((project_root / release["manifest_path"]).read_text("utf-8"))
    assert manifest["snapshot_hash"] == release["snapshot_hash"]
    preflight = client.get(
        f"/api/releases/{release['id']}/preflight", params={"project_id": project["id"]}
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["blocking"] is False
    assert preflight.json()["snapshot_hash"] == release["snapshot_hash"]

    preview = client.post(
        "/api/exports/preview",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["no_op"] is False
    applied = client.post(
        "/api/exports/apply",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert applied.status_code == 201, applied.text
    assert applied.json()["status"] == "succeeded"
    assert (game_root / "gams-lock.json").is_file()

    repeated_preview = client.post(
        "/api/exports/preview",
        json={"project_id": project["id"], "release_id": release["id"]},
    ).json()
    assert repeated_preview["no_op"] is True
    repeated = client.post(
        "/api/exports/apply",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert repeated.status_code == 201, repeated.text
    assert repeated.json()["status"] == "no_op"
    verified = client.post(
        "/api/exports/verify",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["ok"] is True


def test_delivery_protects_unknown_and_manually_modified_files(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    project = create_project(client, project_root)
    _approved_content(client, project["id"], key="content.delivery.protected", title="受管内容")
    release = _v2_release(client, project["id"], "protected-v2")
    game_root = tmp_path / "game"
    game_root.mkdir()
    _bind_checkout(client, project["id"], game_root)
    assert client.post(
        "/api/exports/apply", json={"project_id": project["id"], "release_id": release["id"]}
    ).status_code == 201

    unknown = game_root / "keep-me.txt"
    unknown.write_text("由游戏仓库管理\n", encoding="utf-8")
    managed = game_root / "src/generated/content/registry.ts"
    managed.write_text("// 人工修改\n", encoding="utf-8")
    preview = client.post(
        "/api/exports/preview",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert preview.status_code == 200
    assert preview.json()["blocking"] is True
    assert any("modified outside GAMS" in issue for issue in preview.json()["issues"])
    failed = client.post(
        "/api/exports/apply",
        json={"project_id": project["id"], "release_id": release["id"]},
    )
    assert failed.status_code == 409, failed.text
    assert managed.read_text(encoding="utf-8") == "// 人工修改\n"
    assert unknown.read_text(encoding="utf-8") == "由游戏仓库管理\n"


def test_validation_failure_restores_previous_checkout_and_lock(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    project = create_project(client, project_root)
    _approved_content(client, project["id"], key="content.delivery.rollback", title="第一版")
    first = _v2_release(client, project["id"], "rollback-first")
    game_root = tmp_path / "game"
    game_root.mkdir()
    _bind_checkout(client, project["id"], game_root)
    assert client.post(
        "/api/exports/apply", json={"project_id": project["id"], "release_id": first["id"]}
    ).status_code == 201
    old_registry = (game_root / "src/generated/content/registry.ts").read_bytes()
    old_lock = (game_root / "gams-lock.json").read_bytes()

    # A later approved revision creates a different Release.  The configured
    # command intentionally fails after files have been replaced in staging.
    asset = client.get("/api/assets", params={"project_id": project["id"]}).json()[0]
    revision = client.post(
        "/api/revisions",
        json={
            "asset_id": asset["id"],
            "format": "json",
            "content": {"title": "第二版"},
        },
    ).json()
    assert client.post(
        "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
    ).status_code == 201
    second = _v2_release(client, project["id"], "rollback-second")
    project_yaml = project_root / "project.yaml"
    contract = yaml.safe_load(project_yaml.read_text("utf-8"))
    contract["export"]["validation_commands"] = [
        {"label": "故意失败", "argv": [sys.executable, "-c", "raise SystemExit(7)"]}
    ]
    project_yaml.write_text(yaml.safe_dump(contract, allow_unicode=True, sort_keys=False), encoding="utf-8")

    failed = client.post(
        "/api/exports/apply", json={"project_id": project["id"], "release_id": second["id"]}
    )
    assert failed.status_code == 409, failed.text
    assert (game_root / "src/generated/content/registry.ts").read_bytes() == old_registry
    assert (game_root / "gams-lock.json").read_bytes() == old_lock
    assert client.get("/api/deliveries", params={"project_id": project["id"]}).json()[0]["status"] == "failed"


def test_rollback_re_delivers_old_release_and_receipt_index_rebuilds(
    tmp_path: Path,
) -> None:
    settings = Settings(
        projects_root=tmp_path / "projects",
        state_dir=tmp_path / "state",
        database_url=f"sqlite:///{tmp_path / 'test.sqlite3'}",
        frontend_dist=None,
        job_poll_interval=0.01,
    )
    settings.projects_root.mkdir(parents=True)
    game_root = tmp_path / "game"
    game_root.mkdir()
    with TestClient(create_app(settings)) as client:
        project = create_project(client, settings.projects_root / "sample-game")
        asset = _approved_content(client, project["id"], key="content.delivery.history", title="初版")
        first = _v2_release(client, project["id"], "history-first")
        _bind_checkout(client, project["id"], game_root)
        assert client.post(
            "/api/exports/apply", json={"project_id": project["id"], "release_id": first["id"]}
        ).status_code == 201
        revision = client.post(
            "/api/revisions",
            json={"asset_id": asset["id"], "format": "json", "content": {"title": "后版"}},
        ).json()
        assert client.post(
            "/api/reviews", json={"revision_id": revision["id"], "verdict": "approve"}
        ).status_code == 201
        second = _v2_release(client, project["id"], "history-second")
        assert client.post(
            "/api/exports/apply", json={"project_id": project["id"], "release_id": second["id"]}
        ).status_code == 201
        rolled_back = client.post(
            "/api/exports/rollback",
            json={"project_id": project["id"], "release_id": first["id"]},
        )
        assert rolled_back.status_code == 201, rolled_back.text
        assert rolled_back.json()["status"] == "rolled_back"
        assert json.loads((game_root / "gams-lock.json").read_text())["release_id"] == first["id"]
        deliveries = client.get("/api/deliveries", params={"project_id": project["id"]}).json()
        assert len(deliveries) == 3

    # Remove only SQLite: the Project history is the source of truth and the
    # startup scan must recreate the Delivery index from its receipts.
    db_path = settings.state_dir / "test.sqlite3"
    # Settings.database_url is absolute and points at tmp_path/test.sqlite3.
    db_path = tmp_path / "test.sqlite3"
    assert sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 3
    db_path.unlink()
    with TestClient(create_app(settings)) as rebuilt:
        project = rebuilt.get("/api/projects").json()[0]
        deliveries = rebuilt.get("/api/deliveries", params={"project_id": project["id"]})
        assert deliveries.status_code == 200
        assert len(deliveries.json()) == 3


def test_delivery_preflight_returns_all_detected_issues(
    client: TestClient, project_root: Path, tmp_path: Path
) -> None:
    project = create_project(client, project_root)
    _approved_content(client, project["id"], key="content.delivery.issues", title="完整问题")
    release = _v2_release(client, project["id"], "issues-v2")
    game_root = tmp_path / "game"
    game_root.mkdir()
    _bind_checkout(client, project["id"], game_root)
    manifest_path = project_root / release["manifest_path"]
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["assets"].append(
        {
            "key": "media.missing",
            "kind": "media",
            "subtype": "portrait",
            "revision_id": "missing",
            "content_hash": "missing",
            "dependency_hash": "missing",
            "content": {},
            "renditions": [
                {"id": "r-missing", "path": "approved/objects/missing.webp", "sha256": "sha256:bad"}
            ],
        }
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    response = client.get(
        f"/api/releases/{release['id']}/preflight", params={"project_id": project["id"]}
    )
    assert response.status_code == 200, response.text
    issues = response.json()["issues"]
    assert response.json()["blocking"] is True
    assert any("manifest hash" in issue for issue in issues)
    assert any("snapshot hash" in issue for issue in issues)
    assert any("runtime blob is missing" in issue for issue in issues)
