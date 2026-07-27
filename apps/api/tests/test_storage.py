from __future__ import annotations

from pathlib import Path

import pytest

from game_assets_api.storage import (
    ImmutableRevisionError,
    ProjectStore,
    UnsafePathError,
    atomic_write_bytes,
    safe_join,
    sha256_bytes,
)


def test_project_contract_and_path_safety(project_root: Path) -> None:
    store = ProjectStore(project_root)
    store.initialize(project_id="project-1", name="测试项目")

    assert (project_root / "project.yaml").is_file()
    assert (project_root / "schemas" / "asset.schema.json").is_file()
    assert (project_root / "workspace" / ".gitignore").read_text() == "*\n!.gitignore\n"
    assert not (project_root / ".gitattributes").exists()
    assert not (project_root / "history" / "renditions").exists()
    with pytest.raises(UnsafePathError):
        safe_join(project_root, "../outside")
    with pytest.raises(UnsafePathError):
        safe_join(project_root, "/etc/passwd")


def test_immutable_atomic_write(project_root: Path) -> None:
    target = project_root / "immutable.json"
    atomic_write_bytes(target, b"first", immutable=True)
    with pytest.raises(ImmutableRevisionError):
        atomic_write_bytes(target, b"second", immutable=True)
    assert target.read_bytes() == b"first"


def test_atomic_publish_keeps_content_addressed_backup(project_root: Path) -> None:
    store = ProjectStore(project_root)
    store.initialize(project_id="project-1", name="Sample")
    target = project_root / "public" / "hero.webp"
    target.parent.mkdir()
    target.write_bytes(b"old")
    candidate = project_root / "candidate.webp"
    candidate.write_bytes(b"new")

    published, backup = store.atomic_publish(candidate, "public/hero.webp")

    assert published == "public/hero.webp"
    assert backup is not None
    assert backup.startswith("workspace/backups/")
    assert (project_root / backup).read_bytes() == b"old"
    assert target.read_bytes() == b"new"


def test_media_rendition_is_derived_from_revision(project_root: Path) -> None:
    store = ProjectStore(project_root)
    store.initialize(project_id="project-1", name="Sample")
    source = project_root / "production" / "sources" / "hero.png"
    preview = project_root / "approved" / "assets" / "hero.webp"
    source.parent.mkdir(parents=True, exist_ok=True)
    preview.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source")
    preview.write_bytes(b"preview")
    store.write_asset(
        {
            "id": "asset-1",
            "project_id": "project-1",
            "key": "portrait.hero.neutral",
            "kind": "media",
            "subtype": "portrait",
            "title": "Hero",
            "metadata": {},
            "current_revision_id": "revision-1",
        }
    )
    store.write_revision(
        kind="media",
        key="portrait.hero.neutral",
        revision={
            "id": "revision-1",
            "asset_id": "asset-1",
            "sequence": 1,
            "format": "media",
            "content": {
                "rendition": {
                    "media_type": "image/webp",
                    "source_path": "production/sources/hero.png",
                    "normalized_path": "approved/assets/hero.webp",
                    "target_path": "approved/assets/hero.webp",
                    "sha256": sha256_bytes(b"preview"),
                    "width": 256,
                    "height": 256,
                    "byte_size": 7,
                }
            },
            "content_hash": "content-hash",
            "parent_revision_id": None,
            "input_hash": "input-hash",
            "style_revision": None,
            "prompt_recipe": None,
            "provider_snapshot": {},
            "review_status": "approved",
            "created_at": None,
        },
    )

    assets, revisions, renditions, _qa, _relations, errors = store.scan()

    assert not errors
    assert len(assets) == len(revisions) == len(renditions) == 1
    assert renditions[0]["normalized_path"] == "approved/assets/hero.webp"
