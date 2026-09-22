from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import create_asset, create_project


def _revision(client: TestClient, asset_id: str, content: dict) -> dict:
    response = client.post(
        "/api/revisions",
        json={"asset_id": asset_id, "format": "json", "content": content},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_narrative_map_builds_tree_graph_and_coverage(
    client: TestClient, project_root
) -> None:
    project = create_project(client, project_root)
    chapter = create_asset(
        client,
        project["id"],
        key="content.chapter.court",
        kind="content",
        subtype="chapter",
        title="权谋暗涌",
    )
    scene = create_asset(
        client,
        project["id"],
        key="content.scene.throne_dialogue",
        kind="content",
        subtype="scene",
        title="朝堂对峙",
    )
    emperor = create_asset(
        client,
        project["id"],
        key="entity.character.emperor",
        kind="entity",
        subtype="character",
        title="皇帝",
    )
    ready_portrait = create_asset(
        client,
        project["id"],
        key="media.portrait.emperor",
        kind="media",
        subtype="character_portrait",
        title="皇帝立绘",
    )
    _revision(client, chapter["id"], {"title": "权谋暗涌", "order": 3})
    scene_revision = _revision(
        client,
        scene["id"],
        {
            "title": "朝堂对峙",
            "summary": "边关捷报传来，主战派要求追击。",
            "order": 7,
            "participants": ["entity.character.emperor"],
            "asset_requirements": [
                {
                    "asset_key": "media.portrait.emperor",
                    "title": "皇帝立绘",
                    "role": "角色立绘",
                    "subtype": "character_portrait",
                },
                {
                    "asset_key": "media.cg.throne_dialogue",
                    "title": "朝堂对峙 CG",
                    "role": "CG 画面",
                    "subtype": "cg",
                },
            ],
        },
    )
    emperor_revision = _revision(client, emperor["id"], {"name": "皇帝"})
    assert client.post(
        "/api/reviews", json={"revision_id": emperor_revision["id"], "verdict": "approve"}
    ).status_code == 201
    portrait_revision = _revision(client, ready_portrait["id"], {"name": "ready"})
    assert client.post(
        "/api/reviews", json={"revision_id": portrait_revision["id"], "verdict": "approve"}
    ).status_code == 201
    for source_id, target_id, relation_type in (
        (chapter["id"], scene["id"], "contains"),
        (emperor["id"], scene["id"], "appears_in"),
        (ready_portrait["id"], emperor["id"], "depicts"),
    ):
        response = client.post(
            "/api/relations",
            json={
                "project_id": project["id"],
                "source_asset_id": source_id,
                "target_asset_id": target_id,
                "relation_type": relation_type,
            },
        )
        assert response.status_code == 201, response.text

    response = client.get(f"/api/projects/{project['id']}/narrative-map")
    assert response.status_code == 200, response.text
    atlas = response.json()

    assert atlas["chapters"][0]["scene_ids"] == [scene["id"]]
    mapped_scene = atlas["scenes"][0]
    assert mapped_scene["revision_id"] == scene_revision["id"]
    assert mapped_scene["coverage"] == {
        "required": 3,
        "ready": 2,
        "candidate": 0,
        "planned": 0,
        "missing": 1,
        "ratio": 2 / 3,
    }
    assert {node["asset_id"] for node in mapped_scene["graph"]["nodes"]} >= {
        scene["id"],
        emperor["id"],
        ready_portrait["id"],
    }
    assert atlas["coverage"]["missing"] == 1


def test_materialized_requirements_use_catalog_asset_identity(
    client: TestClient, project_root
) -> None:
    project = create_project(client, project_root)
    scene = create_asset(
        client,
        project["id"],
        key="content.scene.river",
        kind="content",
        subtype="scene",
        title="收复河西",
    )
    _revision(
        client,
        scene["id"],
        {
            "asset_requirements": [
                {
                    "asset_key": "media.cg.river_victory",
                    "title": "收复河西 CG",
                    "role": "场景 CG",
                    "kind": "media",
                    "subtype": "cg",
                    "prompt": "将士在河西城门前庆祝胜利。",
                }
            ]
        },
    )
    atlas = client.get(f"/api/projects/{project['id']}/narrative-map").json()
    requirement_id = atlas["scenes"][0]["requirements"][0]["id"]

    first = client.post(
        f"/api/projects/{project['id']}/narrative-map/scenes/{scene['id']}/requirements",
        json={"requirement_ids": [requirement_id]},
    )
    assert first.status_code == 200, first.text
    created_id = first.json()["asset_ids"][0]
    assert first.json()["created_asset_ids"] == [created_id]

    second = client.post(
        f"/api/projects/{project['id']}/narrative-map/scenes/{scene['id']}/requirements",
        json={"requirement_ids": [requirement_id]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["asset_ids"] == [created_id]
    assert second.json()["created_asset_ids"] == []

    asset = client.get(f"/api/assets/{created_id}").json()
    assert asset["key"] == "media.cg.river_victory"
    assert asset["asset_metadata"]["source_scene_key"] == scene["key"]
    assert client.post(f"/api/projects/{project['id']}/rebuild").status_code == 200
    assert client.get(f"/api/assets/{created_id}").status_code == 200
