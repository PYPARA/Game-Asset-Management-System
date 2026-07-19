from __future__ import annotations

from pathlib import Path

import pytest

from game_assets_api.storage import (
    ImmutableRevisionError,
    ProjectStore,
    UnsafePathError,
    atomic_write_bytes,
    safe_join,
)


def test_project_contract_and_path_safety(project_root: Path) -> None:
    store = ProjectStore(project_root)
    store.initialize(project_id="project-1", name="测试项目")

    assert (project_root / ".game-assets" / "project.yaml").is_file()
    assert (project_root / ".game-assets" / "schemas" / "asset-extension.schema.json").is_file()
    assert (project_root / "output" / "game-assets" / ".gitignore").read_text() == "*\n!.gitignore\n"
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
    assert (project_root / backup).read_bytes() == b"old"
    assert target.read_bytes() == b"new"

