from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import AssetCreate, STABLE_KEY_RE
from .models import Asset, AssetRelation, AssetRevision, Project, Rendition
from .services import ServiceError, create_asset, require
from .storage import stable_id


CHAPTER_SUBTYPES = {"chapter", "story_arc", "act"}
SCENE_SUBTYPES = {"scene", "event", "story_event", "dialogue_scene"}


def _number(value: Any, fallback: int = 0) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _active_revision(session: Session, asset: Asset) -> AssetRevision | None:
    revision_id = asset.latest_candidate_revision_id or asset.current_revision_id
    return session.get(AssetRevision, revision_id) if revision_id else None


def _revision_content(session: Session, asset: Asset) -> dict[str, Any]:
    revision = _active_revision(session, asset)
    return dict(revision.content_json) if revision and isinstance(revision.content_json, dict) else {}


def _asset_status(asset: Asset | None) -> str:
    if asset is None:
        return "missing"
    if asset.content_status == "approved" and asset.current_revision_id:
        return "ready"
    if asset.generation_status in {
        "planned",
        "queued",
        "running",
        "output_received",
        "hard_qa",
        "semantic_qa",
        "remediating",
    }:
        return "planned"
    if asset.latest_candidate_revision_id:
        return "candidate"
    return "missing"


def _requirements(content: dict[str, Any], *, scene: Asset) -> list[dict[str, Any]]:
    raw = content.get("asset_requirements", content.get("requirements", []))
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if isinstance(item, str):
            key = item.strip()
            value: dict[str, Any] = {"asset_key": key}
        elif isinstance(item, dict):
            value = dict(item)
            key = str(value.get("asset_key", value.get("key", ""))).strip()
        else:
            continue
        if not key or not STABLE_KEY_RE.fullmatch(key) or key in seen:
            continue
        seen.add(key)
        title = str(value.get("title") or value.get("name") or key.rsplit(".", 1)[-1])
        kind = str(value.get("kind") or "media")
        if kind not in {"content", "design", "entity", "media", "production"}:
            kind = "media"
        subtype = str(value.get("subtype") or "concept_art")
        role = str(value.get("role") or value.get("label") or "场景配套资产")
        normalized.append(
            {
                "id": stable_id("requirement", scene.id, key),
                "asset_key": key,
                "title": title,
                "kind": kind,
                "subtype": subtype,
                "role": role,
                "prompt": str(
                    value.get("prompt")
                    or f"为场景《{scene.title}》生成{role}：{title}。遵循当前 Project 制作规范。"
                ),
                "width": _number(value.get("width"), 1024),
                "height": _number(value.get("height"), 1024),
                "transparent": bool(value.get("transparent", False)),
                "target_path": value.get("target_path"),
                "order": _number(value.get("order"), index),
            }
        )
    participants = content.get("participants", [])
    if isinstance(participants, list):
        for participant in participants:
            if not isinstance(participant, str):
                continue
            key = participant if participant.startswith("entity.") else f"entity.character.{participant}"
            if not STABLE_KEY_RE.fullmatch(key) or key in seen:
                continue
            seen.add(key)
            normalized.append(
                {
                    "id": stable_id("requirement", scene.id, key),
                    "asset_key": key,
                    "title": participant.rsplit(".", 1)[-1].replace("-", " "),
                    "kind": "entity",
                    "subtype": "character",
                    "role": "出场角色",
                    "prompt": f"为场景《{scene.title}》补齐角色实体：{participant}。",
                    "width": 1024,
                    "height": 1536,
                    "transparent": True,
                    "target_path": None,
                    "order": len(normalized),
                }
            )
    references = content.get("references", [])
    if isinstance(references, list):
        for reference in references:
            if not isinstance(reference, str) or not STABLE_KEY_RE.fullmatch(reference) or reference in seen:
                continue
            seen.add(reference)
            normalized.append(
                {
                    "id": stable_id("requirement", scene.id, reference),
                    "asset_key": reference,
                    "title": reference.rsplit(".", 1)[-1].replace("-", " "),
                    "kind": "entity",
                    "subtype": "reference",
                    "role": "叙事引用",
                    "prompt": f"为场景《{scene.title}》补齐引用资产：{reference}。",
                    "width": 1024,
                    "height": 1024,
                    "transparent": False,
                    "target_path": None,
                    "order": len(normalized),
                }
            )
    return sorted(normalized, key=lambda item: (item["order"], item["asset_key"]))


def _thumbnail_renditions(
    session: Session, assets: list[Asset]
) -> dict[str, Rendition]:
    revision_to_asset: dict[str, str] = {}
    for asset in assets:
        revision_id = asset.latest_candidate_revision_id or asset.current_revision_id
        if revision_id:
            revision_to_asset[revision_id] = asset.id
    if not revision_to_asset:
        return {}
    rows = session.scalars(
        select(Rendition)
        .where(Rendition.revision_id.in_(revision_to_asset))
        .order_by(Rendition.created_at.desc(), Rendition.id)
    ).all()
    result: dict[str, Rendition] = {}
    for rendition in rows:
        result.setdefault(revision_to_asset[rendition.revision_id], rendition)
    return result


def build_narrative_map(session: Session, project_id: str) -> dict[str, Any]:
    project = require(session, Project, project_id, "project")
    assets = list(
        session.scalars(
            select(Asset).where(Asset.project_id == project.id).order_by(Asset.kind, Asset.key)
        ).all()
    )
    by_id = {asset.id: asset for asset in assets}
    by_key = {asset.key: asset for asset in assets}
    contents = {asset.id: _revision_content(session, asset) for asset in assets}
    relations = list(
        session.scalars(
            select(AssetRelation)
            .where(AssetRelation.project_id == project.id)
            .order_by(AssetRelation.created_at, AssetRelation.id)
        ).all()
    )
    thumbnails = _thumbnail_renditions(session, assets)

    chapters = [
        asset
        for asset in assets
        if asset.kind == "content" and asset.subtype in CHAPTER_SUBTYPES
    ]
    scenes = [
        asset
        for asset in assets
        if asset.kind == "content" and asset.subtype in SCENE_SUBTYPES
    ]
    contained_by: dict[str, str] = {}
    contained_order: dict[str, int] = {}
    for relation in relations:
        if relation.relation_type != "contains":
            continue
        if relation.source_asset_id in {chapter.id for chapter in chapters} and relation.target_asset_id in {
            scene.id for scene in scenes
        }:
            contained_by[relation.target_asset_id] = relation.source_asset_id
            contained_order[relation.target_asset_id] = _number(
                relation.metadata_json.get("order") if relation.metadata_json else None,
                0,
            )

    for scene in scenes:
        if scene.id in contained_by:
            continue
        for key in contents.get(scene.id, {}).get("chapter_keys", []):
            chapter = by_key.get(str(key))
            if chapter and chapter in chapters:
                contained_by[scene.id] = chapter.id
                break
        if scene.id in contained_by:
            continue
        chapter_key = str(
            contents[scene.id].get("chapter_key")
            or scene.metadata_json.get("chapter_key", "")
        )
        chapter = by_key.get(chapter_key)
        if chapter and chapter in chapters:
            contained_by[scene.id] = chapter.id

    for chapter in chapters:
        raw_node_keys = contents[chapter.id].get(
            "node_keys", contents[chapter.id].get("nodes", [])
        )
        if not isinstance(raw_node_keys, list):
            continue
        for index, key in enumerate(raw_node_keys):
            scene = by_key.get(str(key))
            if scene and scene in scenes and scene.id not in contained_by:
                contained_by[scene.id] = chapter.id
                contained_order[scene.id] = index

    chapter_rows: list[dict[str, Any]] = []
    for chapter in sorted(
        chapters,
        key=lambda item: (
            _number(contents[item.id].get("order", item.metadata_json.get("order")), 0),
            item.key,
        ),
    ):
        scene_ids = [scene.id for scene in scenes if contained_by.get(scene.id) == chapter.id]
        scene_ids.sort(
            key=lambda scene_id: (
                contained_order.get(
                    scene_id,
                    _number(
                        contents[scene_id].get(
                            "order", by_id[scene_id].metadata_json.get("order")
                        ),
                        0,
                    ),
                ),
                by_id[scene_id].key,
            )
        )
        revision = _active_revision(session, chapter)
        chapter_rows.append(
            {
                "asset_id": chapter.id,
                "key": chapter.key,
                "title": chapter.title,
                "order": _number(
                    contents[chapter.id].get("order", chapter.metadata_json.get("order")),
                    0,
                ),
                "scene_ids": scene_ids,
                "revision_id": revision.id if revision else None,
                "revision_status": revision.review_status if revision else None,
            }
        )

    scene_rows: list[dict[str, Any]] = []
    totals: defaultdict[str, int] = defaultdict(int)
    for scene in scenes:
        content = contents[scene.id]
        requirements = _requirements(content, scene=scene)
        for requirement in requirements:
            target = by_key.get(requirement["asset_key"])
            if target:
                requirement["title"] = target.title
                requirement["kind"] = target.kind
                requirement["subtype"] = target.subtype
            requirement["asset_id"] = target.id if target else None
            requirement["status"] = _asset_status(target)
            rendition = thumbnails.get(target.id) if target else None
            requirement["rendition_id"] = rendition.id if rendition else None
            totals[requirement["status"]] += 1

        direct_ids = {scene.id}
        synthetic_edges: list[dict[str, Any]] = []
        participants = content.get("participants", [])
        if isinstance(participants, list):
            for participant in participants:
                if not isinstance(participant, str):
                    continue
                participant_key = (
                    participant if participant.startswith("entity.") else f"entity.character.{participant}"
                )
                target = by_key.get(participant_key)
                if target:
                    direct_ids.add(target.id)
                    synthetic_edges.append(
                        {
                            "id": stable_id("relation", target.id, scene.id, "appears_in"),
                            "source_asset_id": target.id,
                            "target_asset_id": scene.id,
                            "relation_type": "appears_in",
                            "metadata": {"source": "revision-content"},
                        }
                    )
        references = content.get("references", [])
        if isinstance(references, list):
            for reference in references:
                target = by_key.get(str(reference))
                if target:
                    direct_ids.add(target.id)
                    synthetic_edges.append(
                        {
                            "id": stable_id("relation", scene.id, target.id, "references"),
                            "source_asset_id": scene.id,
                            "target_asset_id": target.id,
                            "relation_type": "references",
                            "metadata": {"source": "revision-content"},
                        }
                    )
        for relation in relations:
            if relation.source_asset_id == scene.id:
                direct_ids.add(relation.target_asset_id)
            if relation.target_asset_id == scene.id:
                direct_ids.add(relation.source_asset_id)
        graph_ids = set(direct_ids)
        for relation in relations:
            if relation.source_asset_id in direct_ids or relation.target_asset_id in direct_ids:
                graph_ids.update((relation.source_asset_id, relation.target_asset_id))
        graph_nodes = []
        for asset_id in sorted(graph_ids, key=lambda value: by_id[value].key if value in by_id else value):
            asset = by_id.get(asset_id)
            if not asset:
                continue
            rendition = thumbnails.get(asset.id)
            graph_nodes.append(
                {
                    "asset_id": asset.id,
                    "key": asset.key,
                    "title": asset.title,
                    "kind": asset.kind,
                    "subtype": asset.subtype,
                    "status": _asset_status(asset),
                    "rendition_id": rendition.id if rendition else None,
                    "role": "scene" if asset.id == scene.id else asset.kind,
                }
            )
        graph_edges = [
            {
                "id": relation.id,
                "source_asset_id": relation.source_asset_id,
                "target_asset_id": relation.target_asset_id,
                "relation_type": relation.relation_type,
                "metadata": relation.metadata_json,
            }
            for relation in relations
            if relation.source_asset_id in graph_ids and relation.target_asset_id in graph_ids
        ]
        known_edge_signatures = {
            (edge["source_asset_id"], edge["target_asset_id"], edge["relation_type"])
            for edge in graph_edges
        }
        graph_edges.extend(
            edge
            for edge in synthetic_edges
            if (edge["source_asset_id"], edge["target_asset_id"], edge["relation_type"])
            not in known_edge_signatures
        )
        revision = _active_revision(session, scene)
        scene_counts: defaultdict[str, int] = defaultdict(int)
        for requirement in requirements:
            scene_counts[requirement["status"]] += 1
        required = len(requirements)
        scene_rows.append(
            {
                "asset_id": scene.id,
                "key": scene.key,
                "title": str(content.get("title") or scene.title),
                "chapter_asset_id": contained_by.get(scene.id),
                "order": contained_order.get(
                    scene.id,
                    _number(content.get("order", scene.metadata_json.get("order")), 0),
                ),
                "revision_id": revision.id if revision else None,
                "revision_status": revision.review_status if revision else None,
                "content": content,
                "requirements": requirements,
                "coverage": {
                    "required": required,
                    "ready": scene_counts["ready"],
                    "candidate": scene_counts["candidate"],
                    "planned": scene_counts["planned"],
                    "missing": scene_counts["missing"],
                    "ratio": (scene_counts["ready"] / required) if required else 1.0,
                },
                "graph": {"nodes": graph_nodes, "edges": graph_edges},
            }
        )
    chapter_rank = {
        chapter["asset_id"]: index for index, chapter in enumerate(chapter_rows)
    }
    scene_rows.sort(
        key=lambda row: (
            chapter_rank.get(row["chapter_asset_id"], len(chapter_rank)),
            row["order"],
            row["key"],
        )
    )
    required_total = sum(totals.values())
    return {
        "project_id": project.id,
        "project_name": project.name,
        "chapters": chapter_rows,
        "scenes": scene_rows,
        "unassigned_scene_ids": [scene.id for scene in scenes if scene.id not in contained_by],
        "coverage": {
            "required": required_total,
            "ready": totals["ready"],
            "candidate": totals["candidate"],
            "planned": totals["planned"],
            "missing": totals["missing"],
            "ratio": (totals["ready"] / required_total) if required_total else 1.0,
        },
    }


def materialize_scene_requirements(
    session: Session,
    *,
    project_id: str,
    scene_asset_id: str,
    requirement_ids: list[str],
) -> dict[str, Any]:
    project = require(session, Project, project_id, "project")
    scene = require(session, Asset, scene_asset_id, "scene asset")
    if scene.project_id != project.id or scene.kind != "content" or scene.subtype not in SCENE_SUBTYPES:
        raise ServiceError(422, "scene asset does not belong to this narrative map")
    requirements = _requirements(_revision_content(session, scene), scene=scene)
    selected = set(requirement_ids)
    if selected:
        unknown = selected - {requirement["id"] for requirement in requirements}
        if unknown:
            raise ServiceError(422, f"unknown narrative requirement: {sorted(unknown)[0]}")
        requirements = [requirement for requirement in requirements if requirement["id"] in selected]
    if not requirements:
        raise ServiceError(422, "no narrative asset requirements were selected")

    results: list[Asset] = []
    created_ids: list[str] = []
    for requirement in requirements:
        asset = session.scalar(
            select(Asset).where(
                Asset.project_id == project.id,
                Asset.key == requirement["asset_key"],
            )
        )
        if asset is None:
            asset = create_asset(
                session,
                AssetCreate(
                    project_id=project.id,
                    key=requirement["asset_key"],
                    kind=requirement["kind"],
                    subtype=requirement["subtype"],
                    title=requirement["title"],
                    tags=["narrative-map", "missing-asset"],
                    metadata={
                        "source_scene_key": scene.key,
                        "requirement_id": requirement["id"],
                        "requirement_role": requirement["role"],
                        "preview_summary": requirement["prompt"],
                        "production_stage": "planned",
                    },
                ),
            )
            created_ids.append(asset.id)
        results.append(asset)
    return {
        "project_id": project.id,
        "scene_asset_id": scene.id,
        "asset_ids": [asset.id for asset in results],
        "created_asset_ids": created_ids,
        "requirement_ids": [requirement["id"] for requirement in requirements],
    }
