from __future__ import annotations

import shutil
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


def register_project(session: Session, payload: ProjectCreate) -> Project:
    root = Path(payload.root_path).expanduser()
    if not root.is_absolute():
        raise ServiceError(422, "project root must be an absolute path")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise ServiceError(422, "project root does not exist") from exc
    if not root.is_dir():
        raise ServiceError(422, "project root must be a directory")
    existing = session.scalar(select(Project).where(Project.root_path == str(root)))
    if existing:
        return existing
    project = Project(id=new_id(), name=payload.name or root.name, root_path=str(root))
    try:
        if payload.initialize:
            ProjectStore(root).initialize(project_id=project.id, name=project.name)
        elif not (root / ".game-assets" / "project.yaml").is_file():
            raise ServiceError(422, "project contract is missing; set initialize=true to create it")
        session.add(project)
        session.commit()
    except StorageError as exc:
        session.rollback()
        raise ServiceError(409, str(exc)) from exc
    return project


def asset_descriptor(asset: Asset) -> dict[str, Any]:
    return {
        "version": 1,
        "id": asset.id,
        "projectId": asset.project_id,
        "key": asset.key,
        "kind": asset.kind,
        "subtype": asset.subtype,
        "title": asset.title,
        "schemaRef": asset.schema_ref,
        "tags": asset.tags,
        "metadata": asset.metadata_json,
        "contentStatus": asset.content_status,
        "generationStatus": asset.generation_status,
        "publicationStatus": asset.publication_status,
        "currentRevisionId": asset.current_revision_id,
        "latestCandidateRevisionId": asset.latest_candidate_revision_id,
        "createdAt": asset.created_at.isoformat(),
        "updatedAt": asset.updated_at.isoformat(),
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
        "version": 1,
        "id": revision.id,
        "assetId": revision.asset_id,
        "sequence": revision.sequence,
        "format": revision.format,
        "content": revision.content_json,
        "contentHash": revision.content_hash,
        "parentRevisionId": revision.parent_revision_id,
        "inputHash": revision.input_hash,
        "styleRevision": revision.style_revision,
        "promptRecipe": revision.prompt_recipe,
        "providerSnapshot": revision.provider_snapshot,
        "candidatePath": revision.candidate_path,
        "reviewStatus": revision.review_status,
        "createdAt": revision.created_at.isoformat(),
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
        candidate_path=payload.candidate_path,
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
        store.write_asset(asset_descriptor(asset))
        session.commit()
    except (StorageError, IntegrityError) as exc:
        session.rollback()
        raise ServiceError(409, str(exc)) from exc
    return revision


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
                    "sourceAssetId": row.source_asset_id,
                    "targetAssetId": row.target_asset_id,
                    "type": row.relation_type,
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


def scan_project(session: Session, project: Project) -> ScanReport:
    store = ProjectStore(project.root_path)
    assets, revisions, errors = store.scan()
    indexed_assets = 0
    indexed_revisions = 0
    known_asset_ids: set[str] = set()
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
            asset.schema_ref = data.get("schemaRef")
            asset.tags = list(data.get("tags", []))
            asset.metadata_json = dict(data.get("metadata", {}))
            asset.content_status = str(data.get("contentStatus", ContentStatus.DRAFT.value))
            asset.generation_status = str(data.get("generationStatus", GenerationStatus.IDLE.value))
            asset.publication_status = str(data.get("publicationStatus", PublicationStatus.UNPUBLISHED.value))
            asset.current_revision_id = data.get("currentRevisionId")
            asset.latest_candidate_revision_id = data.get("latestCandidateRevisionId")
            asset.created_at = _parse_datetime(data.get("createdAt"))
            asset.updated_at = _parse_datetime(data.get("updatedAt"))
            indexed_assets += 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"asset {data.get('id', '<unknown>')}: {exc}")
    session.flush()
    for data in revisions:
        try:
            asset_id = str(data["assetId"])
            if session.get(Asset, asset_id) is None:
                raise ValueError("revision references an unknown asset")
            identifier = str(data["id"])
            revision = session.get(AssetRevision, identifier)
            if revision is None:
                revision = AssetRevision(id=identifier, asset_id=asset_id)
                session.add(revision)
            revision.asset_id = asset_id
            revision.sequence = int(data["sequence"])
            revision.format = str(data["format"])
            revision.content_json = data.get("content")
            revision.content_hash = str(data["contentHash"])
            revision.parent_revision_id = data.get("parentRevisionId")
            revision.input_hash = str(data["inputHash"])
            revision.style_revision = data.get("styleRevision")
            revision.prompt_recipe = data.get("promptRecipe")
            revision.provider_snapshot = dict(data.get("providerSnapshot", {}))
            revision.file_path = str(data["filePath"])
            revision.candidate_path = data.get("candidatePath")
            revision.review_status = str(data.get("reviewStatus", "pending"))
            revision.reviewed_at = _parse_datetime(data["reviewedAt"]) if data.get("reviewedAt") else None
            revision.created_at = _parse_datetime(data.get("createdAt"))
            indexed_revisions += 1
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"revision {data.get('id', '<unknown>')}: {exc}")
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
                "inputHash": revision.input_hash,
                "styleRevision": revision.style_revision,
                "promptRecipe": revision.prompt_recipe,
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
        if revision.candidate_path:
            store = ProjectStore(project.root_path)
            candidate = safe_join(store.root, revision.candidate_path)
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
            "version": 1,
            "id": decision.id,
            "revisionId": decision.revision_id,
            "verdict": decision.verdict,
            "notes": decision.notes,
            "dependencyHash": decision.dependency_hash,
            "createdAt": decision.created_at.isoformat(),
        },
    )
    store.update_asset_state(
        kind=asset.kind,
        key=asset.key,
        content_status=asset.content_status,
        publication_status=asset.publication_status,
        current_revision_id=asset.current_revision_id,
        latest_candidate_revision_id=asset.latest_candidate_revision_id,
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
            "version": 1,
            "id": qa.id,
            "renditionId": rendition.id,
            "verdict": verdict,
            "checks": checks,
            "createdAt": qa.created_at.isoformat(),
        },
        immutable=True,
    )
    qa.report_path = relative_to_root(store.root, report_path)
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
                    "mediaType": rendition.media_type,
                    "path": published_path,
                    "sha256": rendition.sha256,
                    "width": rendition.width,
                    "height": rendition.height,
                    "backupPath": backup_path,
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
                "revisionId": revision.id,
                "contentHash": revision.content_hash,
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
        "version": 1,
        "releaseId": release.id,
        "name": release.name,
        "projectId": project.id,
        "createdAt": release.created_at.isoformat(),
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
