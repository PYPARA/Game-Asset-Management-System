from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from .database import Database
from .domain import (
    AgentDiagnoseRequest,
    AgentEventRead,
    AgentSessionCreate,
    AgentSessionRead,
    GenerationAgentCapabilitiesRead,
    GenerationConversationConfirm,
    GenerationConversationConfirmRead,
    GenerationConversationContextUpdate,
    GenerationConversationCreate,
    GenerationConversationDraftUpdate,
    GenerationConversationEventRead,
    GenerationConversationMessageCreate,
    GenerationConversationMessageRead,
    GenerationConversationRead,
    GenerationConversationSettingsUpdate,
    GenerationConversationSteerCreate,
    GenerationInputAnswerCreate,
    GenerationInputAnswerRead,
    GenerationConversationSummary,
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
    LegacyMediaMigrationCreate,
    LegacyMediaMigrationResult,
    NarrativeRequirementMaterialize,
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
    ProviderModelRead,
    ProviderModelsRead,
    ProviderModelsSyncRead,
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
    validate_models_path,
)
from .provider_catalog import (
    apply_model_overrides,
    ensure_routing_defaults,
    merge_discovered_models,
    model_is_compatible,
    normalize_model_catalog,
    provider_credentials_ready,
    provider_models_sync,
    update_models_sync,
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
from .generation_planning import (
    GenerationPlanningRunner,
    append_generation_message,
    build_planning_context,
    confirm_generation_conversation,
    create_generation_conversation,
    draft_hash,
    ensure_generation_context_v2,
    get_generation_events,
    find_generation_message,
    list_generation_conversations,
    archive_generation_conversation,
    remove_generation_conversation_artifacts,
    replace_generation_conversation_context,
    stream_generation_events,
    unarchive_generation_conversation,
    update_generation_draft,
    update_generation_conversation_settings,
    pending_input_request,
    public_input_request,
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
    migrate_legacy_media_approvals,
    register_project,
    require,
    run_qa,
    scan_project,
    update_project,
)
from .storage import ProjectStore, StorageError, sha256_file
from .narrative import build_narrative_map, materialize_scene_requirements


router = APIRouter(prefix="/api")


def db(request: Request) -> Iterator[Session]:
    database: Database = request.app.state.database
    yield from database.session()


def vault(request: Request) -> CredentialVault:
    return request.app.state.vault


def provider_view(profile: ProviderProfile, credentials: CredentialVault) -> ProviderRead:
    view = ProviderRead.model_validate(profile)
    return view.model_copy(
        update={
            "models": [
                ProviderModelRead.model_validate(model)
                for model in normalize_model_catalog(profile.models_json or [])
            ],
            "models_path": profile.models_path or "models",
            "is_unlocked": provider_credentials_ready(
                profile, unlocked=credentials.is_unlocked(profile.id)
            ),
            "models_sync": ProviderModelsSyncRead.model_validate(provider_models_sync(profile)),
        }
    )


def available_provider_ids(
    session: Session, credentials: CredentialVault
) -> set[str]:
    return {
        profile.id
        for profile in session.scalars(select(ProviderProfile)).all()
        if provider_credentials_ready(profile, unlocked=credentials.is_unlocked(profile.id))
    }


def provider_models_view(
    profile: ProviderProfile,
    *,
    new_model_ids: list[str] | None = None,
    cleared_default_routes: list[str] | None = None,
) -> ProviderModelsRead:
    return ProviderModelsRead(
        provider_profile_id=profile.id,
        models=normalize_model_catalog(profile.models_json or []),
        refreshed_at=profile.models_refreshed_at,
        new_model_ids=new_model_ids or [],
        cleared_default_routes=cleared_default_routes or [],
        models_sync=ProviderModelsSyncRead.model_validate(provider_models_sync(profile)),
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
        video=(
            ProviderDefaultRoute(
                provider_profile_id=defaults.video_provider_profile_id,
                model=defaults.video_model,
            )
            if defaults.video_provider_profile_id and defaults.video_model
            else None
        ),
        audio=(
            ProviderDefaultRoute(
                provider_profile_id=defaults.audio_provider_profile_id,
                model=defaults.audio_model,
            )
            if defaults.audio_provider_profile_id and defaults.audio_model
            else None
        ),
        max_concurrency=defaults.max_concurrency,
        max_transport_retries=defaults.max_transport_retries,
        updated_at=defaults.updated_at,
    )


def provider_http_error(
    exc: ProviderError,
    *,
    fallback_endpoint: str | None = None,
    fallback_hint: str | None = None,
) -> HTTPException:
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
    return HTTPException(
        mapping.get(exc.category.value, 502),
        detail={
            "category": exc.category.value,
            **({"error_code": exc.error_code} if exc.error_code else {}),
            "message": str(exc),
            "status_code": exc.status_code,
            "endpoint": exc.endpoint or fallback_endpoint,
            "request_id": exc.request_id,
            "hint": exc.hint or fallback_hint,
        },
    )


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


@router.post(
    "/projects/{project_id}/legacy-media-promotions",
    response_model=LegacyMediaMigrationResult,
)
def promote_legacy_media(
    project_id: str,
    payload: LegacyMediaMigrationCreate,
    session: Session = Depends(db),
) -> dict[str, Any]:
    """Explicitly upgrade selected pre-M1 media approvals to durable Artifacts."""

    return migrate_legacy_media_approvals(
        session,
        project_id=project_id,
        asset_keys=payload.asset_keys,
    )


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


@router.get("/projects/{project_id}/narrative-map")
def narrative_map(project_id: str, session: Session = Depends(db)) -> dict[str, Any]:
    """Return the chapter tree, scene graph and requirement coverage from Project facts."""

    return build_narrative_map(session, project_id)


@router.post("/projects/{project_id}/narrative-map/scenes/{scene_asset_id}/requirements")
def materialize_narrative_requirements(
    project_id: str,
    scene_asset_id: str,
    payload: NarrativeRequirementMaterialize,
    session: Session = Depends(db),
) -> dict[str, Any]:
    """Create missing Catalog descriptors before handing them to the M2 plan editor.

    This operation deliberately does not create a second planning or identity model:
    returned IDs are ordinary Asset IDs and subsequent generation uses the existing
    GenerationPlan/Job/Artifact/Review pipeline.
    """

    return materialize_scene_requirements(
        session,
        project_id=project_id,
        scene_asset_id=scene_asset_id,
        requirement_ids=payload.requirement_ids,
    )


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
    try:
        models_path = validate_models_path(payload.models_path)
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
        credential_mode=payload.credential_mode.value,
        model_discovery_mode=payload.model_discovery_mode.value,
        models_path=models_path,
        is_active=True,
        models_json=[],
    )
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
    if "models_path" in changes:
        try:
            changes["models_path"] = validate_models_path(changes["models_path"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    for field in ("text_model", "image_model"):
        if field in changes and changes[field] is None:
            changes[field] = ""
    for field, value in changes.items():
        setattr(profile, field, value)
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
    for modality in ("text", "image", "video", "audio"):
        if getattr(defaults, f"{modality}_provider_profile_id") == profile.id:
            setattr(defaults, f"{modality}_provider_profile_id", None)
            setattr(defaults, f"{modality}_model", None)
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
    for modality in ("text", "image", "video", "audio"):
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
                f"default {modality} model {model} is not enabled and classified for {modality}",
            )
        setattr(defaults, f"{modality}_provider_profile_id", profile.id)
        setattr(defaults, f"{modality}_model", model)
    defaults.max_concurrency = payload.max_concurrency
    defaults.max_transport_retries = payload.max_transport_retries
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
    defaults = ensure_routing_defaults(session)
    removed_model_ids = {
        model_id.strip()
        for model_id in payload.removed_model_ids
        if model_id.strip()
    }
    if any(len(model_id) > 240 for model_id in removed_model_ids):
        raise HTTPException(422, "removed model id exceeds 240 characters")
    updated_model_ids = {item.id for item in payload.models}
    overlap = removed_model_ids & updated_model_ids
    if overlap:
        raise HTTPException(
            422,
            f"model {sorted(overlap, key=str.lower)[0]} cannot be updated and removed together",
        )
    existing_ids = {
        str(item.get("id"))
        for item in profile.models_json or []
        if item.get("id")
    }
    retained_models = [
        item
        for item in profile.models_json or []
        if str(item.get("id", "")).strip() not in removed_model_ids
    ]
    profile.models_json = apply_model_overrides(
        retained_models,
        [item.model_dump(mode="json", exclude_unset=True) for item in payload.models],
    )
    if profile.text_model in removed_model_ids:
        profile.text_model = ""
    if profile.image_model in removed_model_ids:
        profile.image_model = ""
    cleared_default_routes: list[str] = []
    for modality in ("text", "image", "video", "audio"):
        provider_field = f"{modality}_provider_profile_id"
        model_field = f"{modality}_model"
        if getattr(defaults, provider_field) != profile.id:
            continue
        model_id = str(getattr(defaults, model_field) or "").strip()
        if model_id and model_is_compatible(profile, model_id, modality):
            continue
        setattr(defaults, provider_field, None)
        setattr(defaults, model_field, None)
        cleared_default_routes.append(modality)
    if cleared_default_routes:
        defaults.updated_at = utcnow()
    if payload.catalog_refreshed:
        endpoint_path = validate_models_path(profile.models_path)
        endpoint = f"{profile.base_url.rstrip('/')}/{endpoint_path}"
        available_count = sum(
            1 for item in profile.models_json or [] if item.get("available") is True
        )
        profile.models_refreshed_at = utcnow()
        update_models_sync(
            profile,
            state="synced" if available_count else "empty",
            endpoint=endpoint,
            status_code=200,
            message=(
                "模型目录已同步。"
                if available_count
                else "供应商返回了空模型列表。"
            ),
            hint=(
                None
                if available_count
                else "请检查供应商模型权限，或手动登记模型 ID。"
            ),
            request_id=payload.request_id,
        )
    profile.updated_at = utcnow()
    session.commit()
    current_ids = {
        str(item.get("id"))
        for item in profile.models_json or []
        if item.get("id")
    }
    return provider_models_view(
        profile,
        new_model_ids=sorted(current_ids - existing_ids, key=str.lower),
        cleared_default_routes=cleared_default_routes,
    )


async def refresh_provider_model_catalog(
    profile: ProviderProfile,
    session: Session,
    credentials: CredentialVault,
) -> ProviderModelsRead:
    try:
        endpoint_path = validate_models_path(profile.models_path)
    except ValueError as exc:
        endpoint = f"{profile.base_url.rstrip('/')}/models"
        update_models_sync(
            profile,
            state="error",
            endpoint=endpoint,
            message="模型列表路径无效。",
            hint="请输入不带协议、查询参数或片段的相对路径，例如 models。",
        )
        profile.updated_at = utcnow()
        session.commit()
        raise HTTPException(
            422,
            detail={
                "category": "validation",
                "message": str(exc),
                "status_code": 422,
                "endpoint": endpoint,
                "hint": "请输入不带协议、查询参数或片段的相对路径，例如 models。",
            },
        ) from exc
    endpoint = f"{profile.base_url.rstrip('/')}/{endpoint_path}"
    if profile.model_discovery_mode == "manual":
        update_models_sync(
            profile,
            state="manual_required",
            endpoint=endpoint,
            message="该供应商已设置为手动登记模型。",
            hint="在模型目录中登记需要使用的模型 ID。",
        )
        profile.updated_at = utcnow()
        session.commit()
        return provider_models_view(profile)
    try:
        provider = build_provider(profile, credentials)
        discovered = await provider.discover_models()
    except ProviderError as exc:
        if exc.status_code in {404, 405}:
            update_models_sync(
                profile,
                state="manual_required",
                endpoint=exc.endpoint or endpoint,
                status_code=exc.status_code,
                message="供应商未提供可用的模型列表接口。",
                hint="连接仍可使用；请在模型目录中手动登记模型 ID。",
                request_id=exc.request_id,
            )
            profile.updated_at = utcnow()
            session.commit()
            return provider_models_view(profile)
        fallback_hint = "检查 Base URL、模型列表路径和本机网络连通性，或根据错误分类修复供应商配置。"
        update_models_sync(
            profile,
            state="error",
            endpoint=exc.endpoint or endpoint,
            status_code=exc.status_code,
            message=str(exc),
            hint=exc.hint or fallback_hint,
            request_id=exc.request_id,
        )
        profile.updated_at = utcnow()
        session.commit()
        raise provider_http_error(
            exc,
            fallback_endpoint=endpoint,
            fallback_hint=fallback_hint,
        ) from exc
    existing_ids = {
        str(item.get("id"))
        for item in profile.models_json or []
        if item.get("id")
    }
    discovered_ids = {
        str(item.get("id"))
        for item in discovered
        if item.get("id")
    }
    profile.models_json = merge_discovered_models(profile.models_json or [], discovered)
    profile.models_refreshed_at = utcnow()
    update_models_sync(
        profile,
        state="synced" if discovered else "empty",
        endpoint=endpoint,
        status_code=200,
        message=("模型目录已同步。" if discovered else "供应商返回了空模型列表。"),
        hint=("已自动兼容 Fake-IP DNS（fake_ip_compatible），继续使用域名及 TLS 校验。"
              if getattr(provider, "network_policy", None) == "fake_ip_compatible"
              else None if discovered else "请检查供应商模型权限，或手动登记模型 ID。"),
    )
    profile.updated_at = utcnow()
    session.commit()
    return provider_models_view(
        profile,
        new_model_ids=sorted(discovered_ids - existing_ids, key=str.lower),
    )


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
            if model.get("id")
            and model.get("available", True)
            and model.get("enabled", True)
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


def _planning_session(session: Session, session_id: str) -> AgentSession:
    value = require(session, AgentSession, session_id, "generation conversation")
    if value.purpose != "generation_planning":
        raise ServiceError(409, "agent session is not a generation planning conversation")
    return value


def _generation_seed_ids(row: AgentSession, *, include_context_fallback: bool) -> list[str]:
    values = (row.sandbox_json or {}).get("seed_asset_ids")
    if not isinstance(values, list) and include_context_fallback:
        values = (row.context_json or {}).get("seed_asset_ids")
    return sorted({str(value) for value in values or [] if value})


def _generation_conversation_summary(row: AgentSession) -> dict[str, Any]:
    result = dict(row.result_json or {})
    return {
        "id": row.id,
        "project_id": row.project_id,
        "plan_id": row.plan_id,
        "title": row.title,
        "agent_model": row.agent_model,
        "status": row.status,
        "turn_count": int(row.turn_count or 0),
        "budget_limit": int(row.budget_limit or 0),
        "budget_used": int(row.budget_used or 0),
        "seed_asset_ids": _generation_seed_ids(row, include_context_fallback=False),
        "error_summary": result.get("last_error"),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
        "archived_at": row.archived_at,
    }


def _generation_conversation_detail(row: AgentSession) -> dict[str, Any]:
    result = dict(row.result_json or {})
    seed_asset_ids = _generation_seed_ids(row, include_context_fallback=True)
    raw_context = dict(row.context_json or {})
    # Context v2 remains a server-side/read-only workspace artifact. The web
    # view gets only the fields needed to explain and edit the current draft.
    context = {
        "context_version": int(raw_context.get("context_version", 2)),
        "read_only": True,
        "seed_asset_ids": seed_asset_ids,
        "project": raw_context.get("project", {"id": row.project_id}),
    }
    attached = object_session(row)
    pending = pending_input_request(attached, row.id) if attached is not None else None
    from .planning_runtime import runtime_detail
    return {
        "id": row.id,
        "project_id": row.project_id,
        "plan_id": row.plan_id,
        "thread_id": row.thread_id,
        "purpose": row.purpose,
        "title": row.title,
        "agent_model": row.agent_model,
        "status": row.status,
        "context_hash": row.context_hash,
        "seed_asset_ids": seed_asset_ids,
        "context": context,
        "draft": dict(row.draft_json or {}),
        "draft_hash": row.draft_hash,
        "draft_version": int(row.draft_version or 0),
        "budget_limit": int(row.budget_limit or 0),
        "budget_used": int(row.budget_used or 0),
        "turn_count": int(row.turn_count or 0),
        "diagnostic_reason": row.diagnostic_reason,
        "stop_reason": row.stop_reason,
        "usage": result.get("usage") if isinstance(result.get("usage"), dict) else {},
        "last_error": result.get("last_error") if isinstance(result.get("last_error"), dict) else None,
        "pending_input": public_input_request(pending) if pending else None,
        **runtime_detail(attached, row),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "completed_at": row.completed_at,
        "archived_at": row.archived_at,
    }


@router.get("/generation-agent/capabilities", response_model=GenerationAgentCapabilitiesRead)
async def generation_agent_capabilities(request: Request) -> dict[str, Any]:
    """Return an actionable local Codex/App Server diagnostic."""

    adapter = getattr(request.app.state, "codex_adapter", None)
    if adapter is None:
        from .codex_adapter import UnavailableCodexAdapter

        adapter = UnavailableCodexAdapter()
    capabilities = getattr(adapter, "capabilities", None)
    if capabilities is None:
        return {
            "available": False,
            "adapter": str(getattr(adapter, "name", "unknown")),
            "version": str(getattr(adapter, "version", "unknown")),
            "models": [],
            "diagnostic": {
                "code": "capabilities_unsupported",
                "message": "当前规划适配器没有提供模型列表。",
                "hint": "可以继续使用右侧人工编排。",
            },
        }
    try:
        value = await asyncio.wait_for(capabilities(), timeout=10.0)
    except asyncio.TimeoutError:
        return {
            "available": False,
            "adapter": str(getattr(adapter, "name", "unknown")),
            "version": str(getattr(adapter, "version", "unknown")),
            "models": [],
            "diagnostic": {
                "code": "capabilities_timeout",
                "message": "Codex App Server 能力检测超时。",
                "hint": "确认本机 Codex 已安装并登录；也可以继续右侧人工编排。",
            },
        }
    except Exception as exc:
        return {
            "available": False,
            "adapter": str(getattr(adapter, "name", "unknown")),
            "version": str(getattr(adapter, "version", "unknown")),
            "models": [],
            "diagnostic": {
                "code": "capabilities_failed",
                "message": "无法连接本机 Codex App Server。",
                "hint": f"{str(exc)[:400]}。检查 Codex 登录状态或继续人工编排。",
            },
        }
    return {**value, "interactive_user_input": bool(value.get("available") and getattr(adapter, "interactive_user_input", False))}


async def _best_effort_thread_action(
    request: Request,
    action: str,
    thread_id: str | None,
) -> None:
    callback = getattr(getattr(request.app.state, "codex_adapter", None), action, None)
    if callback is None:
        return
    try:
        await asyncio.wait_for(callback(thread_id), timeout=8.0)
    except Exception:
        # Conversation lifecycle is local-first. A missing or unavailable
        # App Server must not make archive/delete unusable.
        return


@router.post(
    "/generation-conversations",
    response_model=GenerationConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_generation_conversation_route(
    payload: GenerationConversationCreate,
    request: Request,
    session: Session = Depends(db),
) -> dict[str, Any]:
    adapter = getattr(request.app.state, "codex_adapter", None)
    if adapter is None:
        from .codex_adapter import UnavailableCodexAdapter

        adapter = UnavailableCodexAdapter()
    settings = getattr(request.app.state, "settings", None)
    budget = int(getattr(settings, "planning_agent_budget", 8))
    row = create_generation_conversation(
        session,
        payload,
        adapter=adapter,
        planning_budget=budget,
    )
    return _generation_conversation_detail(row)


@router.get("/generation-conversations", response_model=list[GenerationConversationSummary])
def list_generation_conversations_route(
    project_id: str | None = None,
    include_archived: bool = Query(default=False),
    session: Session = Depends(db),
) -> list[dict[str, Any]]:
    rows = list_generation_conversations(session, project_id, include_archived=include_archived)
    return [_generation_conversation_summary(row) for row in rows]


@router.get("/generation-conversations/{session_id}", response_model=GenerationConversationRead)
def get_generation_conversation_route(
    session_id: str,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = ensure_generation_context_v2(session, _planning_session(session, session_id))
    return _generation_conversation_detail(row)


@router.patch(
    "/generation-conversations/{session_id}/settings",
    response_model=GenerationConversationRead,
)
def patch_generation_conversation_settings(
    session_id: str,
    payload: GenerationConversationSettingsUpdate,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    if payload.title is not None:
        if not payload.title.strip():
            raise ServiceError(422, "会话标题不能为空")
        row.title = payload.title.strip()
    if "agent_model" in payload.model_fields_set:
        row = update_generation_conversation_settings(session, row, agent_model=payload.agent_model)
    else:
        row.updated_at = utcnow()
        session.commit()
    return _generation_conversation_detail(row)


@router.patch(
    "/generation-conversations/{session_id}/context",
    response_model=GenerationConversationRead,
)
def patch_generation_conversation_context(
    session_id: str,
    payload: GenerationConversationContextUpdate,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    row = replace_generation_conversation_context(session, row, seed_asset_ids=payload.seed_asset_ids)
    return _generation_conversation_detail(row)


@router.post(
    "/generation-conversations/{session_id}/archive",
    response_model=GenerationConversationRead,
)
async def archive_generation_conversation_route(
    session_id: str,
    request: Request,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    await _best_effort_thread_action(request, "archive_thread", row.thread_id)
    row = archive_generation_conversation(session, row)
    return _generation_conversation_detail(row)


@router.post(
    "/generation-conversations/{session_id}/unarchive",
    response_model=GenerationConversationRead,
)
async def unarchive_generation_conversation_route(
    session_id: str,
    request: Request,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    await _best_effort_thread_action(request, "unarchive_thread", row.thread_id)
    row = unarchive_generation_conversation(session, row)
    return _generation_conversation_detail(row)


@router.delete("/generation-conversations/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_generation_conversation_route(
    session_id: str,
    request: Request,
    session: Session = Depends(db),
) -> Response:
    row = _planning_session(session, session_id)
    if row.status in {"running", "awaiting_input"}:
        raise ServiceError(409, "stop the active planning turn before permanently deleting the conversation")
    project = require(session, Project, row.project_id, "project")
    conversation_id = row.id
    await _best_effort_thread_action(request, "delete_thread", row.thread_id)
    # AgentSession.plan_id/job_id historically used CASCADE FKs. Null them and
    # their audit references first so deleting chat history can never delete a
    # confirmed Plan, Job, or asset.
    events = list(session.scalars(select(AgentEvent).where(AgentEvent.session_id == row.id)).all())
    for event in events:
        event.plan_id = None
        event.job_id = None
        event.asset_id = None
    row.plan_id = None
    row.job_id = None
    row.asset_id = None
    session.delete(row)
    session.commit()
    remove_generation_conversation_artifacts(conversation_id, project)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/generation-conversations/{session_id}/messages",
    response_model=GenerationConversationMessageRead,
)
async def post_generation_conversation_message(
    session_id: str,
    payload: GenerationConversationMessageCreate,
    request: Request,
    session: Session = Depends(db),
) -> GenerationConversationMessageRead:
    row = _planning_session(session, session_id)
    runner = getattr(
        request.app.state,
        "generation_planning_runner",
        getattr(request.app.state, "planning_runner", None),
    )
    if runner is None:
        raise ServiceError(503, "generation planning runner is unavailable")
    existing_message = find_generation_message(session, row, payload.client_message_id)
    if existing_message is not None:
        turn_id, sequence = existing_message
        return GenerationConversationMessageRead(
            turn_id=turn_id,
            status=row.status,
            next_sequence=sequence,
        )
    turn_id, next_sequence = append_generation_message(
        session,
        row,
        payload.content,
        context_asset_ids=payload.context_asset_ids,
        client_message_id=payload.client_message_id,
    )
    try:
        runner.submit(session_id, turn_id)
    except Exception as exc:
        # The message remains auditable, but the session must immediately offer
        # the manual editor instead of looking permanently busy.
        with request.app.state.database.sessions() as recovery:
            failed = recovery.get(AgentSession, session_id)
            if failed is not None:
                failed.status = "awaiting_user"
                failed.stop_reason = "runner.unavailable"
                failed.updated_at = utcnow()
                record_agent_event(recovery, failed, "turn.failed", data={"reason": "runner.unavailable"}, turn_id=turn_id)
                recovery.commit()
        raise ServiceError(503, f"generation planning runner is unavailable: {exc}") from exc
    return GenerationConversationMessageRead(turn_id=turn_id, status="running", next_sequence=next_sequence)


@router.post(
    "/generation-conversations/{session_id}/steer",
    response_model=GenerationConversationMessageRead,
)
async def steer_generation_conversation_turn(
    session_id: str,
    payload: GenerationConversationSteerCreate,
    request: Request,
    session: Session = Depends(db),
) -> GenerationConversationMessageRead:
    row = _planning_session(session, session_id)
    prior = find_generation_message(session, row, payload.client_message_id)
    if prior:
        return GenerationConversationMessageRead(turn_id=prior[0],status=row.status,next_sequence=prior[1])
    if row.status != "running":
        raise ServiceError(409, "this conversation has no active turn")
    runner = getattr(
        request.app.state,
        "generation_planning_runner",
        getattr(request.app.state, "planning_runner", None),
    )
    if runner is None:
        raise ServiceError(503, "generation planning runner is unavailable")
    turn_id, next_sequence = await runner.steer(session_id, payload.content, client_message_id=payload.client_message_id)
    return GenerationConversationMessageRead(
        turn_id=turn_id,
        status="running",
        next_sequence=next_sequence,
    )


@router.post(
    "/generation-conversations/{session_id}/input-requests/{input_id}/answer",
    response_model=GenerationInputAnswerRead,
)
async def answer_generation_input(
    session_id: str, input_id: str, payload: GenerationInputAnswerCreate,
    request: Request, session: Session = Depends(db),
) -> GenerationInputAnswerRead:
    _planning_session(session, session_id)
    runner = request.app.state.generation_planning_runner
    turn_id, sequence = await runner.answer_input(
        session_id, input_id, payload.client_response_id, payload.answers,
    )
    return GenerationInputAnswerRead(
        request_id=input_id, turn_id=turn_id, status="running", next_sequence=sequence,
    )


@router.get(
    "/generation-conversations/{session_id}/events",
    response_model=list[GenerationConversationEventRead],
)
def get_generation_conversation_events(
    session_id: str,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2_000),
    session: Session = Depends(db),
) -> list[AgentEvent]:
    _planning_session(session, session_id)
    return get_generation_events(session, session_id, after_sequence=after, limit=limit)


@router.get("/generation-conversations/{session_id}/events/stream")
async def stream_generation_conversation_events(
    session_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    session: Session = Depends(db),
) -> StreamingResponse:
    _planning_session(session, session_id)
    last_event_id = request.headers.get("last-event-id")
    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return StreamingResponse(
        stream_generation_events(request.app.state.database, session_id, request, after_sequence=cursor),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.patch(
    "/generation-conversations/{session_id}/draft",
    response_model=GenerationConversationRead,
)
def patch_generation_conversation_draft(
    session_id: str,
    payload: GenerationConversationDraftUpdate,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    row = update_generation_draft(session, row, payload)
    return _generation_conversation_detail(row)


@router.post(
    "/generation-conversations/{session_id}/confirm",
    response_model=GenerationConversationConfirmRead,
)
def confirm_generation_conversation_route(
    session_id: str,
    payload: GenerationConversationConfirm,
    request: Request,
    session: Session = Depends(db),
) -> dict[str, Any]:
    # Serialize confirmation before reading the draft; a simultaneous retry
    # observes the committed immutable batch instead of creating another plan.
    if session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
    row = _planning_session(session, session_id)
    result = confirm_generation_conversation(
        session,
        row,
        payload,
        available_provider_ids=available_provider_ids(session, vault(request)),
    )
    result["conversation"] = _generation_conversation_detail(result["conversation"])
    return result


@router.post(
    "/generation-conversations/{session_id}/cancel-turn",
    response_model=GenerationConversationRead,
)
async def cancel_generation_conversation_turn(
    session_id: str,
    request: Request,
    session: Session = Depends(db),
) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    runner = getattr(
        request.app.state,
        "generation_planning_runner",
        getattr(request.app.state, "planning_runner", None),
    )
    if runner is not None:
        await runner.cancel(session_id)
    else:
        row.status = "awaiting_user"
        row.stop_reason = "user.cancelled"
        row.result_json = {
            **dict(row.result_json or {}),
            "last_error": {
                "error_code": "interrupted",
                "reason": "user.cancelled",
                "message": "本轮已由用户停止，已有消息和草案仍然保留。",
                "retryable": True,
            },
        }
        session.commit()
    with request.app.state.database.sessions() as refreshed:
        return _generation_conversation_detail(_planning_session(refreshed, session_id))


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
        if latest and latest.dispatch_state == "not_sent" and not job.pending_action_id:
            create_remediation(session, job=job, payload=RemediationCreate(
                action=RemediationKind.RETRY, strategy="same-request", reason="继续执行已确认任务",
                idempotency_key=f"resume:{job.id}:{job.attempt_count}",
            ))
            return job
        if not has_recoverable_action and not has_recoverable_output:
            raise ServiceError(
                409,
                "provider delivery is not safely resumable; choose retry, repair, regenerate, or await user",
            )
    job.status = (
        GenerationStatus.QUEUED.value
        if provider_credentials_ready(profile, unlocked=credentials.is_unlocked(profile.id))
        or has_recoverable_output
        or has_recoverable_action
        else GenerationStatus.CREDENTIALS_LOCKED.value
    )
    job.cancel_requested = False
    job.progress = 0.0
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


@router.post("/generation-conversations/{session_id}/turns/{turn_id}/recover")
async def recover_generation_turn(session_id: str, turn_id: str, payload: dict[str, str], request: Request, session: Session = Depends(db)) -> dict[str, Any]:
    from .planning_runtime import recover_turn, backfill_planning
    key = payload.get("client_request_id", "")
    if not key or len(key) > 160:
        raise ServiceError(422, "client_request_id is required (1–160 characters)")
    row = _planning_session(session, session_id)
    backfill_planning(session)
    attempt = recover_turn(session, row, turn_id, key)
    session.commit()
    runner = request.app.state.generation_planning_runner
    if attempt.status == "queued" and session_id not in getattr(runner, "_tasks", {}):
        runner.submit(session_id, turn_id)
    return {"attempt_id": attempt.id, "turn_id": turn_id, "status": attempt.status}


@router.post("/generation-conversations/{session_id}/proposal")
def resolve_generation_proposal(session_id: str, payload: dict[str, Any], session: Session = Depends(db)) -> dict[str, Any]:
    row = _planning_session(session, session_id)
    proposed = (row.result_json or {}).get("pending_proposal")
    if not proposed:
        raise ServiceError(409, "没有待处理提案")
    if payload.get("action") == "apply":
        update_generation_draft(session, row, GenerationConversationDraftUpdate(base_hash=payload.get("base_hash", ""), draft=proposed))
    elif payload.get("action") != "keep":
        raise ServiceError(422, "action must be apply or keep")
    row.result_json = {k: v for k, v in dict(row.result_json or {}).items() if k != "pending_proposal"}
    session.commit()
    return _generation_conversation_detail(row)
