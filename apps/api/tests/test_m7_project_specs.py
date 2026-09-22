from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from game_assets_api.storage import sha256_bytes

from .conftest import create_project


def _write_specs(root: Path, *, style: str = "# 风格圣经\n\n初始规范。\n") -> None:
    production = root / "production"
    recipes = production / "prompt-recipes"
    recipes.mkdir(parents=True, exist_ok=True)
    (production / "style-bible.md").write_text(style, encoding="utf-8")
    (recipes / "emperor-primary.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "id": "prompt_emperor_primary",
                "name": "Emperor primary visual recipe",
                "style_bible": "production/style-bible.md",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _asset_by_key(client: TestClient, project_id: str, key: str) -> dict:
    assets = client.get("/api/assets", params={"project_id": project_id}).json()
    return next(asset for asset in assets if asset["key"] == key)


def test_project_specs_import_as_approved_idempotent_baselines(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    _write_specs(project_root)

    first = client.post(f"/api/projects/{project['id']}/scan")
    assert first.status_code == 200, first.text
    assert first.json()["errors"] == []

    style = _asset_by_key(client, project["id"], "design.style_bible.primary")
    recipe = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    assert style["content_status"] == recipe["content_status"] == "approved"
    assert style["asset_metadata"]["source_drift_status"] == "in_sync"
    assert recipe["asset_metadata"]["recipe_id"] == "prompt_emperor_primary"

    style_revisions = client.get("/api/revisions", params={"asset_id": style["id"]}).json()
    recipe_revisions = client.get("/api/revisions", params={"asset_id": recipe["id"]}).json()
    assert [revision["format"] for revision in style_revisions] == ["markdown"]
    assert [revision["format"] for revision in recipe_revisions] == ["json"]
    reviews = client.get("/api/reviews", params={"asset_id": style["id"]}).json()
    assert len(reviews) == 1
    assert reviews[0]["verdict"] == "approve"
    assert reviews[0]["is_valid"] is True

    second = client.post(f"/api/projects/{project['id']}/scan")
    assert second.status_code == 200, second.text
    assert len(client.get("/api/revisions", params={"asset_id": style["id"]}).json()) == 1
    assert len(client.get("/api/revisions", params={"asset_id": recipe["id"]}).json()) == 1
    profile = json.loads((project_root / "production" / "style-bible.json").read_text())
    assert profile["revision_id"] == style["current_revision_id"]


def test_external_drift_is_one_candidate_and_rejection_does_not_write_source(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    _write_specs(project_root)
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    source = project_root / "production" / "style-bible.md"
    source.write_text("# 外部修改\n\n保留原文件。\n", encoding="utf-8")

    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    style = _asset_by_key(client, project["id"], "design.style_bible.primary")
    candidate_id = style["latest_candidate_revision_id"]
    assert candidate_id
    assert style["asset_metadata"]["source_drift_status"] == "candidate"
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    style_again = _asset_by_key(client, project["id"], "design.style_bible.primary")
    assert style_again["latest_candidate_revision_id"] == candidate_id
    assert len(client.get("/api/revisions", params={"asset_id": style["id"]}).json()) == 2

    rejected = client.post(
        "/api/reviews",
        json={"revision_id": candidate_id, "verdict": "reject"},
    )
    assert rejected.status_code == 201, rejected.text
    assert source.read_text(encoding="utf-8") == "# 外部修改\n\n保留原文件。\n"
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    style_after_reject = _asset_by_key(client, project["id"], "design.style_bible.primary")
    assert style_after_reject["latest_candidate_revision_id"] is None
    assert style_after_reject["asset_metadata"]["source_drift_status"] == "rejected"
    assert len(client.get("/api/revisions", params={"asset_id": style["id"]}).json()) == 2


def test_page_candidates_publish_specs_and_missing_source_keeps_last_approval(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    _write_specs(project_root)
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    recipe = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    candidate = client.post(
        "/api/revisions",
        json={
            "asset_id": recipe["id"],
            "format": "json",
            "parent_revision_id": recipe["current_revision_id"],
            "content": {
                "format_version": 1,
                "id": "prompt_emperor_primary",
                "name": "Updated recipe",
                "style_bible": "production/style-bible.md",
                "quality": "high",
            },
        },
    )
    assert candidate.status_code == 201, candidate.text
    recipe_candidate = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    assert recipe_candidate["asset_metadata"]["source_drift_status"] == "candidate"
    assert recipe_candidate["latest_candidate_revision_id"] == candidate.json()["id"]
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    recipe_after_scan = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    assert recipe_after_scan["asset_metadata"]["source_drift_status"] == "candidate"
    assert recipe_after_scan["latest_candidate_revision_id"] == candidate.json()["id"]
    approved = client.post(
        "/api/reviews",
        json={"revision_id": candidate.json()["id"], "verdict": "approve"},
    )
    assert approved.status_code == 201, approved.text
    recipe_path = project_root / "production" / "prompt-recipes" / "emperor-primary.json"
    published = json.loads(recipe_path.read_text(encoding="utf-8"))
    assert published["name"] == "Updated recipe"
    recipe_after = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    assert recipe_after["asset_metadata"]["source_drift_status"] == "in_sync"
    assert recipe_after["asset_metadata"]["source_sha256"] == sha256_bytes(
        recipe_path.read_bytes()
    )

    recipe_path.unlink()
    rebuilt = client.post(f"/api/projects/{project['id']}/rebuild")
    assert rebuilt.status_code == 200, rebuilt.text
    preserved = _asset_by_key(
        client,
        project["id"],
        "production.prompt_recipe.emperor_primary",
    )
    assert preserved["current_revision_id"] == candidate.json()["id"]
    assert preserved["asset_metadata"]["source_missing"] is True


def test_rejected_page_edit_marks_candidate_without_writing_source(
    client: TestClient, project_root: Path
) -> None:
    project = create_project(client, project_root)
    _write_specs(project_root)
    assert client.post(f"/api/projects/{project['id']}/scan").status_code == 200
    style = _asset_by_key(client, project["id"], "design.style_bible.primary")
    source = project_root / "production" / "style-bible.md"
    original = source.read_text(encoding="utf-8")

    candidate = client.post(
        "/api/revisions",
        json={
            "asset_id": style["id"],
            "format": "markdown",
            "parent_revision_id": style["current_revision_id"],
            "content": "# 页面候选\n\n等待人工审核。\n",
        },
    )
    assert candidate.status_code == 201, candidate.text
    rejected = client.post(
        "/api/reviews",
        json={"revision_id": candidate.json()["id"], "verdict": "reject"},
    )
    assert rejected.status_code == 201, rejected.text

    after = _asset_by_key(client, project["id"], "design.style_bible.primary")
    assert after["asset_metadata"]["source_drift_status"] == "rejected"
    assert after["current_revision_id"] == style["current_revision_id"]
    assert after["latest_candidate_revision_id"] is None
    assert source.read_text(encoding="utf-8") == original
