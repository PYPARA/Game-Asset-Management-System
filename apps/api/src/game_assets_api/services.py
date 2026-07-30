from __future__ import annotations

import copy
import math
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import jsonschema
from PIL import Image, UnidentifiedImageError
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
    RunStage,
    ScanReport,
    TaskKind,
)
from .models import (
    Asset,
    AssetRelation,
    AssetRevision,
    Artifact,
    GenerationJob,
    GenerationPlan,
    Project,
    ProviderProfile,
    QARun,
    Release,
    Rendition,
    ReviewArtifact,
    ReviewDecision,
    new_id,
    utcnow,
)
from .provider_catalog import (
    default_route,
    model_is_compatible,
    provider_snapshot,
    required_modality,
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
    sha256_file,
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
    scanned = store.scan_full()
    assets = scanned.assets
    revisions = scanned.revisions
    renditions = scanned.renditions
    qa_runs = scanned.qa_runs
    relations = scanned.relations
    artifacts = scanned.artifacts
    reviews = scanned.reviews
    releases = scanned.releases
    errors = scanned.errors
    indexed_assets = 0
    indexed_revisions = 0
    known_asset_ids: set[str] = set()
    known_revision_ids: set[str] = set()
    known_rendition_ids: set[str] = set()
    known_qa_ids: set[str] = set()
    known_relation_ids: set[str] = set()
    known_artifact_ids: set[str] = set()
    known_review_ids: set[str] = set()
    known_release_ids: set[str] = set()
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
    for data in sorted(
        revisions,
        key=lambda item: (str(item.get("asset_id", "")), int(item.get("sequence", 0))),
    ):
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
            session.flush()
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"revision {data.get('id', '<unknown>')}: {exc}")
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

    artifact_parents: dict[str, str | None] = {}
    for data in artifacts:
        try:
            revision_id = str(data["revision_id"])
            if session.get(AssetRevision, revision_id) is None:
                raise ValueError("artifact references an unknown revision")
            identifier = str(data["id"])
            known_artifact_ids.add(identifier)
            artifact = session.get(Artifact, identifier)
            if artifact is None:
                artifact = Artifact(id=identifier, project_id=project.id, revision_id=revision_id)
                session.add(artifact)
            artifact.project_id = project.id
            artifact.revision_id = revision_id
            artifact.role = str(data["role"])
            artifact.kind = str(data["kind"])
            artifact.media_type = str(data["media_type"])
            artifact.path = str(data["path"])
            artifact.sha256 = str(data["sha256"])
            artifact.byte_size = int(data["byte_size"])
            artifact.width = int(data["width"]) if data.get("width") is not None else None
            artifact.height = int(data["height"]) if data.get("height") is not None else None
            artifact.parent_artifact_id = None
            artifact.tool_json = dict(data.get("tool", {}))
            artifact.file_path = str(data["file_path"])
            artifact.created_at = _parse_datetime(data.get("created_at"))
            artifact_parents[identifier] = (
                str(data["parent_artifact_id"]) if data.get("parent_artifact_id") else None
            )
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"artifact {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    for artifact_id, parent_id in artifact_parents.items():
        artifact = session.get(Artifact, artifact_id)
        if artifact is None or parent_id is None:
            continue
        parent = session.get(Artifact, parent_id)
        if (
            parent is None
            or parent.id == artifact.id
            or parent.project_id != artifact.project_id
            or parent.revision_id != artifact.revision_id
        ):
            errors.append(f"artifact {artifact_id}: invalid parent artifact {parent_id}")
            continue
        artifact.parent_artifact_id = parent_id

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
            qa_run.created_at = _parse_datetime(data.get("created_at"))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"QA run {data.get('id', '<unknown>')}: {exc}")
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

    scanned_decisions: list[ReviewDecision] = []
    for data in reviews:
        try:
            revision = session.get(AssetRevision, str(data["revision_id"]))
            if revision is None:
                raise ValueError("review references an unknown revision")
            asset = session.get(Asset, revision.asset_id)
            if asset is None or asset.project_id != project.id:
                raise ValueError("review references an asset outside the project")
            if data.get("asset_id") and str(data["asset_id"]) != asset.id:
                raise ValueError("review asset does not match its revision")
            identifier = str(data["id"])
            known_review_ids.add(identifier)
            decision = session.get(ReviewDecision, identifier)
            if decision is None:
                decision = ReviewDecision(id=identifier, revision_id=revision.id, asset_id=asset.id)
                session.add(decision)
            decision.revision_id = revision.id
            decision.asset_id = asset.id
            decision.verdict = str(data["verdict"])
            decision.notes = data.get("notes")
            decision.dependency_hash = str(data["dependency_hash"])
            decision.is_valid = False
            decision.created_at = _parse_datetime(data.get("created_at"))
            for binding in list(decision.artifact_bindings):
                session.delete(binding)
            for binding_data in data.get("artifacts", []):
                if not isinstance(binding_data, dict):
                    raise ValueError("review artifact bindings must be objects")
                artifact = session.get(Artifact, str(binding_data["artifact_id"]))
                if artifact is None or artifact.project_id != project.id:
                    raise ValueError("review references an unknown artifact")
                role = str(binding_data["role"])
                if artifact.revision_id != revision.id or artifact.role != role:
                    raise ValueError("review artifact does not match its revision or role")
                binding_hash = str(binding_data["sha256"])
                if artifact.sha256 != binding_hash:
                    raise ValueError("review artifact hash does not match its metadata")
                decision.artifact_bindings.append(
                    ReviewArtifact(
                        id=stable_id(
                            "review_artifact",
                            identifier,
                            artifact.id,
                            role,
                        ),
                        artifact_id=artifact.id,
                        role=role,
                        sha256=binding_hash,
                    )
                )
            scanned_decisions.append(decision)
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"review {data.get('id', '<unknown>')}: {exc}")
    session.flush()

    latest_by_asset: dict[str, ReviewDecision] = {}
    for decision in scanned_decisions:
        previous = latest_by_asset.get(decision.asset_id)
        if previous is None or (decision.created_at, decision.id) > (
            previous.created_at,
            previous.id,
        ):
            latest_by_asset[decision.asset_id] = decision
    for asset_id, decision in latest_by_asset.items():
        revision = session.get(AssetRevision, decision.revision_id)
        asset = session.get(Asset, asset_id)
        if revision is None or asset is None:
            continue
        decision.is_valid = decision.verdict == ReviewVerdict.REJECT.value or (
            asset.current_revision_id == revision.id
            and decision.dependency_hash == dependency_hash(session, revision)
        )
        if (
            decision.verdict == ReviewVerdict.APPROVE.value
            and not decision.is_valid
            and asset.current_revision_id == revision.id
        ):
            asset.publication_status = PublicationStatus.BLOCKED.value

    for data in releases:
        try:
            identifier = str(data["id"])
            known_release_ids.add(identifier)
            release = session.get(Release, identifier)
            if release is None:
                release = Release(id=identifier, project_id=project.id)
                session.add(release)
            release.project_id = project.id
            release.name = str(data["name"])
            release.manifest_path = str(data["manifest_path"])
            release.manifest_hash = str(data["manifest_hash"])
            release.asset_count = int(data["asset_count"])
            release.created_at = _parse_datetime(data.get("created_at"))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"release {data.get('id', '<unknown>')}: {exc}")
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

        for decision in session.scalars(
            select(ReviewDecision).where(ReviewDecision.asset_id.in_(project_asset_ids))
        ).all():
            if decision.id not in known_review_ids:
                session.delete(decision)
        stale_artifacts = [
            artifact
            for artifact in session.scalars(
                select(Artifact).where(Artifact.project_id == project.id)
            ).all()
            if artifact.id not in known_artifact_ids
        ]
        for artifact in sorted(
            stale_artifacts, key=lambda item: item.parent_artifact_id is None
        ):
            session.delete(artifact)
        for release in session.scalars(
            select(Release).where(Release.project_id == project.id)
        ).all():
            if release.id not in known_release_ids:
                session.delete(release)
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
    if payload.provider_profile_id:
        require(session, ProviderProfile, payload.provider_profile_id, "provider profile")
    ids = [task.id for task in payload.tasks]
    if len(ids) != len(set(ids)):
        raise ServiceError(422, "generation task ids must be unique")
    task_ids = set(ids)
    tasks = {task.id: task for task in payload.tasks}
    resolved: list[dict[str, Any]] = []
    for task in payload.tasks:
        asset = require(session, Asset, task.asset_id, "task asset")
        if asset.project_id != payload.project_id:
            raise ServiceError(422, f"task {task.id} references an asset in another project")
        missing = set(task.depends_on) - task_ids
        if missing:
            raise ServiceError(422, f"task {task.id} has unknown dependencies: {sorted(missing)}")
        if task.id in task.depends_on:
            raise ServiceError(422, f"task {task.id} cannot depend on itself")
        if task.reference_task_id:
            if task.reference_task_id not in task_ids:
                raise ServiceError(
                    422,
                    f"task {task.id} references unknown task {task.reference_task_id}",
                )
            if task.reference_task_id not in task.depends_on:
                raise ServiceError(
                    422,
                    f"task {task.id} must depend on its reference task",
                )
        if task.kind == TaskKind.TEXT and task.output_schema is None and not asset.schema_ref:
            raise ServiceError(422, f"text task {task.id} requires an inline or asset JSON schema")
        if task.kind == TaskKind.IMAGE_EDIT and not (
            task.reference_path or task.reference_task_id or task.depends_on
        ):
            raise ServiceError(422, f"image edit task {task.id} requires a reference input")
        default_provider_id, default_model = default_route(session, task.kind)
        provider_id = task.provider_profile_id or payload.provider_profile_id or default_provider_id
        if not provider_id:
            raise ServiceError(422, f"task {task.id} requires a provider route")
        profile = require(session, ProviderProfile, provider_id, "task provider profile")
        if not profile.is_active:
            raise ServiceError(422, f"task {task.id} references an archived provider")
        if task.model:
            model = task.model.strip()
        elif task.provider_profile_id or payload.provider_profile_id:
            model = profile.text_model if task.kind == TaskKind.TEXT else profile.image_model
        else:
            model = (default_model or "").strip()
        if not model:
            raise ServiceError(422, f"task {task.id} requires a provider model")
        modality = required_modality(task.kind)
        if not model_is_compatible(profile, model, modality):
            raise ServiceError(
                422,
                f"task {task.id} model {model} is not classified for {modality}",
            )
        task_data = task.model_dump(mode="json", by_alias=True)
        task_data["provider_profile_id"] = profile.id
        task_data["model"] = model
        resolved.append(task_data)
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
    return resolved


def _task_price(profile: ProviderProfile, task_kind: str) -> float | None:
    if not profile.pricing:
        return None
    key = "text_call" if task_kind == TaskKind.TEXT.value else "image_call"
    if task_kind == TaskKind.IMAGE_EDIT.value and "image_edit_call" in profile.pricing:
        key = "image_edit_call"
    value = profile.pricing.get(key)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def create_plan(session: Session, payload: GenerationPlanCreate) -> GenerationPlan:
    tasks = validate_plan(session, payload)
    total = 0.0
    known = True
    for task in tasks:
        profile = require(
            session,
            ProviderProfile,
            str(task["provider_profile_id"]),
            "task provider profile",
        )
        price = _task_price(profile, str(task["kind"]))
        if price is None:
            known = False
        else:
            total += price
    estimated_cost: float | None = total if known else None
    suggested_extra_calls = max(2, math.ceil(len(tasks) * 0.2))
    extra_call_budget = (
        suggested_extra_calls
        if payload.extra_call_budget is None
        else payload.extra_call_budget
    )
    plan = GenerationPlan(
        id=new_id(),
        project_id=payload.project_id,
        provider_profile_id=payload.provider_profile_id or str(tasks[0]["provider_profile_id"]),
        name=payload.name.strip(),
        status="draft",
        tasks_json=tasks,
        estimated_calls=len(tasks),
        estimated_cost=estimated_cost,
        suggested_extra_calls=suggested_extra_calls,
        extra_call_budget=extra_call_budget,
        max_paid_remediation_rounds=payload.max_paid_remediation_rounds,
        max_transport_retries=payload.max_transport_retries,
        max_concurrency=payload.max_concurrency,
    )
    session.add(plan)
    session.commit()
    return plan


def confirm_plan(
    session: Session,
    plan: GenerationPlan,
    *,
    available_provider_ids: set[str],
) -> list[GenerationJob]:
    if plan.status != "draft":
        return session.scalars(select(GenerationJob).where(GenerationJob.plan_id == plan.id)).all()
    jobs = []
    for task in plan.tasks_json:
        provider_id = str(task.get("provider_profile_id") or plan.provider_profile_id)
        profile = require(session, ProviderProfile, provider_id, "task provider profile")
        if not profile.is_active:
            raise ServiceError(409, f"provider {profile.name} was archived before confirmation")
        initial_status = (
            GenerationStatus.QUEUED.value
            if profile.kind == "fake" or profile.id in available_provider_ids
            else GenerationStatus.CREDENTIALS_LOCKED.value
        )
        model = str(
            task.get("model")
            or (profile.text_model if task.get("kind") == TaskKind.TEXT.value else profile.image_model)
        )
        task["provider_profile_id"] = profile.id
        task["model"] = model
        job = GenerationJob(
            id=new_id(),
            plan_id=plan.id,
            project_id=plan.project_id,
            provider_profile_id=profile.id,
            task_id=str(task["id"]),
            task_kind=str(task["kind"]),
            request_json=task,
            provider_snapshot_json=provider_snapshot(profile, model=model),
            status=initial_status,
            stage=RunStage.QUEUED.value,
        )
        jobs.append(job)
        session.add(job)
        asset = session.get(Asset, str(task["asset_id"]))
        if asset:
            asset.generation_status = GenerationStatus.PLANNED.value
    plan.status = (
        GenerationStatus.CREDENTIALS_LOCKED.value
        if jobs and all(job.status == GenerationStatus.CREDENTIALS_LOCKED.value for job in jobs)
        else "confirmed"
    )
    plan.confirmed_at = utcnow()
    from .production import record_run_event

    confirmation = record_run_event(
        session,
        plan_id=plan.id,
        project_id=plan.project_id,
        event_type="plan.confirmed",
        data={
            "base_calls": plan.estimated_calls,
            "estimated_base_cost": plan.estimated_cost,
            "suggested_extra_calls": plan.suggested_extra_calls,
            "extra_call_budget": plan.extra_call_budget,
            "max_paid_remediation_rounds": plan.max_paid_remediation_rounds,
            "max_transport_retries": plan.max_transport_retries,
            "max_concurrency": plan.max_concurrency,
            "routes": [
                {
                    "task_id": job.task_id,
                    "provider_profile_id": job.provider_profile_id,
                    "model": job.request_json.get("model"),
                }
                for job in jobs
            ],
        },
    )
    for job in jobs:
        record_run_event(
            session,
            plan_id=plan.id,
            project_id=plan.project_id,
            job_id=job.id,
            asset_id=str(job.request_json.get("asset_id")),
            event_type="run.queued",
            stage=job.stage,
            causation_id=confirmation.id,
            data={
                "task_id": job.task_id,
                "status": job.status,
                "provider_profile_id": job.provider_profile_id,
                "model": job.request_json.get("model"),
            },
        )
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


def _image_source_metadata(path: Path) -> tuple[str, str, int, int]:
    formats = {
        "PNG": ("png", "image/png"),
        "JPEG": ("jpg", "image/jpeg"),
        "WEBP": ("webp", "image/webp"),
        "GIF": ("gif", "image/gif"),
    }
    try:
        with Image.open(path) as image:
            image.load()
            extension, media_type = formats.get(str(image.format), ("bin", "application/octet-stream"))
            return extension, media_type, image.width, image.height
    except (UnidentifiedImageError, OSError) as exc:
        raise ServiceError(409, "candidate source image is not decodable") from exc


def _runtime_extension(media_type: str) -> str:
    return {
        "image/webp": "webp",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/gif": "gif",
    }.get(media_type, "bin")


def _approve_media_revision(
    session: Session,
    *,
    revision: AssetRevision,
    asset: Asset,
    project: Project,
    payload: ReviewCreate,
) -> ReviewDecision:
    renditions = list(
        session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all()
    )
    if len(renditions) != 1:
        raise ServiceError(409, "a media revision must contain exactly one rendition")
    candidate_rendition = renditions[0]
    latest_qa = session.scalar(
        select(QARun)
        .where(QARun.rendition_id == candidate_rendition.id)
        .order_by(QARun.created_at.desc())
        .limit(1)
    )
    if latest_qa is None or latest_qa.verdict not in {
        QAVerdict.PASS.value,
        QAVerdict.WARNING.value,
    }:
        raise ServiceError(409, "hard QA must pass before approving a media revision")

    store = ProjectStore(project.root_path)
    source_candidate = safe_join(store.root, candidate_rendition.source_path)
    runtime_candidate = safe_join(
        store.root, candidate_rendition.normalized_path or candidate_rendition.source_path
    )
    source_extension, source_media_type, source_width, source_height = _image_source_metadata(
        source_candidate
    )
    source_hash = sha256_file(source_candidate)
    source_path, source_hash, source_size = store.promote_blob(
        source_candidate,
        directory="production/sources",
        extension=source_extension,
        expected_hash=source_hash,
    )
    runtime_path, runtime_hash, runtime_size = store.promote_blob(
        runtime_candidate,
        directory="approved/objects",
        extension=_runtime_extension(candidate_rendition.media_type),
        expected_hash=candidate_rendition.sha256,
    )
    revalidated_verdict, revalidated_checks = inspect_image(
        safe_join(store.root, runtime_path),
        expected_width=candidate_rendition.width,
        expected_height=candidate_rendition.height,
        max_bytes=runtime_size,
    )
    if revalidated_verdict == QAVerdict.FAIL.value:
        raise ServiceError(409, "promoted media failed hard QA revalidation")

    promoted_revision_id = stable_id(
        "revision", revision.id, "promotion", source_hash, runtime_hash
    )
    source_artifact_id = stable_id("artifact", promoted_revision_id, "source", source_hash)
    runtime_artifact_id = stable_id("artifact", promoted_revision_id, "runtime", runtime_hash)
    promoted_rendition_id = stable_id("rendition", promoted_revision_id, runtime_hash)
    now = utcnow()
    sequence = (
        session.scalar(
            select(func.max(AssetRevision.sequence)).where(AssetRevision.asset_id == asset.id)
        )
        or 0
    ) + 1

    promoted_content = copy.deepcopy(revision.content_json)
    if not isinstance(promoted_content, dict) or not isinstance(
        promoted_content.get("rendition"), dict
    ):
        raise ServiceError(409, "media revision is missing rendition content")
    promoted_rendition_data = dict(promoted_content["rendition"])
    promoted_rendition_data.update(
        {
            "source_path": source_path,
            "source_sha256": source_hash,
            "source_artifact_id": source_artifact_id,
            "normalized_path": runtime_path,
            "artifact_id": runtime_artifact_id,
            "sha256": runtime_hash,
            "byte_size": runtime_size,
        }
    )
    promoted_content["rendition"] = promoted_rendition_data
    promoted_revision = AssetRevision(
        id=promoted_revision_id,
        asset_id=asset.id,
        sequence=sequence,
        format=revision.format,
        content_json=promoted_content,
        content_hash=sha256_bytes(canonical_json(promoted_content)),
        parent_revision_id=revision.id,
        input_hash=revision.input_hash,
        style_revision=revision.style_revision,
        prompt_recipe=revision.prompt_recipe,
        provider_snapshot=revision.provider_snapshot,
        file_path="pending",
        review_status="approved",
        reviewed_at=now,
        created_at=now,
    )

    source_artifact = Artifact(
        id=source_artifact_id,
        project_id=project.id,
        revision_id=promoted_revision_id,
        role="source",
        kind="provider_output",
        media_type=source_media_type,
        path=source_path,
        sha256=source_hash,
        byte_size=source_size,
        width=source_width,
        height=source_height,
        parent_artifact_id=None,
        tool_json={
            "name": "provider",
            "version": str(
                revision.provider_snapshot.get("image_model")
                or revision.provider_snapshot.get("kind")
                or "unknown"
            ),
        },
        file_path="pending",
        created_at=now,
    )
    runtime_artifact = Artifact(
        id=runtime_artifact_id,
        project_id=project.id,
        revision_id=promoted_revision_id,
        role="runtime",
        kind="normalized_media",
        media_type=candidate_rendition.media_type,
        path=runtime_path,
        sha256=runtime_hash,
        byte_size=runtime_size,
        width=candidate_rendition.width,
        height=candidate_rendition.height,
        parent_artifact_id=source_artifact_id,
        tool_json={"name": "gams.normalize_image", "version": "1"},
        file_path="pending",
        created_at=now,
    )
    promoted_rendition = Rendition(
        id=promoted_rendition_id,
        revision_id=promoted_revision_id,
        media_type=candidate_rendition.media_type,
        source_path=source_path,
        normalized_path=runtime_path,
        target_path=candidate_rendition.target_path,
        sha256=runtime_hash,
        width=candidate_rendition.width,
        height=candidate_rendition.height,
        byte_size=runtime_size,
        created_at=now,
    )
    promoted_qa_id = stable_id("qa", promoted_rendition_id, runtime_hash, latest_qa.id)
    promoted_qa = QARun(
        id=promoted_qa_id,
        rendition_id=promoted_rendition_id,
        verdict=revalidated_verdict,
        checks_json=revalidated_checks,
        report_path=f"history/qa/{promoted_qa_id}.json",
        created_at=now,
    )
    decision = ReviewDecision(
        id=new_id(),
        revision_id=promoted_revision_id,
        asset_id=asset.id,
        verdict=ReviewVerdict.APPROVE.value,
        notes=payload.notes,
        dependency_hash=dependency_hash(session, promoted_revision),
        is_valid=True,
        created_at=now,
    )
    artifact_bindings = [
        {"artifact_id": source_artifact.id, "role": "source", "sha256": source_artifact.sha256},
        {"artifact_id": runtime_artifact.id, "role": "runtime", "sha256": runtime_artifact.sha256},
    ]
    artifact_records = [
        {
            "object_type": "artifact",
            "format_version": 1,
            "id": artifact.id,
            "project_id": artifact.project_id,
            "revision_id": artifact.revision_id,
            "role": artifact.role,
            "kind": artifact.kind,
            "media_type": artifact.media_type,
            "path": artifact.path,
            "sha256": artifact.sha256,
            "byte_size": artifact.byte_size,
            "width": artifact.width,
            "height": artifact.height,
            "parent_artifact_id": artifact.parent_artifact_id,
            "tool": artifact.tool_json,
            "created_at": artifact.created_at.isoformat(),
        }
        for artifact in (source_artifact, runtime_artifact)
    ]
    revision_record = _revision_file(promoted_revision)
    revision_record["promotion_of_revision_id"] = revision.id
    qa_record = {
        "id": promoted_qa.id,
        "rendition_id": promoted_qa.rendition_id,
        "verdict": promoted_qa.verdict,
        "checks": promoted_qa.checks_json,
        "report_path": promoted_qa.report_path,
        "created_at": promoted_qa.created_at.isoformat(),
    }
    review_record = {
        "id": decision.id,
        "asset_id": asset.id,
        "revision_id": decision.revision_id,
        "source_revision_id": revision.id,
        "verdict": decision.verdict,
        "notes": decision.notes,
        "dependency_hash": decision.dependency_hash,
        "artifacts": artifact_bindings,
        "created_at": decision.created_at.isoformat(),
    }
    next_candidate_id = session.scalar(
        select(AssetRevision.id)
        .where(
            AssetRevision.asset_id == asset.id,
            AssetRevision.review_status == "pending",
            AssetRevision.id != revision.id,
        )
        .order_by(AssetRevision.sequence.desc())
        .limit(1)
    )
    title = _revision_title(asset, promoted_content)
    written = store.commit_media_approval(
        key=asset.key,
        history_records=[*artifact_records, revision_record],
        qa_record=qa_record,
        review=review_record,
        asset_updates={
            "title": title,
            "content_status": ContentStatus.APPROVED.value,
            "publication_status": PublicationStatus.READY.value,
            "current_revision_id": promoted_revision.id,
            "latest_candidate_revision_id": next_candidate_id,
            "updated_at": now.isoformat(),
        },
        superseded_revision_ids=[revision.id],
    )
    promoted_revision.file_path = written[promoted_revision.id]
    source_artifact.file_path = written[source_artifact.id]
    runtime_artifact.file_path = written[runtime_artifact.id]

    session.execute(
        ReviewDecision.__table__.update()
        .where(ReviewDecision.asset_id == asset.id, ReviewDecision.is_valid.is_(True))
        .values(is_valid=False)
    )
    revision.review_status = "superseded"
    asset.current_revision_id = promoted_revision.id
    asset.latest_candidate_revision_id = next_candidate_id
    asset.content_status = ContentStatus.APPROVED.value
    asset.publication_status = PublicationStatus.READY.value
    asset.title = title
    asset.updated_at = now
    session.add(promoted_revision)
    session.flush()
    session.add(source_artifact)
    session.flush()
    session.add(runtime_artifact)
    session.add(promoted_rendition)
    session.flush()
    session.add(promoted_qa)
    session.add(decision)
    session.flush()
    for binding in artifact_bindings:
        session.add(
            ReviewArtifact(
                id=stable_id(
                    "review_artifact",
                    decision.id,
                    str(binding["artifact_id"]),
                    str(binding["role"]),
                ),
                review_id=decision.id,
                artifact_id=str(binding["artifact_id"]),
                role=str(binding["role"]),
                sha256=str(binding["sha256"]),
            )
        )
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
    session.commit()
    return decision


def create_review(session: Session, payload: ReviewCreate) -> ReviewDecision:
    revision = require(session, AssetRevision, payload.revision_id, "revision")
    asset = require(session, Asset, revision.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    if revision.review_status != "pending":
        raise ServiceError(409, "only a pending candidate revision can be reviewed")
    if payload.verdict == ReviewVerdict.APPROVE and revision.format == "media":
        return _approve_media_revision(
            session,
            revision=revision,
            asset=asset,
            project=project,
            payload=payload,
        )
    if payload.verdict == ReviewVerdict.APPROVE:
        renditions = session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all()
        for rendition in renditions:
            latest_qa = session.scalar(
                select(QARun)
                .where(QARun.rendition_id == rendition.id)
                .order_by(QARun.created_at.desc())
                .limit(1)
            )
            if latest_qa is None or latest_qa.verdict not in {
                QAVerdict.PASS.value,
                QAVerdict.WARNING.value,
            }:
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
            "asset_id": asset.id,
            "revision_id": decision.revision_id,
            "verdict": decision.verdict,
            "notes": decision.notes,
            "dependency_hash": decision.dependency_hash,
            "artifacts": [],
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


def _release_preflight(
    session: Session, *, project: Project
) -> tuple[list[dict[str, Any]], list[Asset], list[str]]:
    store = ProjectStore(project.root_path)
    assets = list(
        session.scalars(
            select(Asset)
            .where(
                Asset.project_id == project.id,
                Asset.content_status == ContentStatus.APPROVED.value,
            )
            .order_by(Asset.key)
        ).all()
    )
    entries: list[dict[str, Any]] = []
    valid_assets: list[Asset] = []
    issues: list[str] = []
    target_owners: dict[str, str] = {}
    if not assets:
        issues.append("the project has no approved assets")

    for asset in assets:
        if not asset.current_revision_id:
            issues.append(f"{asset.key}: approved asset has no current revision")
            continue
        revision = session.get(AssetRevision, asset.current_revision_id)
        if revision is None or revision.asset_id != asset.id:
            issues.append(f"{asset.key}: current revision is missing or belongs to another asset")
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
        if approval is None:
            issues.append(f"{asset.key}: current revision has no valid approval")
            continue
        if approval.dependency_hash != dependency_hash(session, revision):
            issues.append(f"{asset.key}: approved dependency hash is stale")
            continue

        content_rendition = (
            revision.content_json.get("rendition")
            if isinstance(revision.content_json, dict)
            else None
        )
        renditions = list(
            session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all()
        )
        if revision.format == "media" and len(renditions) != 1:
            issues.append(f"{asset.key}: media revision must have exactly one rendition")
            continue

        rendition_entries: list[dict[str, Any]] = []
        asset_failed = False
        bindings_by_role: dict[str, list[ReviewArtifact]] = {}
        for binding in approval.artifact_bindings:
            bindings_by_role.setdefault(binding.role, []).append(binding)
        for rendition in renditions:
            source_path = rendition.source_path
            runtime_path = rendition.normalized_path or source_path
            if source_path.startswith("workspace/") or runtime_path.startswith("workspace/"):
                issues.append(f"{asset.key}: approved rendition still references workspace")
                asset_failed = True
                continue
            try:
                source_file = safe_join(store.root, source_path)
                runtime_file = safe_join(store.root, runtime_path)
            except StorageError as exc:
                issues.append(f"{asset.key}: {exc}")
                asset_failed = True
                continue

            source_facts: tuple[str, int] | None = None
            if not source_file.is_file():
                issues.append(f"{asset.key}: source blob is missing: {source_path}")
                asset_failed = True
            else:
                try:
                    source_facts = (sha256_file(source_file), source_file.stat().st_size)
                except OSError as exc:
                    issues.append(f"{asset.key}: source blob cannot be verified: {exc}")
                    asset_failed = True

            runtime_facts: tuple[str, int] | None = None
            if not runtime_file.is_file():
                issues.append(f"{asset.key}: runtime blob is missing: {runtime_path}")
                asset_failed = True
            else:
                try:
                    runtime_facts = (sha256_file(runtime_file), runtime_file.stat().st_size)
                except OSError as exc:
                    issues.append(f"{asset.key}: runtime blob cannot be verified: {exc}")
                    asset_failed = True
                else:
                    if runtime_facts[0] != rendition.sha256:
                        issues.append(
                            f"{asset.key}: runtime blob hash does not match its rendition"
                        )
                        asset_failed = True
                    if runtime_facts[1] != rendition.byte_size:
                        issues.append(
                            f"{asset.key}: runtime blob byte size does not match its rendition"
                        )
                        asset_failed = True

            if revision.format == "media":
                expected_rendition = {
                    "media_type": rendition.media_type,
                    "source_path": source_path,
                    "normalized_path": rendition.normalized_path,
                    "target_path": rendition.target_path,
                    "sha256": rendition.sha256,
                    "width": rendition.width,
                    "height": rendition.height,
                    "byte_size": rendition.byte_size,
                }
                if not isinstance(content_rendition, dict) or any(
                    content_rendition.get(field) != value
                    for field, value in expected_rendition.items()
                ):
                    issues.append(f"{asset.key}: revision content does not match its rendition")
                    asset_failed = True

            latest_qa = session.scalar(
                select(QARun)
                .where(QARun.rendition_id == rendition.id)
                .order_by(QARun.created_at.desc())
                .limit(1)
            )
            if latest_qa is None or latest_qa.verdict not in {
                QAVerdict.PASS.value,
                QAVerdict.WARNING.value,
            }:
                issues.append(f"{asset.key}: latest hard QA does not pass")
                asset_failed = True

            artifact_id = (
                content_rendition.get("artifact_id")
                if isinstance(content_rendition, dict)
                else None
            )
            source_artifact_id = (
                content_rendition.get("source_artifact_id")
                if isinstance(content_rendition, dict)
                else None
            )
            artifact_ids = (source_artifact_id, artifact_id)
            if any(artifact_ids) and not all(artifact_ids):
                issues.append(f"{asset.key}: promoted rendition has incomplete artifact metadata")
                asset_failed = True
            elif not any(artifact_ids) and approval.artifact_bindings:
                issues.append(f"{asset.key}: review has artifacts but the revision does not")
                asset_failed = True

            source_sha256 = (
                content_rendition.get("source_sha256")
                if isinstance(content_rendition, dict)
                else None
            )
            for role, expected_id, expected_path, expected_hash, expected_size, facts in (
                ("source", source_artifact_id, source_path, source_sha256, None, source_facts),
                (
                    "runtime",
                    artifact_id,
                    runtime_path,
                    rendition.sha256,
                    rendition.byte_size,
                    runtime_facts,
                ),
            ):
                if not expected_id:
                    continue
                artifact = session.get(Artifact, str(expected_id))
                role_bindings = bindings_by_role.get(role, [])
                expected_parent_id = None if role == "source" else source_artifact_id
                if (
                    artifact is None
                    or artifact.project_id != project.id
                    or artifact.revision_id != revision.id
                    or artifact.role != role
                    or artifact.path != expected_path
                    or artifact.sha256 != expected_hash
                    or artifact.parent_artifact_id != expected_parent_id
                    or (expected_size is not None and artifact.byte_size != expected_size)
                    or len(role_bindings) != 1
                ):
                    issues.append(f"{asset.key}: {role} artifact binding is invalid")
                    asset_failed = True
                    continue
                binding = role_bindings[0]
                if binding.artifact_id != artifact.id or binding.sha256 != artifact.sha256:
                    issues.append(f"{asset.key}: {role} artifact binding is invalid")
                    asset_failed = True
                if facts is not None and facts != (artifact.sha256, artifact.byte_size):
                    issues.append(
                        f"{asset.key}: {role} artifact blob hash or byte size is invalid"
                    )
                    asset_failed = True

            if rendition.target_path:
                try:
                    safe_join(store.root, rendition.target_path)
                except StorageError as exc:
                    issues.append(f"{asset.key}: invalid target path: {exc}")
                    asset_failed = True
                target_key = rendition.target_path.casefold()
                owner = target_owners.get(target_key)
                if owner is not None and owner != asset.key:
                    issues.append(
                        f"{asset.key}: target path collides with {owner}: {rendition.target_path}"
                    )
                    asset_failed = True
                else:
                    target_owners[target_key] = asset.key

            rendition_entries.append(
                {
                    "id": rendition.id,
                    "artifact_id": artifact_id,
                    "source_artifact_id": source_artifact_id,
                    "media_type": rendition.media_type,
                    "path": runtime_path,
                    "target_path": rendition.target_path,
                    "sha256": rendition.sha256,
                    "width": rendition.width,
                    "height": rendition.height,
                    "byte_size": rendition.byte_size,
                }
            )
        if asset_failed:
            continue
        entries.append(
            {
                "key": asset.key,
                "kind": asset.kind,
                "subtype": asset.subtype,
                "revision_id": revision.id,
                "content_hash": revision.content_hash,
                "dependency_hash": approval.dependency_hash,
                "content": revision.content_json,
                "renditions": rendition_entries,
            }
        )
        valid_assets.append(asset)
    return entries, valid_assets, issues


def create_release(session: Session, payload: ReleaseCreate) -> Release:
    project = require(session, Project, payload.project_id, "project")
    entries, released_assets, issues = _release_preflight(session, project=project)
    if issues:
        raise ServiceError(409, "release preflight failed:\n- " + "\n- ".join(issues))

    store = ProjectStore(project.root_path)
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
    session.add(release)
    session.flush()
    release.manifest_path, release.manifest_hash = store.commit_release(
        release.id,
        manifest,
        {
            asset.key: {
                "publication_status": PublicationStatus.PUBLISHED.value,
                "updated_at": asset.updated_at.isoformat(),
            }
            for asset in released_assets
        },
    )
    for asset in released_assets:
        asset.publication_status = PublicationStatus.PUBLISHED.value
    session.commit()
    return release
