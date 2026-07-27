from __future__ import annotations

import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .domain import (
    AssetCreate,
    ContentStatus,
    GenerationPlanCreate,
    GenerationStatus,
    ProjectCreate,
    ProjectUpdate,
    PublicationStatus,
    QARunCreate,
    QAVerdict,
    RelationCreate,
    ReleaseCreate,
    ReviewCreate,
    ReviewVerdict,
    RevisionCreate,
    ScanReport,
    TaskKind,
)
from .models import (
    Asset,
    AssetRelation,
    AssetRevision,
    GenerationJob,
    GenerationPlan,
    Project,
    ProviderProfile,
    QARun,
    Release,
    Rendition,
    ReviewDecision,
    new_id,
    utcnow,
)
from .qa import inspect_image
from .storage import (
    ProjectStore,
    StorageError,
    atomic_write_json,
    canonical_json,
    relative_to_root,
    safe_join,
    sha256_bytes,
    stable_id,
)


class ServiceError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def require(session: Session, model: type[Any], identifier: str, label: str):
    value = session.get(model, identifier)
    if value is None:
        raise ServiceError(404, f"{label} not found")
    return value


def register_project(session: Session, payload: ProjectCreate, projects_root: Path) -> Project:
    container = projects_root.expanduser().resolve()
    root = container / payload.directory_name
    if root.exists():
        raise ServiceError(409, "project directory already exists and will be discovered automatically")
    root.mkdir(parents=False)
    project_id = new_id()
    try:
        ProjectStore(root).initialize(
            project_id=project_id,
            name=payload.name,
            default_language=payload.default_language,
        )
    except StorageError as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise ServiceError(409, str(exc)) from exc
    project = Project(
        id=project_id,
        name=payload.name,
        root_path=str(root.resolve()),
        default_language=payload.default_language,
    )
    try:
        session.add(project)
        session.commit()
    except (StorageError, IntegrityError) as exc:
        session.rollback()
        raise ServiceError(409, str(exc)) from exc
    return project


def update_project(session: Session, project: Project, payload: ProjectUpdate) -> Project:
    try:
        ProjectStore(project.root_path).update_name(project_id=project.id, name=payload.name)
    except (OSError, StorageError) as exc:
        raise ServiceError(409, str(exc)) from exc
    project.name = payload.name
    session.commit()
    session.refresh(project)
    return project


def discover_projects(
    session: Session, projects_root: Path, *, scan: bool = False
) -> tuple[list[Project], list[str]]:
    container = projects_root.expanduser().resolve()
    container.mkdir(parents=True, exist_ok=True)
    discovered: dict[str, Project] = {}
    errors: list[str] = []
    for child in sorted(container.iterdir(), key=lambda path: path.name.casefold()):
        if not child.is_dir() or child.name == "local-state" or not (child / "project.yaml").is_file():
            continue
        try:
            root = child.resolve(strict=True)
            if root.parent != container:
                raise StorageError("project directory must be a direct, non-symlinked child")
            contract = ProjectStore(root).read_yaml(root / "project.yaml")
            if contract.get("format_version") != 1:
                raise StorageError("unsupported project format")
            project_id = contract.get("id")
            if not isinstance(project_id, str) or not project_id:
                raise StorageError("project contract is missing id")
            name = str(contract.get("name") or child.name)
            default_language = str(contract.get("default_language") or "zh-CN")
            project = session.get(Project, project_id)
            root_match = session.scalar(select(Project).where(Project.root_path == str(root)))
            if root_match is not None and root_match.id != project_id:
                session.delete(root_match)
                session.flush()
            if project is None:
                project = Project(id=project_id, root_path=str(root))
                session.add(project)
            project.name = name
            project.root_path = str(root)
            project.default_language = default_language
            discovered[project_id] = project
        except (OSError, StorageError, TypeError, ValueError) as exc:
            errors.append(f"{child.name}: {exc}")
    session.flush()
    for existing in session.scalars(select(Project)).all():
        try:
            managed = Path(existing.root_path).expanduser().resolve().parent == container
        except OSError:
            managed = False
        if managed and existing.id not in discovered:
            session.delete(existing)
    session.commit()
    projects = sorted(discovered.values(), key=lambda project: project.name.casefold())
    if scan:
        for project in projects:
            report = scan_project(session, project)
            errors.extend(f"{Path(project.root_path).name}: {error}" for error in report.errors)
    return projects, errors


def asset_descriptor(asset: Asset) -> dict[str, Any]:
    return {
        "id": asset.id,
        "project_id": asset.project_id,
        "key": asset.key,
        "kind": asset.kind,
        "subtype": asset.subtype,
        "title": asset.title,
        "schema_ref": asset.schema_ref,
        "tags": asset.tags,
        "metadata": asset.metadata_json,
        "content_status": asset.content_status,
        "generation_status": asset.generation_status,
        "publication_status": asset.publication_status,
        "current_revision_id": asset.current_revision_id,
        "latest_candidate_revision_id": asset.latest_candidate_revision_id,
        "created_at": asset.created_at.isoformat(),
        "updated_at": asset.updated_at.isoformat(),
    }


def create_asset(session: Session, payload: AssetCreate) -> Asset:
    project = require(session, Project, payload.project_id, "project")
    store = ProjectStore(project.root_path)
    if payload.schema_ref:
        try:
            schema = store.read_schema(payload.schema_ref)
            jsonschema.validate(payload.metadata, schema)
        except (StorageError, jsonschema.ValidationError, jsonschema.SchemaError) as exc:
            raise ServiceError(422, f"custom metadata does not satisfy schema: {exc}") from exc
    asset = Asset(
        id=new_id(),
        project_id=project.id,
        key=payload.key,
        kind=payload.kind.value,
        subtype=payload.subtype,
        title=payload.title,
        schema_ref=payload.schema_ref,
        tags=payload.tags,
        metadata_json=payload.metadata,
    )
    session.add(asset)
    session.flush()
    try:
        store.write_asset(asset_descriptor(asset))
        session.commit()
    except (StorageError, IntegrityError) as exc:
        session.rollback()
        message = "asset key already exists" if isinstance(exc, IntegrityError) else str(exc)
        raise ServiceError(409, message) from exc
    return asset


def _revision_file(revision: AssetRevision) -> dict[str, Any]:
    return {
        "id": revision.id,
        "asset_id": revision.asset_id,
        "sequence": revision.sequence,
        "format": revision.format,
        "content": revision.content_json,
        "content_hash": revision.content_hash,
        "parent_revision_id": revision.parent_revision_id,
        "input_hash": revision.input_hash,
        "style_revision": revision.style_revision,
        "prompt_recipe": revision.prompt_recipe,
        "provider_snapshot": revision.provider_snapshot,
        "review_status": revision.review_status,
        "created_at": revision.created_at.isoformat(),
    }


def create_revision(session: Session, payload: RevisionCreate) -> AssetRevision:
    asset = require(session, Asset, payload.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    store = ProjectStore(project.root_path)
    if asset.schema_ref and payload.format.value == "json":
        try:
            schema = store.read_schema(asset.schema_ref)
            jsonschema.validate(payload.content, schema)
        except (StorageError, jsonschema.ValidationError, jsonschema.SchemaError) as exc:
            raise ServiceError(422, f"revision does not satisfy asset schema: {exc}") from exc
    parent = None
    if payload.parent_revision_id:
        parent = require(session, AssetRevision, payload.parent_revision_id, "parent revision")
        if parent.asset_id != asset.id:
            raise ServiceError(422, "parent revision belongs to a different asset")
    pending_revisions = list(
        session.scalars(
            select(AssetRevision).where(
                AssetRevision.asset_id == asset.id,
                AssetRevision.review_status == "pending",
            )
        ).all()
    )
    superseded_revision_ids = [revision.id for revision in pending_revisions]
    for pending_revision in pending_revisions:
        pending_revision.review_status = "superseded"
    sequence = (session.scalar(select(func.max(AssetRevision.sequence)).where(AssetRevision.asset_id == asset.id)) or 0) + 1
    content_hash = sha256_bytes(canonical_json(payload.content))
    input_hash = payload.input_hash or sha256_bytes(
        canonical_json(
            {
                "content": payload.content,
                "parent": payload.parent_revision_id,
                "style": payload.style_revision,
                "recipe": payload.prompt_recipe,
                "provider": payload.provider_snapshot,
            }
        )
    )
    revision = AssetRevision(
        id=new_id(),
        asset_id=asset.id,
        sequence=sequence,
        format=payload.format.value,
        content_json=payload.content,
        content_hash=content_hash,
        parent_revision_id=payload.parent_revision_id,
        input_hash=input_hash,
        style_revision=payload.style_revision,
        prompt_recipe=payload.prompt_recipe,
        provider_snapshot=payload.provider_snapshot,
        file_path="pending",
        review_status="pending",
    )
    asset.latest_candidate_revision_id = revision.id
    if asset.current_revision_id is None:
        asset.content_status = ContentStatus.CANDIDATE.value
        asset.publication_status = PublicationStatus.UNPUBLISHED.value
    asset.updated_at = utcnow()
    session.add(revision)
    session.flush()
    try:
        revision.file_path = store.write_revision(
            kind=asset.kind, key=asset.key, revision=_revision_file(revision)
        )
        store.update_asset_candidate(
            kind=asset.kind,
            key=asset.key,
            latest_candidate_revision_id=revision.id,
            superseded_revision_ids=superseded_revision_ids,
            updated_at=asset.updated_at.isoformat(),
        )
        session.commit()
    except (StorageError, IntegrityError) as exc:
        session.rollback()
        raise ServiceError(409, str(exc)) from exc
    return revision


def _revision_title(asset: Asset, content: Any) -> str:
    if not isinstance(content, dict):
        return asset.title
    for field in ("title", "name", "summary"):
        value = content.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:240]
    return asset.title


def _target_asset_by_key(session: Session, project_id: str, key: str) -> Asset:
    target = session.scalar(
        select(Asset).where(Asset.project_id == project_id, Asset.key == key)
    )
    if target is None:
        raise ServiceError(422, f"relation target does not exist: {key}")
    return target


def _sync_relation_group(
    session: Session,
    *,
    project: Project,
    relation_type: str,
    desired_pairs: set[tuple[str, str]],
    source_scope: set[str] | None = None,
    target_scope: set[str] | None = None,
) -> set[str]:
    statement = select(AssetRelation).where(
        AssetRelation.project_id == project.id,
        AssetRelation.relation_type == relation_type,
    )
    if source_scope is not None:
        statement = statement.where(AssetRelation.source_asset_id.in_(source_scope))
    if target_scope is not None:
        statement = statement.where(AssetRelation.target_asset_id.in_(target_scope))
    existing = list(session.scalars(statement).all())
    existing_pairs = {(row.source_asset_id, row.target_asset_id) for row in existing}
    affected_sources = {source for source, _target in existing_pairs | desired_pairs}
    for row in existing:
        if (row.source_asset_id, row.target_asset_id) not in desired_pairs:
            session.delete(row)
    for source_id, target_id in sorted(desired_pairs - existing_pairs):
        session.add(
            AssetRelation(
                id=new_id(),
                project_id=project.id,
                source_asset_id=source_id,
                target_asset_id=target_id,
                relation_type=relation_type,
                metadata_json={"source": "approved-revision"},
            )
        )
    session.flush()
    return affected_sources


def _write_relation_sources(
    session: Session,
    *,
    project: Project,
    relation_type: str,
    source_ids: set[str],
) -> None:
    if not source_ids:
        return
    store = ProjectStore(project.root_path)
    sources = {
        asset.id: asset
        for asset in session.scalars(select(Asset).where(Asset.id.in_(source_ids))).all()
    }
    rows = list(
        session.scalars(
            select(AssetRelation).where(
                AssetRelation.project_id == project.id,
                AssetRelation.relation_type == relation_type,
                AssetRelation.source_asset_id.in_(source_ids),
            )
        ).all()
    )
    target_ids = {row.target_asset_id for row in rows}
    targets = {
        asset.id: asset.key
        for asset in session.scalars(select(Asset).where(Asset.id.in_(target_ids))).all()
    } if target_ids else {}
    by_source: dict[str, list[str]] = {source_id: [] for source_id in source_ids}
    for row in rows:
        if row.target_asset_id in targets:
            by_source.setdefault(row.source_asset_id, []).append(targets[row.target_asset_id])
    for source_id, target_keys in by_source.items():
        source = sources.get(source_id)
        if source is not None:
            store.update_asset_relations(
                kind=source.kind,
                key=source.key,
                relation_type=relation_type,
                target_keys=target_keys,
            )


def _sync_approved_relations(
    session: Session, *, project: Project, asset: Asset, content: Any
) -> None:
    if not isinstance(content, dict):
        return

    if asset.subtype == "story_arc" and isinstance(content.get("nodes"), list):
        targets = {
            _target_asset_by_key(session, project.id, str(key)).id
            for key in content["nodes"]
            if isinstance(key, str)
        }
        affected = _sync_relation_group(
            session,
            project=project,
            relation_type="contains",
            desired_pairs={(asset.id, target_id) for target_id in targets},
            source_scope={asset.id},
        )
        _write_relation_sources(
            session, project=project, relation_type="contains", source_ids=affected
        )

    if asset.kind == "content" and isinstance(content.get("participants"), list):
        character_ids = {
            _target_asset_by_key(
                session,
                project.id,
                participant
                if participant.startswith("entity.character.")
                else f"entity.character.{participant}",
            ).id
            for participant in content["participants"]
            if isinstance(participant, str)
        }
        affected = _sync_relation_group(
            session,
            project=project,
            relation_type="appears_in",
            desired_pairs={(character_id, asset.id) for character_id in character_ids},
            target_scope={asset.id},
        )
        _write_relation_sources(
            session, project=project, relation_type="appears_in", source_ids=affected
        )

    if isinstance(content.get("references"), list):
        targets = {
            _target_asset_by_key(session, project.id, str(key)).id
            for key in content["references"]
            if isinstance(key, str)
        }
        affected = _sync_relation_group(
            session,
            project=project,
            relation_type="references",
            desired_pairs={(asset.id, target_id) for target_id in targets},
            source_scope={asset.id},
        )
        _write_relation_sources(
            session, project=project, relation_type="references", source_ids=affected
        )


def create_relation(session: Session, payload: RelationCreate) -> AssetRelation:
    source = require(session, Asset, payload.source_asset_id, "source asset")
    target = require(session, Asset, payload.target_asset_id, "target asset")
    if source.project_id != payload.project_id or target.project_id != payload.project_id:
        raise ServiceError(422, "both assets must belong to the relation project")
    relation = AssetRelation(
        id=new_id(),
        project_id=payload.project_id,
        source_asset_id=source.id,
        target_asset_id=target.id,
        relation_type=payload.relation_type.value,
        metadata_json=payload.metadata,
    )
    session.add(relation)
    try:
        session.flush()
        project = require(session, Project, payload.project_id, "project")
        rows = session.scalars(
            select(AssetRelation).where(AssetRelation.project_id == payload.project_id)
        ).all()
        ProjectStore(project.root_path).write_relations(
            [
                {
                    "id": row.id,
                    "source_asset_id": row.source_asset_id,
                    "target_asset_id": row.target_asset_id,
                    "relation_type": row.relation_type,
                    "metadata": row.metadata_json,
                }
                for row in rows
            ]
        )
        session.commit()
    except (StorageError, IntegrityError) as exc:
        session.rollback()
        raise ServiceError(409, "relation already exists" if isinstance(exc, IntegrityError) else str(exc)) from exc
    return relation


def _parse_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return utcnow()


_SCAN_LOCKS_GUARD = threading.Lock()
_SCAN_LOCKS: dict[str, threading.Lock] = {}


def scan_project(session: Session, project: Project) -> ScanReport:
    with _SCAN_LOCKS_GUARD:
        project_lock = _SCAN_LOCKS.setdefault(project.id, threading.Lock())
    with project_lock:
        return _scan_project(session, project)


def _scan_project(session: Session, project: Project) -> ScanReport:
    store = ProjectStore(project.root_path)
    assets, revisions, renditions, qa_runs, relations, errors = store.scan()
    indexed_assets = 0
    indexed_revisions = 0
    known_asset_ids: set[str] = set()
    known_revision_ids: set[str] = set()
    known_rendition_ids: set[str] = set()
    known_qa_ids: set[str] = set()
    known_relation_ids: set[str] = set()
    for data in assets:
        try:
            identifier = str(data["id"])
            known_asset_ids.add(identifier)
            asset = session.get(Asset, identifier)
            if asset is None:
                asset = session.scalar(
                    select(Asset).where(Asset.project_id == project.id, Asset.key == str(data["key"]))
                )
            if asset is None:
                asset = Asset(id=identifier, project_id=project.id)
                session.add(asset)
            asset.key = str(data["key"])
            asset.kind = str(data["kind"])
            asset.subtype = str(data["subtype"])
            asset.title = str(data["title"])
            asset.schema_ref = data.get("schema_ref")
            asset.tags = list(data.get("tags", []))
            asset.metadata_json = dict(data.get("metadata", {}))
            asset.content_status = str(data.get("content_status", ContentStatus.DRAFT.value))
            asset.generation_status = str(data.get("generation_status", GenerationStatus.IDLE.value))
            asset.publication_status = str(data.get("publication_status", PublicationStatus.UNPUBLISHED.value))
            asset.current_revision_id = data.get("current_revision_id")
            asset.latest_candidate_revision_id = data.get("latest_candidate_revision_id")
            asset.created_at = _parse_datetime(data.get("created_at"))
            asset.updated_at = _parse_datetime(data.get("updated_at"))
            indexed_assets += 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"asset {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    incoming_revision_pairs = {
        (str(data.get("asset_id", "")), int(data.get("sequence", 0))): str(data.get("id", ""))
        for data in revisions
        if data.get("asset_id") and data.get("id") and data.get("sequence") is not None
    }
    if known_asset_ids:
        superseded = [
            revision
            for revision in session.scalars(
                select(AssetRevision).where(AssetRevision.asset_id.in_(known_asset_ids))
            ).all()
            if (replacement := incoming_revision_pairs.get((revision.asset_id, revision.sequence)))
            and replacement != revision.id
        ]
        superseded_ids = {revision.id for revision in superseded}
        if superseded_ids:
            old_renditions = list(
                session.scalars(
                    select(Rendition).where(Rendition.revision_id.in_(superseded_ids))
                ).all()
            )
            old_rendition_ids = {rendition.id for rendition in old_renditions}
            if old_rendition_ids:
                for qa in session.scalars(
                    select(QARun).where(QARun.rendition_id.in_(old_rendition_ids))
                ).all():
                    session.delete(qa)
            for rendition in old_renditions:
                session.delete(rendition)
            for decision in session.scalars(
                select(ReviewDecision).where(ReviewDecision.revision_id.in_(superseded_ids))
            ).all():
                session.delete(decision)
            for job in session.scalars(
                select(GenerationJob).where(GenerationJob.result_revision_id.in_(superseded_ids))
            ).all():
                job.result_revision_id = None
            for revision in superseded:
                session.delete(revision)
            session.flush()
    for data in revisions:
        try:
            asset_id = str(data["asset_id"])
            if session.get(Asset, asset_id) is None:
                raise ValueError("revision references an unknown asset")
            identifier = str(data["id"])
            known_revision_ids.add(identifier)
            revision = session.get(AssetRevision, identifier)
            if revision is None:
                revision = AssetRevision(id=identifier, asset_id=asset_id)
                session.add(revision)
            revision.asset_id = asset_id
            revision.sequence = int(data["sequence"])
            revision.format = str(data["format"])
            revision.content_json = data.get("content")
            revision.content_hash = str(data["content_hash"])
            revision.parent_revision_id = data.get("parent_revision_id")
            revision.input_hash = str(data["input_hash"])
            revision.style_revision = data.get("style_revision")
            revision.prompt_recipe = data.get("prompt_recipe")
            revision.provider_snapshot = dict(data.get("provider_snapshot", {}))
            revision.file_path = str(data["file_path"])
            revision.review_status = str(data.get("review_status", "pending"))
            revision.reviewed_at = _parse_datetime(data["reviewed_at"]) if data.get("reviewed_at") else None
            revision.created_at = _parse_datetime(data.get("created_at"))
            indexed_revisions += 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"revision {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    for data in renditions:
        try:
            revision_id = str(data["revision_id"])
            if session.get(AssetRevision, revision_id) is None:
                raise ValueError("rendition references an unknown revision")
            identifier = str(data["id"])
            known_rendition_ids.add(identifier)
            rendition = session.get(Rendition, identifier)
            if rendition is None:
                rendition = Rendition(id=identifier, revision_id=revision_id)
                session.add(rendition)
            rendition.revision_id = revision_id
            rendition.media_type = str(data["media_type"])
            rendition.source_path = str(data["source_path"])
            rendition.normalized_path = data.get("normalized_path")
            rendition.target_path = data.get("target_path")
            rendition.sha256 = str(data["sha256"])
            rendition.width = int(data["width"]) if data.get("width") is not None else None
            rendition.height = int(data["height"]) if data.get("height") is not None else None
            rendition.byte_size = int(data.get("byte_size", 0))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"rendition {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    for data in qa_runs:
        try:
            rendition_id = str(data["rendition_id"])
            if session.get(Rendition, rendition_id) is None:
                raise ValueError("QA run references an unknown rendition")
            identifier = str(data["id"])
            known_qa_ids.add(identifier)
            qa_run = session.get(QARun, identifier)
            if qa_run is None:
                qa_run = QARun(id=identifier, rendition_id=rendition_id)
                session.add(qa_run)
            qa_run.rendition_id = rendition_id
            qa_run.verdict = str(data["verdict"])
            qa_run.checks_json = list(data.get("checks", []))
            qa_run.report_path = data.get("report_path")
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"QA run {data.get('id', '<unknown>')}: {exc}")
    assets_by_key = {
        asset.key: asset
        for asset in session.scalars(select(Asset).where(Asset.project_id == project.id)).all()
        if asset.id in known_asset_ids
    }
    for data in relations:
        try:
            source = session.get(Asset, str(data["source_asset_id"]))
            target = session.get(Asset, str(data["target_asset_id"]))
            if source is None or target is None:
                raise ValueError("relation references an unknown asset")
            identifier = str(data["id"])
            known_relation_ids.add(identifier)
            relation = session.get(AssetRelation, identifier)
            if relation is None:
                relation = AssetRelation(id=identifier, project_id=project.id)
                session.add(relation)
            relation.project_id = project.id
            relation.source_asset_id = source.id
            relation.target_asset_id = target.id
            relation.relation_type = str(data["relation_type"])
            relation.metadata_json = dict(data.get("metadata", {}))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"relation {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    for asset in session.scalars(
        select(Asset).where(
            Asset.project_id == project.id,
            Asset.current_revision_id.is_not(None),
        )
    ).all():
        revision = session.get(AssetRevision, asset.current_revision_id)
        if revision is None or revision.review_status != "approved":
            continue
        existing_decision = session.scalar(
            select(ReviewDecision).where(ReviewDecision.revision_id == revision.id).limit(1)
        )
        if existing_decision is None:
            session.add(
                ReviewDecision(
                    id=stable_id("review", revision.id, "approve"),
                    revision_id=revision.id,
                    asset_id=asset.id,
                    verdict=ReviewVerdict.APPROVE.value,
                    notes="Verified project baseline",
                    dependency_hash=dependency_hash(session, revision),
                    is_valid=True,
                    created_at=revision.reviewed_at or utcnow(),
                )
            )
    if not errors:
        project_assets = list(
            session.scalars(select(Asset).where(Asset.project_id == project.id)).all()
        )
        project_asset_ids = {asset.id for asset in project_assets}
        project_revisions = (
            list(
                session.scalars(
                    select(AssetRevision).where(AssetRevision.asset_id.in_(project_asset_ids))
                ).all()
            )
            if project_asset_ids
            else []
        )
        project_revision_ids = {revision.id for revision in project_revisions}
        project_renditions = (
            list(
                session.scalars(
                    select(Rendition).where(Rendition.revision_id.in_(project_revision_ids))
                ).all()
            )
            if project_revision_ids
            else []
        )
        project_rendition_ids = {rendition.id for rendition in project_renditions}
        stale_revision_ids = project_revision_ids - known_revision_ids
        stale_asset_ids = project_asset_ids - known_asset_ids

        for relation in session.scalars(
            select(AssetRelation).where(AssetRelation.project_id == project.id)
        ).all():
            if relation.id not in known_relation_ids:
                session.delete(relation)
        if project_rendition_ids:
            for qa in session.scalars(
                select(QARun).where(QARun.rendition_id.in_(project_rendition_ids))
            ).all():
                if qa.id not in known_qa_ids:
                    session.delete(qa)
        for rendition in project_renditions:
            if rendition.id not in known_rendition_ids:
                session.delete(rendition)
        if stale_revision_ids:
            for decision in session.scalars(
                select(ReviewDecision).where(ReviewDecision.revision_id.in_(stale_revision_ids))
            ).all():
                session.delete(decision)
            for job in session.scalars(
                select(GenerationJob).where(GenerationJob.result_revision_id.in_(stale_revision_ids))
            ).all():
                job.result_revision_id = None
        for revision in project_revisions:
            if revision.id in stale_revision_ids:
                session.delete(revision)
        for asset in project_assets:
            if asset.id in stale_asset_ids:
                session.delete(asset)
    session.commit()
    return ScanReport(
        project_id=project.id,
        assets_indexed=indexed_assets,
        revisions_indexed=indexed_revisions,
        errors=errors,
    )


def validate_plan(session: Session, payload: GenerationPlanCreate) -> list[dict[str, Any]]:
    require(session, Project, payload.project_id, "project")
    require(session, ProviderProfile, payload.provider_profile_id, "provider profile")
    ids = [task.id for task in payload.tasks]
    if len(ids) != len(set(ids)):
        raise ServiceError(422, "generation task ids must be unique")
    task_ids = set(ids)
    tasks = {task.id: task for task in payload.tasks}
    for task in payload.tasks:
        asset = require(session, Asset, task.asset_id, "task asset")
        if asset.project_id != payload.project_id:
            raise ServiceError(422, f"task {task.id} references an asset in another project")
        missing = set(task.depends_on) - task_ids
        if missing:
            raise ServiceError(422, f"task {task.id} has unknown dependencies: {sorted(missing)}")
        if task.id in task.depends_on:
            raise ServiceError(422, f"task {task.id} cannot depend on itself")
        if task.kind == TaskKind.TEXT and task.output_schema is None and not asset.schema_ref:
            raise ServiceError(422, f"text task {task.id} requires an inline or asset JSON schema")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id in visiting:
            raise ServiceError(422, "generation dependency graph must be acyclic")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in tasks[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task_id in task_ids:
        visit(task_id)
    return [task.model_dump(mode="json", by_alias=True) for task in payload.tasks]


def create_plan(session: Session, payload: GenerationPlanCreate) -> GenerationPlan:
    tasks = validate_plan(session, payload)
    profile = require(session, ProviderProfile, payload.provider_profile_id, "provider profile")
    estimated_cost: float | None = None
    if profile.pricing:
        total = 0.0
        known = True
        for task in payload.tasks:
            price = profile.pricing.get("image_call" if task.kind != TaskKind.TEXT else "text_call")
            if price is None:
                known = False
                break
            total += float(price)
        if known:
            estimated_cost = total
    plan = GenerationPlan(
        id=new_id(),
        project_id=payload.project_id,
        provider_profile_id=payload.provider_profile_id,
        status="draft",
        tasks_json=tasks,
        estimated_calls=len(tasks),
        estimated_cost=estimated_cost,
    )
    session.add(plan)
    session.commit()
    return plan


def confirm_plan(session: Session, plan: GenerationPlan, *, credentials_available: bool) -> list[GenerationJob]:
    if plan.status != "draft":
        return session.scalars(select(GenerationJob).where(GenerationJob.plan_id == plan.id)).all()
    profile = require(session, ProviderProfile, plan.provider_profile_id, "provider profile")
    initial_status = (
        GenerationStatus.QUEUED.value
        if profile.kind == "fake" or credentials_available
        else GenerationStatus.CREDENTIALS_LOCKED.value
    )
    jobs = []
    for task in plan.tasks_json:
        job = GenerationJob(
            id=new_id(),
            plan_id=plan.id,
            project_id=plan.project_id,
            provider_profile_id=plan.provider_profile_id,
            task_id=str(task["id"]),
            task_kind=str(task["kind"]),
            request_json=task,
            status=initial_status,
        )
        jobs.append(job)
        session.add(job)
        asset = session.get(Asset, str(task["asset_id"]))
        if asset:
            asset.generation_status = GenerationStatus.PLANNED.value
    plan.status = "confirmed"
    plan.confirmed_at = utcnow()
    session.commit()
    return jobs


def dependency_hash(session: Session, revision: AssetRevision) -> str:
    relations = session.scalars(
        select(AssetRelation).where(AssetRelation.source_asset_id == revision.asset_id)
    ).all()
    dependencies: list[dict[str, Any]] = []
    for relation in relations:
        target = session.get(Asset, relation.target_asset_id)
        dependencies.append(
            {
                "relation": relation.relation_type,
                "target": relation.target_asset_id,
                "revision": target.current_revision_id if target else None,
            }
        )
    return sha256_bytes(
        canonical_json(
            {
                "revision": revision.id,
                "input_hash": revision.input_hash,
                "style_revision": revision.style_revision,
                "prompt_recipe": revision.prompt_recipe,
                "provider": revision.provider_snapshot,
                "dependencies": sorted(dependencies, key=lambda item: (item["relation"], item["target"])),
            }
        )
    )


def create_review(session: Session, payload: ReviewCreate) -> ReviewDecision:
    revision = require(session, AssetRevision, payload.revision_id, "revision")
    asset = require(session, Asset, revision.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    if payload.verdict == ReviewVerdict.APPROVE:
        renditions = session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all()
        for rendition in renditions:
            latest_qa = session.scalar(
                select(QARun)
                .where(QARun.rendition_id == rendition.id)
                .order_by(QARun.created_at.desc())
                .limit(1)
            )
            if latest_qa is None or latest_qa.verdict == QAVerdict.FAIL.value:
                raise ServiceError(409, "hard QA must pass before approving a media revision")
        _sync_approved_relations(session, project=project, asset=asset, content=revision.content_json)
    session.execute(
        ReviewDecision.__table__.update()
        .where(ReviewDecision.asset_id == asset.id, ReviewDecision.is_valid.is_(True))
        .values(is_valid=False)
    )
    decision = ReviewDecision(
        id=new_id(),
        revision_id=revision.id,
        asset_id=asset.id,
        verdict=payload.verdict.value,
        notes=payload.notes,
        dependency_hash=dependency_hash(session, revision),
        is_valid=True,
    )
    session.add(decision)
    revision.review_status = (
        "approved" if payload.verdict == ReviewVerdict.APPROVE else "rejected"
    )
    revision.reviewed_at = utcnow()
    if payload.verdict == ReviewVerdict.APPROVE:
        asset.current_revision_id = revision.id
        asset.content_status = ContentStatus.APPROVED.value
        asset.publication_status = PublicationStatus.READY.value
        asset.title = _revision_title(asset, revision.content_json)
        dependent_assets = session.scalars(
            select(Asset)
            .join(AssetRelation, AssetRelation.source_asset_id == Asset.id)
            .where(AssetRelation.target_asset_id == asset.id)
        ).all()
        for dependent in dependent_assets:
            session.execute(
                ReviewDecision.__table__.update()
                .where(ReviewDecision.asset_id == dependent.id, ReviewDecision.is_valid.is_(True))
                .values(is_valid=False)
            )
            dependent.publication_status = PublicationStatus.BLOCKED.value
    else:
        if asset.current_revision_id is None:
            asset.content_status = ContentStatus.REJECTED.value
            asset.publication_status = PublicationStatus.BLOCKED.value
        rendition = revision.content_json.get("rendition") if isinstance(revision.content_json, dict) else None
        rejected_media_path = rendition.get("normalized_path") if isinstance(rendition, dict) else None
        if isinstance(rejected_media_path, str) and rejected_media_path:
            store = ProjectStore(project.root_path)
            candidate = safe_join(store.root, rejected_media_path)
            if candidate.is_file():
                rejected = store.output / "rejected" / revision.id / candidate.name
                rejected.parent.mkdir(parents=True, exist_ok=True)
                if not rejected.exists():
                    shutil.copy2(candidate, rejected)
    session.flush()
    asset.latest_candidate_revision_id = session.scalar(
        select(AssetRevision.id)
        .where(
            AssetRevision.asset_id == asset.id,
            AssetRevision.review_status == "pending",
        )
        .order_by(AssetRevision.sequence.desc())
        .limit(1)
    )
    asset.updated_at = utcnow()
    store = ProjectStore(project.root_path)
    store.write_review(
        kind=asset.kind,
        key=asset.key,
        review={
            "id": decision.id,
            "revision_id": decision.revision_id,
            "verdict": decision.verdict,
            "notes": decision.notes,
            "dependency_hash": decision.dependency_hash,
            "created_at": decision.created_at.isoformat(),
        },
    )
    store.update_asset_approval(
        kind=asset.kind,
        key=asset.key,
        title=asset.title,
        content_status=asset.content_status,
        publication_status=asset.publication_status,
        current_revision_id=asset.current_revision_id,
        latest_candidate_revision_id=asset.latest_candidate_revision_id,
        updated_at=asset.updated_at.isoformat(),
    )
    if (
        payload.verdict == ReviewVerdict.APPROVE
        and asset.kind == "design"
        and asset.subtype == "style_bible"
        and isinstance(revision.content_json, str)
    ):
        store.publish_style_bible(
            markdown=revision.content_json,
            revision_id=revision.id,
            markdown_path=str(asset.metadata_json.get("style_markdown_path", "production/style-bible.md")),
            profile_path=str(asset.metadata_json.get("style_profile_path", "production/style-bible.json")),
        )
    session.commit()
    return decision


def run_qa(session: Session, payload: QARunCreate) -> QARun:
    rendition = require(session, Rendition, payload.rendition_id, "rendition")
    revision = require(session, AssetRevision, rendition.revision_id, "revision")
    asset = require(session, Asset, revision.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    store = ProjectStore(project.root_path)
    image_path = safe_join(store.root, rendition.normalized_path or rendition.source_path)
    verdict, checks = inspect_image(
        image_path,
        expected_width=payload.expected_width,
        expected_height=payload.expected_height,
        require_alpha=payload.require_alpha,
        max_bytes=payload.max_bytes,
    )
    qa = QARun(
        id=new_id(),
        rendition_id=rendition.id,
        verdict=verdict,
        checks_json=checks,
        report_path=None,
    )
    session.add(qa)
    session.flush()
    report_path = store.qa_report_path(qa.id)
    atomic_write_json(
        report_path,
        {
            "id": qa.id,
            "rendition_id": rendition.id,
            "verdict": verdict,
            "checks": checks,
            "report_path": relative_to_root(store.root, report_path),
            "created_at": qa.created_at.isoformat(),
        },
        immutable=True,
    )
    qa.report_path = relative_to_root(store.root, report_path)
    store.write_qa_record(
        {
            "id": qa.id,
            "rendition_id": rendition.id,
            "verdict": verdict,
            "checks": checks,
            "report_path": qa.report_path,
            "created_at": qa.created_at.isoformat(),
        }
    )
    session.commit()
    return qa


def create_release(session: Session, payload: ReleaseCreate) -> Release:
    project = require(session, Project, payload.project_id, "project")
    store = ProjectStore(project.root_path)
    assets = session.scalars(
        select(Asset).where(
            Asset.project_id == project.id,
            Asset.current_revision_id.is_not(None),
            Asset.content_status == ContentStatus.APPROVED.value,
        )
    ).all()
    entries: list[dict[str, Any]] = []
    released_assets: list[Asset] = []
    for asset in assets:
        revision = session.get(AssetRevision, asset.current_revision_id)
        if revision is None:
            continue
        approval = session.scalar(
            select(ReviewDecision)
            .where(
                ReviewDecision.revision_id == revision.id,
                ReviewDecision.verdict == ReviewVerdict.APPROVE.value,
                ReviewDecision.is_valid.is_(True),
            )
            .order_by(ReviewDecision.created_at.desc())
            .limit(1)
        )
        if approval is None or approval.dependency_hash != dependency_hash(session, revision):
            asset.publication_status = PublicationStatus.BLOCKED.value
            continue
        rendition_entries: list[dict[str, Any]] = []
        renditions = session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all()
        hard_failure = False
        for rendition in renditions:
            qa = session.scalar(
                select(QARun)
                .where(QARun.rendition_id == rendition.id)
                .order_by(QARun.created_at.desc())
                .limit(1)
            )
            if qa is None or qa.verdict == QAVerdict.FAIL.value:
                hard_failure = True
                break
            published_path = rendition.normalized_path or rendition.source_path
            backup_path = None
            if payload.publish_media and rendition.target_path:
                candidate = safe_join(store.root, rendition.normalized_path or rendition.source_path)
                published_path, backup_path = store.atomic_publish(candidate, rendition.target_path)
            rendition_entries.append(
                {
                    "id": rendition.id,
                    "media_type": rendition.media_type,
                    "path": published_path,
                    "sha256": rendition.sha256,
                    "width": rendition.width,
                    "height": rendition.height,
                    "backup_path": backup_path,
                }
            )
        if hard_failure:
            asset.publication_status = PublicationStatus.BLOCKED.value
            continue
        entries.append(
            {
                "key": asset.key,
                "kind": asset.kind,
                "subtype": asset.subtype,
                "revision_id": revision.id,
                "content_hash": revision.content_hash,
                "content": revision.content_json,
                "renditions": rendition_entries,
            }
        )
        released_assets.append(asset)
    release = Release(
        id=new_id(),
        project_id=project.id,
        name=payload.name,
        manifest_path="pending",
        manifest_hash="pending",
        asset_count=len(entries),
        created_at=utcnow(),
    )
    manifest = {
        "format_version": 1,
        "release_id": release.id,
        "name": release.name,
        "project_id": project.id,
        "created_at": release.created_at.isoformat(),
        "assets": sorted(entries, key=lambda entry: entry["key"]),
    }
    release.manifest_path, release.manifest_hash = store.write_release(release.id, manifest)
    session.add(release)
    for asset in released_assets:
        asset.publication_status = PublicationStatus.PUBLISHED.value
        store.update_asset_state(
            kind=asset.kind,
            key=asset.key,
            content_status=asset.content_status,
            publication_status=asset.publication_status,
            current_revision_id=asset.current_revision_id,
            latest_candidate_revision_id=asset.latest_candidate_revision_id,
        )
    session.commit()
    return release
