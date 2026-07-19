from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def _array(values: list[str]) -> str:
    return ", ".join(json.dumps(value) for value in values)


@pytest.fixture()
def legacy_fixture(tmp_path: Path) -> Path:
    """Create a complete, tiny 3-anchor/186-asset legacy checkout."""

    root = tmp_path / "Emperor-Simulator"
    core = [(f"person-{index:02d}", "calm", "concerned") for index in range(1, 25)]
    emperor = ["young", "middle", "elder", "ill"]
    support = [f"support-{index:02d}" for index in range(1, 17)]
    backgrounds = [f"place-{index:02d}" for index in range(1, 21)]
    cgs = [f"scene-{index:02d}" for index in range(1, 17)]
    endings = [f"ending-{index:02d}" for index in range(1, 9)]

    manifest_lines = [
        "const corePortraitSpecs = [",
        *[
            f"  {{ characterId: {json.dumps(character)}, expressions: "
            f"[{json.dumps(first)}, {json.dumps(second)}] }},"
            for character, first, second in core
        ],
        "] as const",
        f"const emperorPortraits: unknown[] = [{_array(emperor)}].map((expression) => expression)",
        f"const supportPortraitIds = [{_array(support)}] as const",
        f"const backgroundIds = [{_array(backgrounds)}] as const",
        f"const cgIds = [{_array(cgs)}] as const",
        "const icons = Array.from({ length: 50 }, (_, index) => index)",
        f"const endingIds = [{_array(endings)}] as const",
        "export const assetManifest = []",
    ]
    _write(root / "src/content/assetManifest.ts", "\n".join(manifest_lines) + "\n")

    entries: list[dict[str, object]] = []
    for character, first, second in core:
        for expression in ("neutral", first, second):
            entries.append(
                {
                    "key": f"portrait.{character}.{expression}",
                    "type": "portrait",
                    "target": f"public/assets/portraits/core/{character}/{expression}.webp",
                    "source": f"art-source/portraits/core/{character}/{expression}.png",
                }
            )
    for expression in emperor:
        entries.append(
            {
                "key": f"portrait.emperor.{expression}",
                "type": "portrait",
                "target": f"public/assets/portraits/emperor/{expression}.webp",
                "source": f"art-source/portraits/emperor/{expression}.png",
            }
        )
    for support_id in support:
        entries.append(
            {
                "key": f"portrait.support-{support_id}.neutral",
                "type": "portrait",
                "target": f"public/assets/portraits/support/{support_id}.webp",
                "source": f"art-source/portraits/support/{support_id}.png",
            }
        )
    for item_id in backgrounds:
        entries.append(
            {
                "key": f"background.{item_id}",
                "type": "background",
                "target": f"public/assets/backgrounds/{item_id}.webp",
                "source": f"art-source/backgrounds/{item_id}.png",
            }
        )
    for item_id in cgs:
        entries.append(
            {
                "key": f"cg.{item_id}",
                "type": "cg",
                "target": f"public/assets/cgs/{item_id}.webp",
                "source": f"art-source/cgs/{item_id}.png",
            }
        )
    for index in range(1, 51):
        item_id = f"artifact-{index:02d}"
        entries.append(
            {
                "key": f"icon.{item_id}",
                "type": "icon",
                "target": f"public/assets/icons/{item_id}.webp",
                "source": f"art-source/icons/{item_id}.png",
            }
        )
    for item_id in endings:
        entries.append(
            {
                "key": f"ending.{item_id}",
                "type": "ending",
                "target": f"public/assets/endings/{item_id}.webp",
                "source": f"art-source/endings/{item_id}.png",
            }
        )
    assert len(entries) == 186

    statuses = "\n".join(f'  {json.dumps(item["key"])}: "planned",' for item in entries)
    _write(
        root / "src/content/assetProductionStatus.ts",
        f"export const assetProductionStatus = {{\n{statuses}\n}}\n",
    )

    ledger: list[str] = ["# Fixture production ledger", ""]
    anchors = (
        ("anchor.character", "Character", 1024, 1536),
        ("anchor.background", "Background", 1920, 1080),
        ("anchor.cg", "CG", 2048, 1152),
    )
    for key, name, _, _ in anchors:
        subtype = key.split(".")[1]
        target = f"art-source/anchors/{subtype}-style-anchor.png"
        source = f"art-source/anchors/{subtype}-style-anchor-source.png"
        ledger.extend(
            [
                f"### {key} — {name}",
                "- Status: approved",
                f"- Target: {target}",
                f"- Source: {source}",
                "- Reference: 无",
                "- Prompt: fixture prompt",
                "- QA: fixture qa",
                "- Notes: fixture notes",
                "",
            ]
        )
        _write(root / target, b"anchor-target")
        _write(root / source, b"anchor-source")
    for item in entries:
        ledger.extend(
            [
                f"### {item['key']} — {item['key']}",
                "- Status: planned",
                f"- Target: {item['target']}",
                f"- Source: {item['source']}",
                "- Reference: 无",
                "- Prompt: fixture prompt",
                "- QA: fixture qa",
                "- Notes: fixture notes",
                "",
            ]
        )
        _write(root / str(item["target"]), b"runtime")
        _write(root / str(item["source"]), b"source")
    _write(root / "docs/assets/asset-production.md", "\n".join(ledger))
    _write(root / "docs/assets/style-bible.md", "# Fixture style bible\n")

    validation_assets = [
        {
            "key": item["key"],
            "path": item["target"],
            "errors": [],
            "warnings": [],
        }
        for item in entries
    ]
    _write(
        root / "output/imagegen/remaining/qa/validation.json",
        json.dumps({"assets": validation_assets, "errors": 0, "warnings": 0}),
    )
    sheet = "output/imagegen/remaining/qa/all.jpg"
    _write(root / sheet, b"contact-sheet")
    _write(
        root / "output/imagegen/remaining/qa/contact-sheets.json",
        json.dumps([sheet]),
    )

    v2_counts = {
        "chains": 80,
        "chainNodes": 480,
        "standalone": 120,
        "nodes": 600,
        "achievements": 40,
        "collectibles": 50,
        "endings": 14,
    }
    v3_counts = {
        "coreChains": 3,
        "coreNodes": 15,
        "shortChains": 5,
        "shortNodes": 15,
        "standaloneStories": 12,
        "stateMemorials": 18,
        "randomEvents": 30,
    }
    _write(
        root / "src/content/v2/registry.ts",
        "export const contentV2Registry = { counts: {\n"
        + "\n".join(f"  {key}: {value}," for key, value in v2_counts.items())
        + "\n} }\n",
    )
    _write(
        root / "src/content/v3/registry.ts",
        "export const contentV3Registry = { counts: {\n"
        + "\n".join(f"  {key}: {value}," for key, value in v3_counts.items())
        + "\n} }\n",
    )
    for filename, prefix, count in (
        ("achievements.ts", "achievement", 40),
        ("collectibles.ts", "artifact", 50),
        ("endings.ts", "ending", 14),
    ):
        records = "\n".join(
            f'  {{\n    id: "{prefix}-{index:02d}", name: "{prefix} {index}", value: 1,\n  }},'
            for index in range(1, count + 1)
        )
        _write(root / f"src/content/v2/{filename}", f"export const records = [\n{records}\n]\n")

    # A credential-looking file proves the adapter does not discover arbitrary root files.
    _write(root / "image-gen-key.json", '{"apiKey":"must-not-be-read"}')
    return root


@pytest.fixture(scope="session")
def real_emperor_project() -> Path:
    configured = os.environ.get(
        "EMPEROR_SIMULATOR_ROOT", "/Users/0x10/Documents/Github/Emperor-Simulator"
    )
    root = Path(configured)
    if not (root / "src/content/assetManifest.ts").is_file():
        pytest.skip("real Emperor-Simulator checkout is unavailable")
    return root.resolve()

