from __future__ import annotations

import json
from pathlib import Path

import pytest

from emperor_adapter import PathSafetyError, dry_run, export_manifest, import_to


EXPECTED_TYPES = {
    "portrait": 92,
    "background": 20,
    "cg": 16,
    "icon": 50,
    "ending": 8,
}


def test_dry_run_reports_verified_contract_without_credentials(legacy_fixture: Path) -> None:
    preview = dry_run(legacy_fixture)

    assert preview["anchors"]["count"] == 3
    assert preview["runtime_assets"]["count"] == 186
    assert preview["runtime_assets"]["by_type"] == EXPECTED_TYPES
    assert preview["qa"]["asset_count"] == 186
    assert preview["qa"]["errors"] == 0
    assert preview["qa"]["warnings"] == 0
    assert preview["content"]["parseable_record_count"] == 882
    assert preview["missing_media"] == []
    serialized = json.dumps(preview)
    assert "must-not-be-read" not in serialized
    assert "image-gen-key.json" not in serialized


def test_import_is_idempotent_and_never_copies_media(
    legacy_fixture: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "managed-project"
    first = import_to(legacy_fixture, destination)
    second = import_to(legacy_fixture, destination)
    metadata = destination / ".game-assets"

    assert first["runtime_assets"] == 186
    assert first["anchors"] == 3
    assert first["media_copied"] == 0
    assert first["written_files"] > 0
    assert second["written_files"] == 0
    assert second["unchanged_files"] == first["written_files"]
    assert len(list((metadata / "assets/media").glob("*/asset.yaml"))) == 186
    assert not list(metadata.rglob("*.webp"))
    assert not list(metadata.rglob("*.png"))

    example = json.loads(
        (metadata / "assets/media/portrait.person-01.neutral/asset.yaml").read_text()
    )
    assert example["legacy"]["key"] == "portrait.person-01.neutral"
    assert example["legacy"]["path"].endswith("neutral.webp")
    assert example["revisions"][0]["provider"] == "unknown"
    assert example["renditions"][0]["sha256"]
    assert example["relations"] == [
        {"target": "entity.character.person-01", "type": "depicts"}
    ]


def test_export_is_stably_sorted_and_supports_typescript(
    legacy_fixture: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "managed-project"
    import_to(legacy_fixture, destination)

    first = export_manifest(destination)
    second = export_manifest(destination)
    entries = json.loads(first)
    assert first == second
    assert len(entries) == 186
    assert [item["key"] for item in entries] == sorted(item["key"] for item in entries)
    assert entries[0]["path"].startswith("public/assets/")

    typescript = export_manifest(destination, format="typescript")
    assert "export const assetManifest" in typescript
    assert "as const" in typescript
    assert "portrait.person-01.neutral" in typescript


def test_metadata_paths_cannot_escape_legacy_root(legacy_fixture: Path) -> None:
    ledger = legacy_fixture / "docs/assets/asset-production.md"
    ledger.write_text(
        ledger.read_text().replace(
            "- Source: art-source/portraits/core/person-01/neutral.png",
            "- Source: ../image-gen-key.json",
            1,
        )
    )
    with pytest.raises(PathSafetyError):
        dry_run(legacy_fixture)


def test_import_refuses_to_write_inside_legacy_checkout(legacy_fixture: Path) -> None:
    with pytest.raises(PathSafetyError):
        import_to(legacy_fixture, legacy_fixture / "managed")


@pytest.mark.integration
def test_real_project_acceptance_is_exact_and_read_only(real_emperor_project: Path) -> None:
    observed = [
        real_emperor_project / "docs/assets/asset-production.md",
        real_emperor_project / "docs/assets/style-bible.md",
        real_emperor_project / "src/content/assetManifest.ts",
        real_emperor_project / "src/content/assetProductionStatus.ts",
        real_emperor_project / "output/imagegen/remaining/qa/validation.json",
    ]
    before = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in observed}

    preview = dry_run(real_emperor_project)

    after = {path: (path.stat().st_size, path.stat().st_mtime_ns) for path in observed}
    assert after == before
    assert preview["anchors"]["count"] == 3
    assert preview["runtime_assets"]["count"] == 186
    assert preview["runtime_assets"]["by_type"] == EXPECTED_TYPES
    assert preview["runtime_assets"]["by_status"] == {
        "integrated": 6,
        "normalized": 28,
        "reviewed": 152,
    }
    assert preview["qa"]["asset_count"] == 186
    assert preview["qa"]["contact_sheet_count"] == 8
    assert preview["qa"]["errors"] == 0
    assert preview["qa"]["warnings"] == 0
    assert preview["content"]["versions"]["v2"]["declared_counts"]["nodes"] == 600
    assert preview["content"]["versions"]["v3"]["declared_counts"]["stateMemorials"] == 18
    assert preview["missing_media"] == []

