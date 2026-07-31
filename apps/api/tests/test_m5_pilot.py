from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from game_assets_api.models import Asset, AssetRevision, Rendition, ReviewDecision, new_id
from game_assets_api.storage import ProjectStore, sha256_file, stable_id

from .conftest import create_asset, create_project


def _legacy_media_fixture(client, project_root: Path, project_id: str) -> tuple[dict, dict]:
    asset = create_asset(
        client,
        project_id,
        key="icon.m5-legacy",
        kind="media",
        subtype="icon",
        title="M5 legacy icon",
    )
    source = project_root / "production/sources/icons/m5-legacy.png"
    runtime = project_root / "approved/assets/icons/m5-legacy.webp"
    source.parent.mkdir(parents=True, exist_ok=True)
    runtime.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (32, 32), (176, 144, 88, 255)).save(source, format="PNG")
    Image.open(source).save(runtime, format="WEBP")
    content = {
        "key": asset["key"],
        "title": asset["title"],
        "rendition": {
            "media_type": "image/webp",
            "source_path": "production/sources/icons/m5-legacy.png",
            "normalized_path": "approved/assets/icons/m5-legacy.webp",
            "target_path": "approved/assets/icons/m5-legacy.webp",
            "sha256": sha256_file(runtime),
            "width": 32,
            "height": 32,
            "byte_size": runtime.stat().st_size,
        },
    }
    revision = client.post(
        "/api/revisions",
        json={"asset_id": asset["id"], "format": "media", "content": content},
    ).json()
    now = datetime.now(UTC)
    with client.app.state.database.sessions() as session:
        db_asset = session.get(Asset, asset["id"])
        db_revision = session.get(AssetRevision, revision["id"])
        assert db_asset is not None and db_revision is not None
        db_asset.content_status = "approved"
        db_asset.publication_status = "ready"
        db_asset.current_revision_id = db_revision.id
        db_revision.review_status = "approved"
        session.add(
            Rendition(
                id=stable_id("rendition", db_revision.id, sha256_file(runtime)),
                revision_id=db_revision.id,
                media_type="image/webp",
                source_path="production/sources/icons/m5-legacy.png",
                normalized_path="approved/assets/icons/m5-legacy.webp",
                target_path="approved/assets/icons/m5-legacy.webp",
                sha256=sha256_file(runtime),
                width=32,
                height=32,
                byte_size=runtime.stat().st_size,
                created_at=now,
            )
        )
        session.add(
            ReviewDecision(
                id=f"review_legacy_{new_id()}",
                revision_id=db_revision.id,
                asset_id=db_asset.id,
                verdict="approve",
                notes="legacy import",
                dependency_hash="legacy",
                is_valid=False,
                created_at=now,
            )
        )
        session.commit()
    ProjectStore(project_root).update_asset_approval(
        kind="media",
        key=asset["key"],
        title=asset["title"],
        content_status="approved",
        publication_status="ready",
        current_revision_id=revision["id"],
        latest_candidate_revision_id=None,
        updated_at=now.isoformat(),
    )
    return asset, revision


def test_m5_legacy_promotion_is_durable_and_idempotent(client, project_root: Path) -> None:
    project = create_project(client, project_root)
    asset, legacy_revision = _legacy_media_fixture(client, project_root, project["id"])

    first = client.post(
        f"/api/projects/{project['id']}/legacy-media-promotions",
        json={"asset_keys": [asset["key"]]},
    )
    assert first.status_code == 200, first.text
    migrated = first.json()["migrated"]
    assert len(migrated) == 1
    assert migrated[0]["source_revision_id"] == legacy_revision["id"]

    indexed = client.post(f"/api/projects/{project['id']}/scan")
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["errors"] == []
    current = client.get("/api/assets", params={"project_id": project["id"]}).json()[0]
    revisions = client.get("/api/revisions", params={"asset_id": asset["id"]}).json()
    promoted = next(item for item in revisions if item["id"] == current["current_revision_id"])
    assert promoted["content"]["rendition"]["target_path"] == "public/assets/icons/m5-legacy.webp"
    assert promoted["content"]["rendition"]["source_artifact_id"].startswith("artifact_")
    assert promoted["content"]["rendition"]["artifact_id"].startswith("artifact_")
    assert len(client.get("/api/artifacts", params={"revision_id": promoted["id"]}).json()) == 2

    second = client.post(
        f"/api/projects/{project['id']}/legacy-media-promotions",
        json={"asset_keys": [asset["key"]]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["migrated"] == []
    assert second.json()["skipped"][0]["reason"] == "already_promoted"

    release = client.post(
        "/api/releases",
        json={
            "project_id": project["id"],
            "name": "m5-subset",
            "format_version": 2,
            "asset_keys": [asset["key"]],
        },
    )
    assert release.status_code == 201, release.text
    preflight = client.get(
        f"/api/releases/{release.json()['id']}/preflight",
        params={"project_id": project["id"]},
    )
    assert preflight.status_code == 200, preflight.text
    assert preflight.json()["blocking"] is False
    assert len(preflight.json()["assets"]) == 1
