from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Database
from .domain import (
    AssetCreate,
    AssetRead,
    EmperorImportRequest,
    EmperorPreviewRequest,
    GenerationAttemptRead,
    GenerationJobRead,
    GenerationPlanCreate,
    GenerationPlanRead,
    GenerationStatus,
    Message,
    ProjectCreate,
    ProjectRead,
    ProviderCapabilities,
    ProviderCreate,
    ProviderKind,
    ProviderRead,
    ProviderUnlock,
    QARunCreate,
    QARunRead,
    RelationCreate,
    RelationRead,
    ReleaseCreate,
    ReleaseRead,
    RenditionRead,
    ReviewCreate,
    ReviewRead,
    RevisionCreate,
    RevisionRead,
    ScanReport,
)
from .models import (
    Asset,
    AssetRelation,
    AssetRevision,
    GenerationAttempt,
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
from .providers import (
    CredentialVault,
    ProviderError,
    build_provider,
    validate_base_url,
)
from .services import (
    ServiceError,
    confirm_plan,
    create_asset,
    create_plan,
    create_relation,
    create_release,
    create_review,
    create_revision,
    register_project,
    require,
    run_qa,
    scan_project,
)
from .storage import ProjectStore, StorageError, safe_join


router = APIRouter(prefix="/api")


def db(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    yield from database.session()


def vault(request: Request) -> CredentialVault:
    return request.app.state.vault


def provider_view(profile: ProviderProfile, credentials: CredentialVault) -> ProviderRead:
    view = ProviderRead.model_validate(profile)
    return view.model_copy(
        update={"is_unlocked": profile.kind == ProviderKind.FAKE.value or credentials.is_unlocked(profile.id)}
    )


def provider_http_error(exc: ProviderError) -> HTTPException:
    mapping = {
        "auth": 401,
        "billing": 402,
        "quota": 429,
        "rate_limit": 429,
        "content_policy": 422,
        "validation": 422,
        "network": 502,
        "server": 502,
    }
    return HTTPException(mapping.get(exc.category.value, 502), detail={"category": exc.category.value, "message": str(exc)})


def emperor_adapter():
    try:
        from emperor_adapter import AdapterError, dry_run, import_to
    except ImportError as exc:
        raise HTTPException(503, "Emperor adapter is not installed") from exc
    return AdapterError, dry_run, import_to


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/importers/emperor/preview")
def emperor_preview(payload: EmperorPreviewRequest) -> dict[str, Any]:
    AdapterError, dry_run, _import_to = emperor_adapter()
    try:
        return {"executed": False, "preview": dry_run(payload.source_root)}
    except (AdapterError, OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/importers/emperor/import")
def emperor_import(payload: EmperorImportRequest) -> dict[str, Any]:
    AdapterError, dry_run, import_to = emperor_adapter()
    try:
        if not payload.apply:
            return {"executed": False, "preview": dry_run(payload.source_root)}
        if not payload.destination_path:
            raise HTTPException(422, "destination_path is required when apply=true")
        return {
            "executed": True,
            "report": import_to(payload.source_root, payload.destination_path),
        }
    except HTTPException:
        raise
    except (AdapterError, OSError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(session: Session = Depends(db)) -> list[Project]:
    return list(session.scalars(select(Project).order_by(Project.created_at.desc())).all())


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def add_project(payload: ProjectCreate, session: Session = Depends(db)) -> Project:
    return register_project(session, payload)


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, session: Session = Depends(db)) -> Project:
    return require(session, Project, project_id, "project")


@router.post("/projects/{project_id}/scan", response_model=ScanReport)
def scan(project_id: str, session: Session = Depends(db)) -> ScanReport:
    return scan_project(session, require(session, Project, project_id, "project"))


@router.post("/projects/{project_id}/rebuild", response_model=ScanReport)
def rebuild(project_id: str, session: Session = Depends(db)) -> ScanReport:
    return scan_project(session, require(session, Project, project_id, "project"))


@router.get("/assets", response_model=list[AssetRead])
def list_assets(
    project_id: str,
    kind: str | None = None,
    subtype: str | None = None,
    search: str | None = None,
    session: Session = Depends(db),
) -> list[Asset]:
    statement = select(Asset).where(Asset.project_id == project_id)
    if kind:
        statement = statement.where(Asset.kind == kind)
    if subtype:
        statement = statement.where(Asset.subtype == subtype)
    if search:
        statement = statement.where(Asset.title.contains(search) | Asset.key.contains(search))
    return list(session.scalars(statement.order_by(Asset.kind, Asset.key)).all())


@router.post("/assets", response_model=AssetRead, status_code=status.HTTP_201_CREATED)
def add_asset(payload: AssetCreate, session: Session = Depends(db)) -> Asset:
    return create_asset(session, payload)


@router.get("/assets/{asset_id}", response_model=AssetRead)
def get_asset(asset_id: str, session: Session = Depends(db)) -> Asset:
    return require(session, Asset, asset_id, "asset")


@router.get("/assets/{asset_id}/candidates", response_model=list[RevisionRead])
def asset_candidates(asset_id: str, session: Session = Depends(db)) -> list[AssetRevision]:
    require(session, Asset, asset_id, "asset")
    return list(
        session.scalars(
            select(AssetRevision)
            .where(
                AssetRevision.asset_id == asset_id,
                AssetRevision.review_status == "pending",
            )
            .order_by(AssetRevision.sequence.desc())
        ).all()
    )


@router.get("/revisions", response_model=list[RevisionRead])
def list_revisions(asset_id: str, session: Session = Depends(db)) -> list[AssetRevision]:
    return list(
        session.scalars(
            select(AssetRevision)
            .where(AssetRevision.asset_id == asset_id)
            .order_by(AssetRevision.sequence.desc())
        ).all()
    )


@router.post("/revisions", response_model=RevisionRead, status_code=status.HTTP_201_CREATED)
def add_revision(payload: RevisionCreate, session: Session = Depends(db)) -> AssetRevision:
    return create_revision(session, payload)


@router.get("/revisions/{revision_id}", response_model=RevisionRead)
def get_revision(revision_id: str, session: Session = Depends(db)) -> AssetRevision:
    return require(session, AssetRevision, revision_id, "revision")


@router.get("/revisions/{revision_id}/renditions", response_model=list[RenditionRead])
def revision_renditions(revision_id: str, session: Session = Depends(db)) -> list[Rendition]:
    return list(session.scalars(select(Rendition).where(Rendition.revision_id == revision_id)).all())


@router.get("/renditions/{rendition_id}/content", response_class=FileResponse)
def rendition_content(rendition_id: str, session: Session = Depends(db)) -> FileResponse:
    rendition = require(session, Rendition, rendition_id, "rendition")
    revision = require(session, AssetRevision, rendition.revision_id, "revision")
    asset = require(session, Asset, revision.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    store = ProjectStore(project.root_path)
    try:
        content_path = safe_join(store.root, rendition.normalized_path or rendition.source_path)
    except StorageError as exc:
        raise HTTPException(409, "rendition path is outside the registered project") from exc
    if not content_path.is_file():
        raise HTTPException(404, "rendition content is missing")
    return FileResponse(
        content_path,
        media_type=rendition.media_type,
        filename=None,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/relations", response_model=list[RelationRead])
def list_relations(project_id: str, session: Session = Depends(db)) -> list[AssetRelation]:
    return list(
        session.scalars(
            select(AssetRelation)
            .where(AssetRelation.project_id == project_id)
            .order_by(AssetRelation.created_at)
        ).all()
    )


@router.post("/relations", response_model=RelationRead, status_code=status.HTTP_201_CREATED)
def add_relation(payload: RelationCreate, session: Session = Depends(db)) -> AssetRelation:
    return create_relation(session, payload)


@router.get("/providers", response_model=list[ProviderRead])
def list_providers(
    session: Session = Depends(db), credentials: CredentialVault = Depends(vault)
) -> list[ProviderRead]:
    profiles = session.scalars(select(ProviderProfile).order_by(ProviderProfile.created_at)).all()
    return [provider_view(profile, credentials) for profile in profiles]


@router.post("/providers", response_model=ProviderRead, status_code=status.HTTP_201_CREATED)
def add_provider(
    payload: ProviderCreate,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    base_url = payload.base_url
    if payload.kind == ProviderKind.OPENAI_COMPATIBLE:
        try:
            base_url = validate_base_url(
                payload.base_url, allow_private_network=payload.allow_private_network
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    profile = ProviderProfile(
        id=new_id(),
        name=payload.name,
        kind=payload.kind.value,
        base_url=base_url,
        text_model=payload.text_model,
        image_model=payload.image_model,
        quality=payload.quality,
        concurrency=payload.concurrency,
        max_retries=payload.max_retries,
        allow_private_network=payload.allow_private_network,
        pricing=payload.pricing,
    )
    session.add(profile)
    session.commit()
    return provider_view(profile, credentials)


@router.get("/providers/{provider_id}", response_model=ProviderRead)
def get_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    return provider_view(require(session, ProviderProfile, provider_id, "provider profile"), credentials)


@router.post("/providers/{provider_id}/unlock", response_model=Message)
def unlock_provider(
    provider_id: str,
    payload: ProviderUnlock,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> Message:
    require(session, ProviderProfile, provider_id, "provider profile")
    credentials.unlock(provider_id, payload.api_key)
    jobs = session.scalars(
        select(GenerationJob).where(
            GenerationJob.provider_profile_id == provider_id,
            GenerationJob.status == GenerationStatus.CREDENTIALS_LOCKED.value,
        )
    ).all()
    for job in jobs:
        job.status = GenerationStatus.QUEUED.value
        job.error_category = None
        job.error_message = None
        job.updated_at = utcnow()
    session.commit()
    return Message(detail="provider unlocked for this process")


@router.post("/providers/{provider_id}/lock", response_model=Message)
def lock_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> Message:
    require(session, ProviderProfile, provider_id, "provider profile")
    credentials.lock(provider_id)
    return Message(detail="provider credentials removed from memory")


@router.get("/providers/{provider_id}/capabilities", response_model=ProviderCapabilities)
def provider_capabilities(
    provider_id: str, session: Session = Depends(db)
) -> ProviderCapabilities:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    return ProviderCapabilities(
        structured_text=True,
        image_generation=True,
        image_edit=True,
        models=[profile.text_model, profile.image_model],
    )


@router.post("/providers/{provider_id}/test", response_model=ProviderCapabilities)
async def test_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderCapabilities:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    try:
        models = await build_provider(profile, credentials).test_connection()
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    return ProviderCapabilities(
        structured_text=True, image_generation=True, image_edit=True, models=models
    )


@router.get("/generation-plans", response_model=list[GenerationPlanRead])
def list_plans(project_id: str, session: Session = Depends(db)) -> list[GenerationPlan]:
    return list(
        session.scalars(
            select(GenerationPlan)
            .where(GenerationPlan.project_id == project_id)
            .order_by(GenerationPlan.created_at.desc())
        ).all()
    )


@router.post("/generation-plans", response_model=GenerationPlanRead, status_code=status.HTTP_201_CREATED)
def add_plan(payload: GenerationPlanCreate, session: Session = Depends(db)) -> GenerationPlan:
    return create_plan(session, payload)


@router.get("/generation-plans/{plan_id}", response_model=GenerationPlanRead)
def get_plan(plan_id: str, session: Session = Depends(db)) -> GenerationPlan:
    return require(session, GenerationPlan, plan_id, "generation plan")


@router.post("/generation-plans/{plan_id}/confirm", response_model=list[GenerationJobRead])
def approve_plan(
    plan_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> list[GenerationJob]:
    plan = require(session, GenerationPlan, plan_id, "generation plan")
    profile = require(session, ProviderProfile, plan.provider_profile_id, "provider profile")
    return confirm_plan(
        session,
        plan,
        credentials_available=profile.kind == ProviderKind.FAKE.value
        or credentials.is_unlocked(profile.id),
    )


@router.get("/jobs/events")
async def job_events(
    request: Request,
    project_id: str | None = None,
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        seen: dict[str, str] = {}
        last_keepalive = time.monotonic()
        while not await request.is_disconnected():
            database: Database = request.app.state.database
            with database.sessions() as session:
                statement = select(GenerationJob).order_by(GenerationJob.updated_at, GenerationJob.id)
                if project_id:
                    statement = statement.where(GenerationJob.project_id == project_id)
                jobs = session.scalars(statement).all()
                for job in jobs:
                    token = f"{job.status}:{job.progress}:{job.attempt_count}:{job.updated_at.isoformat()}"
                    if seen.get(job.id) == token:
                        continue
                    seen[job.id] = token
                    data = GenerationJobRead.model_validate(job).model_dump_json()
                    yield f"id: {job.id}:{job.attempt_count}\nevent: job\ndata: {data}\n\n"
            if time.monotonic() - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
            await asyncio.sleep(0.25)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs", response_model=list[GenerationJobRead])
def list_jobs(
    project_id: str | None = None,
    plan_id: str | None = None,
    job_status: str | None = Query(default=None, alias="status"),
    session: Session = Depends(db),
) -> list[GenerationJob]:
    statement = select(GenerationJob)
    if project_id:
        statement = statement.where(GenerationJob.project_id == project_id)
    if plan_id:
        statement = statement.where(GenerationJob.plan_id == plan_id)
    if job_status:
        statement = statement.where(GenerationJob.status == job_status)
    return list(session.scalars(statement.order_by(GenerationJob.created_at.desc())).all())


@router.get("/jobs/{job_id}", response_model=GenerationJobRead)
def get_job(job_id: str, session: Session = Depends(db)) -> GenerationJob:
    return require(session, GenerationJob, job_id, "generation job")


@router.get("/jobs/{job_id}/attempts", response_model=list[GenerationAttemptRead])
def job_attempts(job_id: str, session: Session = Depends(db)) -> list[GenerationAttempt]:
    require(session, GenerationJob, job_id, "generation job")
    return list(
        session.scalars(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == job_id)
            .order_by(GenerationAttempt.number)
        ).all()
    )


@router.post("/jobs/{job_id}/cancel", response_model=GenerationJobRead)
def cancel_job(job_id: str, session: Session = Depends(db)) -> GenerationJob:
    job = require(session, GenerationJob, job_id, "generation job")
    job.cancel_requested = True
    if job.status in {
        GenerationStatus.QUEUED.value,
        GenerationStatus.CREDENTIALS_LOCKED.value,
    }:
        job.status = GenerationStatus.CANCELLED.value
        job.error_category = "cancelled"
        job.error_message = "cancelled before provider invocation"
    job.updated_at = utcnow()
    session.commit()
    return job


@router.post("/jobs/{job_id}/resume", response_model=GenerationJobRead)
def resume_job(
    job_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> GenerationJob:
    job = require(session, GenerationJob, job_id, "generation job")
    if job.status not in {
        GenerationStatus.FAILED.value,
        GenerationStatus.CANCELLED.value,
        GenerationStatus.CREDENTIALS_LOCKED.value,
    }:
        raise ServiceError(409, "only failed, cancelled, or credentials-locked jobs can resume")
    profile = require(session, ProviderProfile, job.provider_profile_id, "provider profile")
    job.status = (
        GenerationStatus.QUEUED.value
        if profile.kind == ProviderKind.FAKE.value or credentials.is_unlocked(profile.id)
        else GenerationStatus.CREDENTIALS_LOCKED.value
    )
    job.cancel_requested = False
    job.progress = 0.0
    job.error_category = None
    job.error_message = None
    job.updated_at = utcnow()
    session.commit()
    return job


@router.get("/qa-runs", response_model=list[QARunRead])
def list_qa_runs(rendition_id: str | None = None, session: Session = Depends(db)) -> list[QARun]:
    statement = select(QARun)
    if rendition_id:
        statement = statement.where(QARun.rendition_id == rendition_id)
    return list(session.scalars(statement.order_by(QARun.created_at.desc())).all())


@router.post("/qa-runs", response_model=QARunRead, status_code=status.HTTP_201_CREATED)
def add_qa_run(payload: QARunCreate, session: Session = Depends(db)) -> QARun:
    return run_qa(session, payload)


@router.get("/reviews", response_model=list[ReviewRead])
def list_reviews(asset_id: str | None = None, session: Session = Depends(db)) -> list[ReviewDecision]:
    statement = select(ReviewDecision)
    if asset_id:
        statement = statement.where(ReviewDecision.asset_id == asset_id)
    return list(session.scalars(statement.order_by(ReviewDecision.created_at.desc())).all())


@router.post("/reviews", response_model=ReviewRead, status_code=status.HTTP_201_CREATED)
def add_review(payload: ReviewCreate, session: Session = Depends(db)) -> ReviewDecision:
    return create_review(session, payload)


@router.get("/releases", response_model=list[ReleaseRead])
def list_releases(project_id: str, session: Session = Depends(db)) -> list[Release]:
    return list(
        session.scalars(
            select(Release)
            .where(Release.project_id == project_id)
            .order_by(Release.created_at.desc())
        ).all()
    )


@router.post("/releases", response_model=ReleaseRead, status_code=status.HTTP_201_CREATED)
def add_release(payload: ReleaseCreate, session: Session = Depends(db)) -> Release:
    return create_release(session, payload)


@router.get("/releases/{release_id}", response_model=ReleaseRead)
def get_release(release_id: str, session: Session = Depends(db)) -> Release:
    return require(session, Release, release_id, "release")
