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
    AgentDiagnoseRequest,
    AgentEventRead,
    AgentSessionCreate,
    AgentSessionRead,
    AssetCreate,
    AssetRead,
    ArtifactRead,
    ChangeSetRead,
    DeliveryRead,
    ExportConfigRead,
    ExportConfigUpdate,
    ExportRequest,
    ExportRollbackRequest,
    ExportVerifyRequest,
    GenerationAttemptRead,
    GenerationJobRead,
    GenerationPlanCreate,
    GenerationPlanRead,
    GenerationStatus,
    FindingRead,
    Message,
    PlanBudgetUpdate,
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    ProviderCapabilities,
    ProviderCreate,
    ProviderDefaultRoute,
    ProviderDefaultsRead,
    ProviderDefaultsUpdate,
    ProviderKind,
    ProviderModelsRead,
    ProviderModelsUpdate,
    ProviderRead,
    ProviderUnlock,
    ProviderUpdate,
    QARunCreate,
    QARunRead,
    RemediationCreate,
    RemediationKind,
    RemediationRead,
    RemediationStatus,
    RelationCreate,
    RelationRead,
    ReleaseCreate,
    ReleasePreflightRead,
    ReleaseRead,
    RenditionRead,
    ReviewCreate,
    ReviewRead,
    RevisionCreate,
    RevisionRead,
    RunEventRead,
    RunEvidenceRead,
    RunInspectRead,
    ScanReport,
    SystemInfo,
)
from .models import (
    AgentEvent,
    AgentSession,
    Asset,
    AssetRelation,
    AssetRevision,
    Artifact,
    ChangeSet,
    Delivery,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    ProductionFinding,
    Project,
    ProviderProfile,
    ProviderRoutingDefaults,
    QARun,
    RemediationAction,
    Release,
    Rendition,
    ReviewDecision,
    RunEvent,
    RunEvidence,
    new_id,
    utcnow,
)
from .providers import (
    CredentialVault,
    ProviderError,
    build_provider,
    validate_base_url,
)
from .provider_catalog import (
    apply_model_overrides,
    ensure_profile_default_models,
    ensure_routing_defaults,
    merge_discovered_models,
    model_is_compatible,
)
from .production import (
    attempt_output_is_valid,
    create_remediation,
    inspect_run,
    record_run_event,
    refresh_plan_status,
    resume_recoverable_jobs,
    update_extra_call_budget,
)
from .agent import (
    _persist_agent_audit,
    build_context_package,
    diagnose_job,
    list_agent_events,
    record_agent_event,
)
from .delivery import (
    DeliveryError,
    apply_export,
    export_preview,
    list_deliveries,
    release_preflight,
    verify_export,
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
    discover_projects,
    register_project,
    require,
    run_qa,
    scan_project,
    update_project,
)
from .storage import ProjectStore, StorageError, sha256_file


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


def available_provider_ids(
    session: Session, credentials: CredentialVault
) -> set[str]:
    return {
        profile.id
        for profile in session.scalars(select(ProviderProfile)).all()
        if profile.kind == ProviderKind.FAKE.value or credentials.is_unlocked(profile.id)
    }


def provider_models_view(profile: ProviderProfile) -> ProviderModelsRead:
    return ProviderModelsRead(
        provider_profile_id=profile.id,
        models=profile.models_json or [],
        refreshed_at=profile.models_refreshed_at,
    )


def provider_defaults_view(defaults: ProviderRoutingDefaults) -> ProviderDefaultsRead:
    return ProviderDefaultsRead(
        text=(
            ProviderDefaultRoute(
                provider_profile_id=defaults.text_provider_profile_id,
                model=defaults.text_model,
            )
            if defaults.text_provider_profile_id and defaults.text_model
            else None
        ),
        image=(
            ProviderDefaultRoute(
                provider_profile_id=defaults.image_provider_profile_id,
                model=defaults.image_model,
            )
            if defaults.image_provider_profile_id and defaults.image_model
            else None
        ),
        updated_at=defaults.updated_at,
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


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(request: Request, session: Session = Depends(db)) -> list[Project]:
    projects, _errors = discover_projects(session, request.app.state.settings.projects_root)
    return projects


@router.get("/projects/discovery")
def project_discovery(request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    projects, errors = discover_projects(
        session, request.app.state.settings.projects_root, scan=False
    )
    return {
        "projects_root": str(request.app.state.settings.projects_root.resolve()),
        "projects": [ProjectRead.model_validate(project).model_dump(mode="json") for project in projects],
        "errors": errors,
    }


@router.post("/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def add_project(payload: ProjectCreate, request: Request, session: Session = Depends(db)) -> Project:
    return register_project(session, payload, request.app.state.settings.projects_root)


@router.get("/system", response_model=SystemInfo)
def system_info(request: Request) -> SystemInfo:
    settings = request.app.state.settings
    return SystemInfo(
        projects_root=str(settings.projects_root.resolve()),
        state_dir=str(settings.state_dir.resolve()),
    )


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: str, session: Session = Depends(db)) -> Project:
    return require(session, Project, project_id, "project")


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def edit_project(
    project_id: str, payload: ProjectUpdate, session: Session = Depends(db)
) -> Project:
    return update_project(session, require(session, Project, project_id, "project"), payload)


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


@router.get("/artifacts", response_model=list[ArtifactRead])
def list_artifacts(
    project_id: str | None = None,
    revision_id: str | None = None,
    session: Session = Depends(db),
) -> list[Artifact]:
    statement = select(Artifact)
    if project_id:
        statement = statement.where(Artifact.project_id == project_id)
    if revision_id:
        statement = statement.where(Artifact.revision_id == revision_id)
    return list(session.scalars(statement.order_by(Artifact.created_at, Artifact.id)).all())


@router.get("/artifacts/{artifact_id}", response_model=ArtifactRead)
def get_artifact(artifact_id: str, session: Session = Depends(db)) -> Artifact:
    return require(session, Artifact, artifact_id, "artifact")


@router.get("/renditions/{rendition_id}/content", response_class=FileResponse)
def rendition_content(rendition_id: str, session: Session = Depends(db)) -> FileResponse:
    rendition = require(session, Rendition, rendition_id, "rendition")
    revision = require(session, AssetRevision, rendition.revision_id, "revision")
    asset = require(session, Asset, revision.asset_id, "asset")
    project = require(session, Project, asset.project_id, "project")
    store = ProjectStore(project.root_path)
    try:
        content_path = store.resolve_rendition_path(
            rendition.normalized_path or rendition.source_path
        )
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
    include_archived: bool = True,
    session: Session = Depends(db), credentials: CredentialVault = Depends(vault)
) -> list[ProviderRead]:
    statement = select(ProviderProfile).order_by(ProviderProfile.created_at, ProviderProfile.id)
    if not include_archived:
        statement = statement.where(ProviderProfile.is_active.is_(True))
    profiles = session.scalars(statement).all()
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
        is_active=True,
        models_json=[],
    )
    ensure_profile_default_models(profile)
    session.add(profile)
    session.flush()
    ensure_routing_defaults(session)
    session.commit()
    return provider_view(profile, credentials)


@router.patch("/providers/{provider_id}", response_model=ProviderRead)
def edit_provider(
    provider_id: str,
    payload: ProviderUpdate,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    changes = payload.model_dump(exclude_unset=True)
    if "base_url" in changes or "allow_private_network" in changes:
        base_url = str(changes.get("base_url", profile.base_url))
        allow_private_network = bool(
            changes.get("allow_private_network", profile.allow_private_network)
        )
        if profile.kind == ProviderKind.OPENAI_COMPATIBLE.value:
            try:
                changes["base_url"] = validate_base_url(
                    base_url, allow_private_network=allow_private_network
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
    for field, value in changes.items():
        setattr(profile, field, value)
    ensure_profile_default_models(profile)
    profile.updated_at = utcnow()
    session.commit()
    return provider_view(profile, credentials)


@router.post("/providers/{provider_id}/archive", response_model=ProviderRead)
def archive_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    profile.is_active = False
    profile.updated_at = utcnow()
    defaults = ensure_routing_defaults(session)
    if defaults.text_provider_profile_id == profile.id:
        defaults.text_provider_profile_id = None
        defaults.text_model = None
    if defaults.image_provider_profile_id == profile.id:
        defaults.image_provider_profile_id = None
        defaults.image_model = None
    defaults.updated_at = utcnow()
    session.commit()
    return provider_view(profile, credentials)


@router.post("/providers/{provider_id}/restore", response_model=ProviderRead)
def restore_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    profile.is_active = True
    profile.updated_at = utcnow()
    session.commit()
    return provider_view(profile, credentials)


@router.get("/provider-defaults", response_model=ProviderDefaultsRead)
def get_provider_defaults(session: Session = Depends(db)) -> ProviderDefaultsRead:
    defaults = ensure_routing_defaults(session)
    session.commit()
    return provider_defaults_view(defaults)


@router.put("/provider-defaults", response_model=ProviderDefaultsRead)
def set_provider_defaults(
    payload: ProviderDefaultsUpdate,
    session: Session = Depends(db),
) -> ProviderDefaultsRead:
    defaults = ensure_routing_defaults(session)
    for modality in ("text", "image"):
        route = getattr(payload, modality)
        if route is None:
            setattr(defaults, f"{modality}_provider_profile_id", None)
            setattr(defaults, f"{modality}_model", None)
            continue
        profile = require(
            session,
            ProviderProfile,
            route.provider_profile_id,
            f"default {modality} provider",
        )
        if not profile.is_active:
            raise HTTPException(422, f"default {modality} provider is archived")
        model = route.model.strip()
        if not model_is_compatible(profile, model, modality):
            raise HTTPException(
                422,
                f"default {modality} model {model} is not classified for {modality}",
            )
        setattr(defaults, f"{modality}_provider_profile_id", profile.id)
        setattr(defaults, f"{modality}_model", model)
    defaults.updated_at = utcnow()
    session.commit()
    return provider_defaults_view(defaults)


@router.get("/providers/{provider_id}", response_model=ProviderRead)
def get_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderRead:
    return provider_view(require(session, ProviderProfile, provider_id, "provider profile"), credentials)


@router.get("/providers/{provider_id}/models", response_model=ProviderModelsRead)
def get_provider_models(
    provider_id: str, session: Session = Depends(db)
) -> ProviderModelsRead:
    return provider_models_view(require(session, ProviderProfile, provider_id, "provider profile"))


@router.patch("/providers/{provider_id}/models", response_model=ProviderModelsRead)
def update_provider_models(
    provider_id: str,
    payload: ProviderModelsUpdate,
    session: Session = Depends(db),
) -> ProviderModelsRead:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    profile.models_json = apply_model_overrides(
        profile.models_json or [],
        [item.model_dump(mode="json") for item in payload.models],
    )
    ensure_profile_default_models(profile)
    profile.updated_at = utcnow()
    session.commit()
    return provider_models_view(profile)


async def refresh_provider_model_catalog(
    profile: ProviderProfile,
    session: Session,
    credentials: CredentialVault,
) -> ProviderModelsRead:
    try:
        discovered = await build_provider(profile, credentials).discover_models()
    except ProviderError as exc:
        raise provider_http_error(exc) from exc
    profile.models_json = merge_discovered_models(profile.models_json or [], discovered)
    ensure_profile_default_models(profile)
    profile.models_refreshed_at = utcnow()
    profile.updated_at = utcnow()
    session.commit()
    return provider_models_view(profile)


@router.post("/providers/{provider_id}/models/refresh", response_model=ProviderModelsRead)
async def refresh_provider_models(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderModelsRead:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    return await refresh_provider_model_catalog(profile, session, credentials)


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
    plan_ids = {job.plan_id for job in jobs}
    for plan_id in plan_ids:
        plan = session.get(GenerationPlan, plan_id)
        if plan:
            refresh_plan_status(session, plan)
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
        models=[
            str(model.get("id"))
            for model in profile.models_json or []
            if model.get("id") and model.get("available", True)
        ],
    )


@router.post("/providers/{provider_id}/test", response_model=ProviderCapabilities)
async def test_provider(
    provider_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> ProviderCapabilities:
    profile = require(session, ProviderProfile, provider_id, "provider profile")
    catalog = await refresh_provider_model_catalog(profile, session, credentials)
    return ProviderCapabilities(
        structured_text=True,
        image_generation=True,
        image_edit=True,
        models=[model.id for model in catalog.models if model.available],
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


@router.get("/generation-plans/{plan_id}/inspect", response_model=RunInspectRead)
def inspect_plan(plan_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    plan = require(session, GenerationPlan, plan_id, "generation plan")
    return inspect_run(session, plan)


@router.post("/jobs/{job_id}/agent/diagnose", response_model=AgentSessionRead)
async def diagnose_job_with_codex(
    job_id: str,
    request: Request,
    payload: AgentDiagnoseRequest = AgentDiagnoseRequest(),
    session: Session = Depends(db),
) -> AgentSession:
    """Ask the isolated Codex adapter for one structured diagnosis.

    Adapter failures are represented as an ``awaiting_user`` session rather than
    an HTTP 5xx, so the deterministic manual remediation UI remains usable.
    """

    job = require(session, GenerationJob, job_id, "generation job")
    adapter = getattr(request.app.state, "codex_adapter", None)
    if adapter is None:
        from .codex_adapter import UnavailableCodexAdapter

        adapter = UnavailableCodexAdapter()
    return await diagnose_job(
        session,
        job,
        adapter=adapter,
        finding_ids=payload.finding_ids,
        budget=payload.budget,
        requested_reason=payload.reason,
    )


@router.get("/jobs/{job_id}/agent/context")
def get_agent_context(job_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    job = require(session, GenerationJob, job_id, "generation job")
    context, context_hash = build_context_package(session, job)
    return {"context_hash": context_hash, "context": context}


@router.get("/agent/sessions", response_model=list[AgentSessionRead])
def list_agent_sessions(
    project_id: str | None = None,
    plan_id: str | None = None,
    job_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(db),
) -> list[AgentSession]:
    statement = select(AgentSession)
    if project_id:
        statement = statement.where(AgentSession.project_id == project_id)
    if plan_id:
        statement = statement.where(AgentSession.plan_id == plan_id)
    if job_id:
        statement = statement.where(AgentSession.job_id == job_id)
    if status:
        statement = statement.where(AgentSession.status == status)
    return list(session.scalars(statement.order_by(AgentSession.created_at.desc())).all())


@router.post("/agent/sessions", response_model=AgentSessionRead, status_code=status.HTTP_201_CREATED)
async def create_agent_session(
    payload: AgentSessionCreate,
    request: Request,
    session: Session = Depends(db),
) -> AgentSession:
    job = require(session, GenerationJob, payload.job_id, "generation job")
    adapter = getattr(request.app.state, "codex_adapter", None)
    if adapter is None:
        from .codex_adapter import UnavailableCodexAdapter

        adapter = UnavailableCodexAdapter()
    return await diagnose_job(
        session,
        job,
        adapter=adapter,
        finding_ids=payload.finding_ids,
        budget=payload.budget,
        requested_reason=payload.reason,
    )


@router.get("/agent/sessions/{session_id}", response_model=AgentSessionRead)
def get_agent_session(session_id: str, session: Session = Depends(db)) -> AgentSession:
    return require(session, AgentSession, session_id, "agent session")


@router.get("/agent/sessions/{session_id}/events", response_model=list[AgentEventRead])
def get_agent_session_events(
    session_id: str, session: Session = Depends(db)
) -> list[AgentEvent]:
    require(session, AgentSession, session_id, "agent session")
    return list_agent_events(session, session_id)


@router.get("/agent/sessions/{session_id}/context")
def get_agent_session_context(session_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    value = require(session, AgentSession, session_id, "agent session")
    return {
        "session_id": value.id,
        "context_hash": value.context_hash,
        "context_path": value.context_path,
        "context": value.context_json,
    }


@router.post("/agent/sessions/{session_id}/diagnose", response_model=AgentSessionRead)
async def diagnose_existing_agent_session(
    session_id: str,
    request: Request,
    payload: AgentDiagnoseRequest = AgentDiagnoseRequest(),
    session: Session = Depends(db),
) -> AgentSession:
    existing = require(session, AgentSession, session_id, "agent session")
    if not existing.job_id:
        raise ServiceError(409, "agent session has no Job to diagnose")
    job = require(session, GenerationJob, existing.job_id, "generation job")
    adapter = getattr(request.app.state, "codex_adapter", None)
    if adapter is None:
        from .codex_adapter import UnavailableCodexAdapter

        adapter = UnavailableCodexAdapter()
    return await diagnose_job(
        session,
        job,
        adapter=adapter,
        finding_ids=payload.finding_ids,
        budget=payload.budget,
        requested_reason=payload.reason,
    )


@router.get("/changesets", response_model=list[ChangeSetRead])
def list_changesets(
    project_id: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    session: Session = Depends(db),
) -> list[ChangeSet]:
    statement = select(ChangeSet)
    if project_id:
        statement = statement.where(ChangeSet.project_id == project_id)
    if session_id:
        statement = statement.where(ChangeSet.session_id == session_id)
    if status:
        statement = statement.where(ChangeSet.status == status)
    return list(session.scalars(statement.order_by(ChangeSet.created_at.desc())).all())


@router.get("/changesets/{changeset_id}", response_model=ChangeSetRead)
def get_changeset(changeset_id: str, session: Session = Depends(db)) -> ChangeSet:
    return require(session, ChangeSet, changeset_id, "changeset")


@router.get("/changesets/{changeset_id}/patch", response_class=FileResponse)
def get_changeset_patch(changeset_id: str, session: Session = Depends(db)) -> FileResponse:
    changeset = require(session, ChangeSet, changeset_id, "changeset")
    project = require(session, Project, changeset.project_id, "project")
    try:
        patch_path = ProjectStore(project.root_path).resolve_rendition_path(changeset.patch_path)
    except StorageError as exc:
        raise HTTPException(409, "ChangeSet patch path is outside the registered project") from exc
    if not patch_path.is_file() or sha256_file(patch_path) != changeset.patch_hash:
        raise HTTPException(404, "ChangeSet patch is missing or hash does not match")
    return FileResponse(
        patch_path,
        media_type="text/plain; charset=utf-8",
        filename=f"{changeset.id}.patch",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


def _decide_changeset(
    changeset_id: str,
    *,
    decision: str,
    reason: str | None,
    session: Session,
) -> ChangeSet:
    changeset = require(session, ChangeSet, changeset_id, "changeset")
    if changeset.status != "pending_approval":
        raise ServiceError(409, "changeset has already been decided")
    if decision == "approved":
        project = require(session, Project, changeset.project_id, "project")
        try:
            patch_path = ProjectStore(project.root_path).resolve_rendition_path(changeset.patch_path)
        except StorageError as exc:
            raise ServiceError(409, "ChangeSet patch path is outside the registered project") from exc
        if not patch_path.is_file() or sha256_file(patch_path) != changeset.patch_hash:
            raise ServiceError(409, "ChangeSet patch is missing or hash does not match")
    changeset.status = decision
    changeset.decision_reason = reason
    changeset.decided_at = utcnow()
    agent_session = session.get(AgentSession, changeset.session_id)
    if agent_session:
        agent_session.status = "completed" if decision == "approved" else "awaiting_user"
        agent_session.stop_reason = f"changeset.{decision}"
        agent_session.updated_at = utcnow()
        record_agent_event(
            session,
            agent_session,
            f"changeset.{decision}",
            data={"changeset_id": changeset.id, "reason": reason or ""},
        )
        _persist_agent_audit(session, agent_session)
    session.commit()
    return changeset


@router.post("/changesets/{changeset_id}/approve", response_model=ChangeSetRead)
def approve_changeset(
    changeset_id: str,
    payload: dict[str, str] | None = None,
    session: Session = Depends(db),
) -> ChangeSet:
    # Approval records intent only.  Applying a patch and all Git operations stay
    # outside this API and require a separate, human-controlled workflow.
    return _decide_changeset(
        changeset_id,
        decision="approved",
        reason=(payload or {}).get("reason"),
        session=session,
    )


@router.post("/changesets/{changeset_id}/reject", response_model=ChangeSetRead)
def reject_changeset(
    changeset_id: str,
    payload: dict[str, str] | None = None,
    session: Session = Depends(db),
) -> ChangeSet:
    return _decide_changeset(
        changeset_id,
        decision="rejected",
        reason=(payload or {}).get("reason"),
        session=session,
    )


@router.patch("/generation-plans/{plan_id}/budget", response_model=GenerationPlanRead)
def change_plan_budget(
    plan_id: str,
    payload: PlanBudgetUpdate,
    session: Session = Depends(db),
) -> GenerationPlan:
    plan = require(session, GenerationPlan, plan_id, "generation plan")
    return update_extra_call_budget(
        session,
        plan=plan,
        extra_call_budget=payload.extra_call_budget,
    )


@router.post("/generation-plans/{plan_id}/resume", response_model=list[GenerationJobRead])
def resume_plan(
    plan_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> list[GenerationJob]:
    plan = require(session, GenerationPlan, plan_id, "generation plan")
    return resume_recoverable_jobs(
        session,
        plan=plan,
        available_provider_ids=available_provider_ids(session, credentials),
    )


@router.post("/generation-plans/{plan_id}/confirm", response_model=list[GenerationJobRead])
def approve_plan(
    plan_id: str,
    session: Session = Depends(db),
    credentials: CredentialVault = Depends(vault),
) -> list[GenerationJob]:
    plan = require(session, GenerationPlan, plan_id, "generation plan")
    return confirm_plan(
        session,
        plan,
        available_provider_ids=available_provider_ids(session, credentials),
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


@router.get("/run-events", response_model=list[RunEventRead])
def list_run_events(
    project_id: str | None = None,
    plan_id: str | None = None,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2_000),
    session: Session = Depends(db),
) -> list[RunEvent]:
    statement = select(RunEvent).where(RunEvent.sequence > after)
    if project_id:
        statement = statement.where(RunEvent.project_id == project_id)
    if plan_id:
        statement = statement.where(RunEvent.plan_id == plan_id)
    return list(session.scalars(statement.order_by(RunEvent.sequence).limit(limit)).all())


@router.get("/runs/events")
async def run_events(
    request: Request,
    project_id: str | None = None,
    plan_id: str | None = None,
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    header_cursor = request.headers.get("last-event-id")
    try:
        initial_cursor = max(after, int(header_cursor)) if header_cursor else after
    except ValueError:
        initial_cursor = after

    async def events() -> AsyncIterator[str]:
        cursor = initial_cursor
        last_keepalive = time.monotonic()
        while not await request.is_disconnected():
            database: Database = request.app.state.database
            with database.sessions() as session:
                statement = select(RunEvent).where(RunEvent.sequence > cursor)
                if project_id:
                    statement = statement.where(RunEvent.project_id == project_id)
                if plan_id:
                    statement = statement.where(RunEvent.plan_id == plan_id)
                rows = session.scalars(statement.order_by(RunEvent.sequence).limit(500)).all()
                for row in rows:
                    cursor = row.sequence
                    data = RunEventRead.model_validate(row).model_dump_json()
                    yield f"id: {row.sequence}\nevent: {row.event_type}\ndata: {data}\n\n"
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


@router.get("/findings", response_model=list[FindingRead])
def list_findings(
    plan_id: str | None = None,
    job_id: str | None = None,
    blocking: bool | None = None,
    session: Session = Depends(db),
) -> list[ProductionFinding]:
    statement = select(ProductionFinding)
    if plan_id:
        statement = statement.where(ProductionFinding.plan_id == plan_id)
    if job_id:
        statement = statement.where(ProductionFinding.job_id == job_id)
    if blocking is not None:
        statement = statement.where(ProductionFinding.blocking.is_(blocking))
    return list(
        session.scalars(
            statement.order_by(ProductionFinding.created_at, ProductionFinding.id)
        ).all()
    )


@router.get("/run-evidence", response_model=list[RunEvidenceRead])
def list_run_evidence(
    plan_id: str | None = None,
    job_id: str | None = None,
    session: Session = Depends(db),
) -> list[RunEvidence]:
    statement = select(RunEvidence)
    if plan_id:
        statement = statement.where(RunEvidence.plan_id == plan_id)
    if job_id:
        statement = statement.where(RunEvidence.job_id == job_id)
    return list(session.scalars(statement.order_by(RunEvidence.created_at, RunEvidence.id)).all())


@router.get("/run-evidence/{evidence_id}/content", response_class=FileResponse)
def run_evidence_content(
    evidence_id: str,
    session: Session = Depends(db),
) -> FileResponse:
    evidence = require(session, RunEvidence, evidence_id, "run evidence")
    if not evidence.path or not evidence.media_type:
        raise HTTPException(404, "evidence has no media content")
    plan = require(session, GenerationPlan, evidence.plan_id, "generation plan")
    project = require(session, Project, plan.project_id, "project")
    try:
        content_path = ProjectStore(project.root_path).resolve_rendition_path(evidence.path)
    except StorageError as exc:
        raise HTTPException(409, "evidence path is outside the registered project") from exc
    if not content_path.is_file():
        raise HTTPException(404, "evidence content is missing")
    if evidence.sha256 and sha256_file(content_path) != evidence.sha256:
        raise HTTPException(409, "evidence content hash does not match the persisted record")
    return FileResponse(
        content_path,
        media_type=evidence.media_type,
        filename=None,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.get("/remediations", response_model=list[RemediationRead])
def list_remediations(
    plan_id: str | None = None,
    job_id: str | None = None,
    session: Session = Depends(db),
) -> list[RemediationAction]:
    statement = select(RemediationAction)
    if plan_id:
        statement = statement.where(RemediationAction.plan_id == plan_id)
    if job_id:
        statement = statement.where(RemediationAction.job_id == job_id)
    return list(
        session.scalars(
            statement.order_by(RemediationAction.created_at, RemediationAction.id)
        ).all()
    )


@router.post(
    "/jobs/{job_id}/remediations",
    response_model=RemediationRead,
    status_code=status.HTTP_201_CREATED,
)
def add_remediation(
    job_id: str,
    payload: RemediationCreate,
    session: Session = Depends(db),
) -> RemediationAction:
    job = require(session, GenerationJob, job_id, "generation job")
    return create_remediation(session, job=job, payload=payload)


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
    previous_status = job.status
    if job.status not in {
        GenerationStatus.AWAITING_USER.value,
        GenerationStatus.CREDENTIALS_LOCKED.value,
    }:
        raise ServiceError(409, "use an explicit remediation action to repeat completed calls")
    profile = require(session, ProviderProfile, job.provider_profile_id, "provider profile")
    has_recoverable_output = False
    has_recoverable_action = False
    if job.status == GenerationStatus.AWAITING_USER.value:
        latest = session.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == job.id)
            .order_by(GenerationAttempt.number.desc())
            .limit(1)
        )
        has_recoverable_output = attempt_output_is_valid(session, job, latest)
        pending_action = (
            session.get(RemediationAction, job.pending_action_id)
            if job.pending_action_id
            else None
        )
        has_recoverable_action = bool(
            pending_action
            and pending_action.action == RemediationKind.TOOL_REPAIR.value
            and pending_action.status
            in {
                RemediationStatus.ACCEPTED.value,
                RemediationStatus.RUNNING.value,
            }
        )
        if not has_recoverable_action and not has_recoverable_output:
            raise ServiceError(
                409,
                "provider delivery is not safely resumable; choose retry, repair, regenerate, or await user",
            )
    job.status = (
        GenerationStatus.QUEUED.value
        if profile.kind == ProviderKind.FAKE.value
        or credentials.is_unlocked(profile.id)
        or has_recoverable_output
        or has_recoverable_action
        else GenerationStatus.CREDENTIALS_LOCKED.value
    )
    job.cancel_requested = False
    job.progress = 0.0
    job.error_category = None
    job.error_message = None
    job.updated_at = utcnow()
    if job.status != previous_status:
        record_run_event(
            session,
            plan_id=job.plan_id,
            project_id=job.project_id,
            job_id=job.id,
            asset_id=str(job.request_json.get("asset_id")),
            event_type="run.resumed",
            stage=job.stage,
            data={
                "previous_status": previous_status,
                "recoverable_output": has_recoverable_output,
                "recoverable_action": has_recoverable_action,
            },
        )
        plan = session.get(GenerationPlan, job.plan_id)
        if plan:
            refresh_plan_status(session, plan)
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


@router.get("/projects/{project_id}/export-config", response_model=ExportConfigRead)
def get_export_config(project_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    project = require(session, Project, project_id, "project")
    store = ProjectStore(project.root_path)
    try:
        return {
            "project_id": project.id,
            "project": store.export_contract(),
            "local": store.local_export_config(),
        }
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.put("/projects/{project_id}/export-config", response_model=ExportConfigRead)
def put_export_config(
    project_id: str,
    payload: ExportConfigUpdate,
    session: Session = Depends(db),
) -> dict[str, Any]:
    project = require(session, Project, project_id, "project")
    store = ProjectStore(project.root_path)
    try:
        local = store.update_local_export_config(game_root=payload.game_root)
        return {"project_id": project.id, "project": store.export_contract(), "local": local}
    except StorageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/releases/{release_id}/preflight", response_model=ReleasePreflightRead)
def preflight_release_endpoint(
    release_id: str,
    project_id: str | None = None,
    session: Session = Depends(db),
) -> dict[str, Any]:
    if project_id is None:
        release = session.get(Release, release_id)
        if release is None:
            raise HTTPException(status_code=404, detail=f"release not found: {release_id}")
        project_id = release.project_id
    try:
        return release_preflight(session, project_id=project_id, release_id=release_id)
    except DeliveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/exports/preview")
def export_preview_endpoint(payload: ExportRequest, session: Session = Depends(db)) -> dict[str, Any]:
    try:
        return export_preview(
            session,
            project_id=payload.project_id,
            release_id=payload.release_id,
            game_root=payload.game_root,
        )
    except DeliveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/exports/apply", response_model=DeliveryRead, status_code=status.HTTP_201_CREATED)
def export_apply_endpoint(payload: ExportRequest, session: Session = Depends(db)) -> Delivery:
    try:
        return apply_export(
            session,
            project_id=payload.project_id,
            release_id=payload.release_id,
            game_root=payload.game_root,
            run_commands=payload.run_commands,
        )
    except DeliveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/exports/verify")
def export_verify_endpoint(
    payload: ExportVerifyRequest, session: Session = Depends(db)
) -> dict[str, Any]:
    try:
        return verify_export(
            session,
            project_id=payload.project_id,
            release_id=payload.release_id,
            game_root=payload.game_root,
            run_commands=payload.run_commands,
        )
    except DeliveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.post("/exports/rollback", response_model=DeliveryRead, status_code=status.HTTP_201_CREATED)
def export_rollback_endpoint(
    payload: ExportRollbackRequest, session: Session = Depends(db)
) -> Delivery:
    try:
        preview = export_preview(
            session,
            project_id=payload.project_id,
            release_id=payload.release_id,
            game_root=payload.game_root,
        )
        return apply_export(
            session,
            project_id=payload.project_id,
            release_id=payload.release_id,
            game_root=payload.game_root,
            run_commands=payload.run_commands,
            rollback_from=preview.get("previous_release_id"),
        )
    except DeliveryError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/deliveries", response_model=list[DeliveryRead])
def get_deliveries(project_id: str, session: Session = Depends(db)) -> list[Delivery]:
    require(session, Project, project_id, "project")
    return list_deliveries(session, project_id=project_id)
