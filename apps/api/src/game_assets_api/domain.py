from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


STABLE_KEY_RE = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")


class AssetKind(StrEnum):
    CONTENT = "content"
    DESIGN = "design"
    ENTITY = "entity"
    MEDIA = "media"
    PRODUCTION = "production"


class ContentStatus(StrEnum):
    DRAFT = "draft"
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"


class GenerationStatus(StrEnum):
    IDLE = "idle"
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    QA_FAILED = "qa_failed"
    OUTPUT_RECEIVED = "output_received"
    HARD_QA = "hard_qa"
    SEMANTIC_QA = "semantic_qa"
    CANDIDATE_READY = "candidate_ready"
    REMEDIATING = "remediating"
    AWAITING_USER = "awaiting_user"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CREDENTIALS_LOCKED = "credentials_locked"


class PublicationStatus(StrEnum):
    UNPUBLISHED = "unpublished"
    READY = "ready"
    PUBLISHED = "published"
    BLOCKED = "blocked"


class RelationType(StrEnum):
    DEPICTS = "depicts"
    ILLUSTRATES = "illustrates"
    REPRESENTS = "represents"
    DEPENDS_ON = "depends_on"
    APPEARS_IN = "appears_in"
    REFERENCES = "references"
    CONTAINS = "contains"


class RevisionFormat(StrEnum):
    JSON = "json"
    MARKDOWN = "markdown"
    MEDIA = "media"


class ProviderKind(StrEnum):
    OPENAI_COMPATIBLE = "openai_compatible"
    FAKE = "fake"


class ProviderCredentialMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    NONE = "none"


class ProviderModelDiscoveryMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


class TaskKind(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    IMAGE_EDIT = "image_edit"


class ReviewVerdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class QAVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"


class ErrorCategory(StrEnum):
    AUTH = "auth"
    BILLING = "billing"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    NETWORK = "network"
    EMPTY = "empty_response"
    INVALID_RESPONSE = "invalid_response"
    VALIDATION = "validation"
    CONTENT_POLICY = "content_policy"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class RunStage(StrEnum):
    QUEUED = "queued"
    OUTPUT_RECEIVED = "output_received"
    HARD_QA = "hard_qa"
    SEMANTIC_QA = "semantic_qa"
    CANDIDATE_READY = "candidate_ready"


class RemediationKind(StrEnum):
    RETRY = "retry"
    TOOL_REPAIR = "tool_repair"
    REGENERATE = "regenerate"
    IMAGE_EDIT = "image_edit"
    AWAIT_USER = "await_user"
    PROPOSE_CHANGESET = "propose_changeset"


class RemediationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


class AgentSessionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    AWAITING_USER = "awaiting_user"
    AWAITING_INPUT = "awaiting_input"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class ProjectCreate(BaseModel):
    directory_name: str = Field(min_length=1, max_length=160)
    name: str = Field(min_length=1, max_length=200)
    default_language: str = Field(default="zh-CN", min_length=2, max_length=20)

    @field_validator("directory_name")
    @classmethod
    def safe_directory_name(cls, value: str) -> str:
        if value in {".", "..", "local-state"} or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", value
        ):
            raise ValueError("directory_name must be one safe directory segment")
        return value


class ProjectUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be blank")
        return value


class ProjectRead(ORMModel):
    id: str
    name: str
    root_path: str
    default_language: str
    created_at: datetime
    updated_at: datetime


class AssetCreate(BaseModel):
    project_id: str
    key: str
    kind: AssetKind
    subtype: str
    title: str
    schema_ref: str | None = None
    tags: list[str] = []
    metadata: dict[str, Any] = {}

    @field_validator("key")
    @classmethod
    def stable_key(cls, value: str) -> str:
        if not STABLE_KEY_RE.fullmatch(value):
            raise ValueError("key must be lowercase ASCII segments separated by '.', '_' or '-'")
        return value


class AssetRead(ORMModel):
    id: str
    project_id: str
    key: str
    kind: str
    subtype: str
    title: str
    schema_ref: str | None
    tags: list[str]
    asset_metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    content_status: str
    generation_status: str
    publication_status: str
    current_revision_id: str | None
    latest_candidate_revision_id: str | None
    created_at: datetime
    updated_at: datetime


class RevisionCreate(BaseModel):
    asset_id: str
    format: RevisionFormat = RevisionFormat.JSON
    content: Any
    parent_revision_id: str | None = None
    input_hash: str | None = None
    style_revision: str | None = None
    prompt_recipe: str | None = None
    provider_snapshot: dict[str, Any] = {}


class RevisionRead(ORMModel):
    id: str
    asset_id: str
    sequence: int
    format: str
    content: Any = Field(validation_alias="content_json")
    content_hash: str
    parent_revision_id: str | None
    input_hash: str
    style_revision: str | None
    prompt_recipe: str | None
    provider_snapshot: dict[str, Any]
    file_path: str
    review_status: str
    reviewed_at: datetime | None
    created_at: datetime


class RelationCreate(BaseModel):
    project_id: str
    source_asset_id: str
    target_asset_id: str
    relation_type: RelationType
    metadata: dict[str, Any] = {}


class RelationRead(ORMModel):
    id: str
    project_id: str
    source_asset_id: str
    target_asset_id: str
    relation_type: str
    relation_metadata: dict[str, Any] = Field(validation_alias="metadata_json")
    created_at: datetime


class NarrativeRequirementMaterialize(BaseModel):
    """Select scene requirements to promote into ordinary Catalog assets."""

    requirement_ids: list[str] = Field(default_factory=list, max_length=500)


class RenditionRead(ORMModel):
    id: str
    revision_id: str
    media_type: str
    source_path: str
    normalized_path: str | None
    target_path: str | None
    sha256: str
    width: int | None
    height: int | None
    byte_size: int
    created_at: datetime


class ArtifactRead(ORMModel):
    id: str
    project_id: str
    revision_id: str
    role: str
    kind: str
    media_type: str
    path: str
    sha256: str
    byte_size: int
    width: int | None
    height: int | None
    parent_artifact_id: str | None
    tool: dict[str, Any] = Field(validation_alias="tool_json")
    file_path: str
    created_at: datetime


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    base_url: str = "https://api.openai.com/v1"
    # Legacy compatibility fields. New provider profiles do not carry an
    # implicit model route; model selection lives in the explicit catalog and
    # global routing defaults.
    text_model: str = Field(default="", max_length=160)
    image_model: str = Field(default="", max_length=160)
    quality: str = "high"
    concurrency: int = Field(default=6, ge=1, le=32)
    max_retries: int = Field(default=2, ge=0, le=8)
    allow_private_network: bool = False
    pricing: dict[str, Any] | None = None
    credential_mode: ProviderCredentialMode = ProviderCredentialMode.REQUIRED
    model_discovery_mode: ProviderModelDiscoveryMode = ProviderModelDiscoveryMode.AUTO
    models_path: str | None = Field(default="models", max_length=240)


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = None
    text_model: str | None = Field(default=None, min_length=1, max_length=160)
    image_model: str | None = Field(default=None, min_length=1, max_length=160)
    quality: str | None = None
    concurrency: int | None = Field(default=None, ge=1, le=32)
    max_retries: int | None = Field(default=None, ge=0, le=8)
    allow_private_network: bool | None = None
    pricing: dict[str, Any] | None = None
    credential_mode: ProviderCredentialMode | None = None
    model_discovery_mode: ProviderModelDiscoveryMode | None = None
    models_path: str | None = Field(default=None, max_length=240)


class ProviderModelRead(BaseModel):
    id: str
    modalities: list[Literal["text", "image", "video", "audio"]] = Field(default_factory=list)
    classification: Literal["provider", "heuristic", "manual", "unknown"] = "unknown"
    available: bool = True
    enabled: bool = True


class ProviderModelsSyncRead(BaseModel):
    state: Literal["never", "synced", "empty", "manual_required", "error"] = "never"
    checked_at: datetime | None = None
    endpoint: str | None = None
    status_code: int | None = None
    message: str | None = None
    hint: str | None = None
    request_id: str | None = None


class ProviderModelsRead(BaseModel):
    provider_profile_id: str
    models: list[ProviderModelRead] = Field(default_factory=list)
    refreshed_at: datetime | None = None
    new_model_ids: list[str] = Field(default_factory=list)
    cleared_default_routes: list[Literal["text", "image", "video", "audio"]] = Field(
        default_factory=list
    )
    model_catalog_api_version: Literal[2] = 2
    models_sync: ProviderModelsSyncRead = Field(default_factory=ProviderModelsSyncRead)


class ProviderModelOverride(BaseModel):
    id: str = Field(min_length=1, max_length=240)
    modalities: list[Literal["text", "image", "video", "audio"]] = Field(default_factory=list)
    classification: Literal["provider", "heuristic", "manual", "unknown"] | None = None
    available: bool | None = None
    enabled: bool | None = None


class ProviderModelsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[ProviderModelOverride] = Field(default_factory=list)
    removed_model_ids: list[str] = Field(default_factory=list)
    catalog_refreshed: bool = False
    request_id: str | None = Field(default=None, max_length=240)


class ProviderDefaultRoute(BaseModel):
    provider_profile_id: str
    model: str = Field(min_length=1, max_length=160)


class ProviderDefaultsUpdate(BaseModel):
    text: ProviderDefaultRoute | None = None
    image: ProviderDefaultRoute | None = None
    video: ProviderDefaultRoute | None = None
    audio: ProviderDefaultRoute | None = None
    max_concurrency: int = Field(default=3, ge=1, le=32)
    max_transport_retries: int = Field(default=2, ge=0, le=8)


class ProviderDefaultsRead(BaseModel):
    text: ProviderDefaultRoute | None = None
    image: ProviderDefaultRoute | None = None
    video: ProviderDefaultRoute | None = None
    audio: ProviderDefaultRoute | None = None
    max_concurrency: int = 3
    max_transport_retries: int = 2
    updated_at: datetime | None = None


class ProviderRead(ORMModel):
    id: str
    name: str
    kind: str
    base_url: str
    text_model: str
    image_model: str
    quality: str
    concurrency: int
    max_retries: int
    allow_private_network: bool
    pricing: dict[str, Any] | None
    credential_mode: ProviderCredentialMode = ProviderCredentialMode.REQUIRED
    model_discovery_mode: ProviderModelDiscoveryMode = ProviderModelDiscoveryMode.AUTO
    models_path: str | None = None
    is_active: bool = True
    models: list[ProviderModelRead] = Field(
        default_factory=list, validation_alias="models_json"
    )
    models_refreshed_at: datetime | None = None
    models_sync: ProviderModelsSyncRead = Field(default_factory=ProviderModelsSyncRead)
    model_catalog_api_version: Literal[2] = 2
    is_unlocked: bool = False
    created_at: datetime
    updated_at: datetime


class ProviderUnlock(BaseModel):
    api_key: str = Field(min_length=1, max_length=4096)


class GenerationTask(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    kind: TaskKind
    asset_id: str
    prompt: str
    provider_profile_id: str | None = None
    model: str | None = Field(default=None, min_length=1, max_length=160)
    output_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    depends_on: list[str] = []
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    max_bytes: int | None = Field(default=None, ge=1, le=2_000_000_000)
    transparent: bool = False
    reference_path: str | None = None
    reference_task_id: str | None = None
    target_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationPlanCreate(BaseModel):
    project_id: str
    provider_profile_id: str | None = None
    name: str = Field(default="未命名生成计划", min_length=1, max_length=200)
    tasks: list[GenerationTask] = Field(min_length=1)
    extra_call_budget: int | None = Field(default=None, ge=0, le=10_000)
    max_paid_remediation_rounds: int = Field(default=2, ge=0, le=20)
    max_transport_retries: int = Field(default=2, ge=0, le=8)
    max_concurrency: int = Field(default=3, ge=1, le=32)


class GenerationPlanRead(ORMModel):
    id: str
    project_id: str
    provider_profile_id: str
    name: str
    status: str
    tasks: list[dict[str, Any]] = Field(validation_alias="tasks_json")
    estimated_calls: int
    estimated_cost: float | None
    suggested_extra_calls: int
    extra_call_budget: int
    extra_calls_used: int
    actual_calls: int
    actual_cost: float
    max_paid_remediation_rounds: int
    max_transport_retries: int
    max_concurrency: int
    confirmed_at: datetime | None
    created_at: datetime


class GenerationJobRead(ORMModel):
    blocking_reason: str | None = None
    recovery_eligible: bool = False
    delivery_state: str | None = None
    id: str
    plan_id: str
    project_id: str
    provider_profile_id: str
    task_id: str
    task_kind: str
    request: dict[str, Any] = Field(default_factory=dict, validation_alias="request_json")
    status: str
    stage: str
    progress: float
    result_revision_id: str | None
    error_category: str | None
    error_message: str | None
    cancel_requested: bool
    attempt_count: int
    paid_remediation_rounds: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    resolved_request: dict[str, Any] = Field(
        default_factory=dict, validation_alias="resolved_request_json"
    )
    provider_snapshot: dict[str, Any] = Field(
        default_factory=dict, validation_alias="provider_snapshot_json"
    )
    pending_action_id: str | None
    created_at: datetime
    updated_at: datetime


class GenerationAttemptRead(ORMModel):
    dispatch_state: str = "legacy_unknown"
    dispatched_at: datetime | None = None
    dispatch_count: int = 0
    error_code: str | None = None
    error_hint: str | None = None
    network_policy: str | None = None
    id: str
    job_id: str
    number: int
    status: str
    phase: str
    purpose: str
    idempotency_key: str | None
    request_hash: str | None
    request_id: str | None
    error_category: str | None
    error_message: str | None
    output_path: str | None
    output_hash: str | None
    result_revision_id: str | None
    billable: bool
    estimated_cost: float | None
    started_at: datetime
    completed_at: datetime | None


class PlanBudgetUpdate(BaseModel):
    extra_call_budget: int = Field(ge=0, le=10_000)


class RunEventRead(ORMModel):
    id: str
    sequence: int
    project_id: str
    plan_id: str
    job_id: str | None
    asset_id: str | None
    attempt_id: str | None
    event_type: str
    stage: str | None
    data: dict[str, Any] = Field(default_factory=dict, validation_alias="data_json")
    causation_id: str | None
    created_at: datetime


class FindingRead(ORMModel):
    id: str
    project_id: str
    plan_id: str
    job_id: str
    revision_id: str | None
    code: str
    severity: str
    blocking: bool
    evidence: list[dict[str, Any]] = Field(
        default_factory=list, validation_alias="evidence_json"
    )
    confidence: float
    suggested_action: str
    occurrence: int
    resolved_at: datetime | None
    created_at: datetime


class RunEvidenceRead(ORMModel):
    id: str
    project_id: str
    plan_id: str
    job_id: str | None
    revision_id: str | None
    kind: str
    label: str
    path: str | None
    media_type: str | None
    sha256: str | None
    byte_size: int | None
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="metadata_json")
    created_at: datetime


class RemediationCreate(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=160)
    action: RemediationKind
    strategy: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    finding_ids: list[str] = Field(default_factory=list)
    expected_additional_calls: int = Field(default=0, ge=0, le=10)


class AgentDiagnoseRequest(BaseModel):
    """Optional limits for a single Codex diagnosis turn."""

    finding_ids: list[str] = Field(default_factory=list)
    budget: int = Field(default=1, ge=1, le=20)
    reason: str | None = Field(default=None, max_length=2_000)


class AgentSessionCreate(AgentDiagnoseRequest):
    job_id: str


class GenerationConversationCreate(BaseModel):
    """Create a user-facing, planning-focused Agent session."""

    project_id: str
    seed_asset_ids: list[str] = Field(default_factory=list, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    agent_model: str | None = Field(default=None, max_length=160)


class GenerationConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    context_asset_ids: list[str] = Field(default_factory=list, max_length=100)
    client_message_id: str | None = Field(default=None, max_length=160)


class GenerationConversationSteerCreate(BaseModel):
    client_message_id: str | None = Field(default=None, max_length=160)
    content: str = Field(min_length=1, max_length=20_000)


class GenerationInputOption(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)


class GenerationInputQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=100, pattern=STABLE_KEY_RE.pattern)
    header: str = Field(min_length=1, max_length=80)
    question: str = Field(min_length=1, max_length=1000)
    options: list[GenerationInputOption] | None = Field(default=None, max_length=3)
    isOther: bool = False
    isSecret: bool = False


class GenerationInputRequestRead(BaseModel):
    id: str
    turn_id: str
    response_mode: Literal["resume_turn", "new_turn"]
    status: Literal["pending", "resolved", "cancelled"]
    questions: list[GenerationInputQuestion]
    answers: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    auto_resolution_ms: int | None = None
    fallback_reason: str | None = None


class GenerationInputAnswerCreate(BaseModel):
    client_response_id: str = Field(min_length=1, max_length=160)
    answers: dict[str, dict[str, list[str]]] = Field(min_length=1, max_length=3)


class GenerationInputAnswerRead(BaseModel):
    request_id: str
    turn_id: str
    status: str
    next_sequence: int


class GenerationConversationDraftUpdate(BaseModel):
    base_hash: str = Field(min_length=8, max_length=128)
    draft: dict[str, Any]


class GenerationConversationConfirm(BaseModel):
    draft_hash: str = Field(min_length=8, max_length=128)
    context_hash: str | None = Field(default=None, min_length=8, max_length=128)
    accepted_warning_codes: list[str] = Field(default_factory=list, max_length=100)


class GenerationConversationRead(ORMModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True, populate_by_name=True)

    id: str
    project_id: str
    plan_id: str | None
    thread_id: str | None
    purpose: str
    title: str | None
    agent_model: str | None
    status: str
    context_hash: str
    seed_asset_ids: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict, validation_alias="context_json")
    draft: dict[str, Any] = Field(default_factory=dict, validation_alias="draft_json")
    draft_hash: str | None
    draft_version: int
    budget_limit: int
    budget_used: int
    turn_count: int
    diagnostic_reason: str | None
    stop_reason: str | None
    usage: dict[str, Any] = Field(default_factory=dict)
    last_error: dict[str, Any] | None = None
    pending_input: GenerationInputRequestRead | None = None
    current_turn_id: str | None = None
    recovery_status: str | None = None
    last_sequence: int = 0
    batches: list[dict[str, Any]] = Field(default_factory=list)
    pending_proposal: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    archived_at: datetime | None


class GenerationConversationSummary(BaseModel):
    id: str
    project_id: str
    plan_id: str | None = None
    title: str | None = None
    agent_model: str | None = None
    status: str
    turn_count: int = 0
    budget_limit: int = 0
    budget_used: int = 0
    seed_asset_ids: list[str] = Field(default_factory=list)
    error_summary: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    archived_at: datetime | None = None


class GenerationConversationMessageRead(BaseModel):
    turn_id: str
    status: str
    next_sequence: int = 0


class GenerationConversationSettingsUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    agent_model: str | None = Field(default=None, max_length=160)


class GenerationConversationContextUpdate(BaseModel):
    seed_asset_ids: list[str] = Field(default_factory=list, max_length=100)


class GenerationAgentCapabilitiesRead(BaseModel):
    available: bool
    interactive_user_input: bool = False
    can_connect: bool = False
    can_start: bool = False
    can_resume: bool | None = None
    adapter: str
    version: str
    models: list[dict[str, Any]] = Field(default_factory=list)
    diagnostic: dict[str, Any] = Field(default_factory=dict)


class GenerationConversationEventRead(ORMModel):
    sequence: int
    id: str
    session_id: str
    event_type: str
    thread_id: str | None
    turn_id: str | None
    data: dict[str, Any] = Field(default_factory=dict, validation_alias="data_json")
    created_at: datetime


class GenerationConversationConfirmRead(BaseModel):
    conversation: GenerationConversationRead
    plan: GenerationPlanRead
    jobs: list[GenerationJobRead]
    created_assets: list[AssetRead] = Field(default_factory=list)


class GenerationAssetProposal(BaseModel):
    mode: Literal["existing", "new"] = "existing"
    asset_id: str | None = None
    key: str | None = Field(default=None, max_length=240)
    kind: AssetKind | None = None
    subtype: str | None = Field(default=None, max_length=80)
    title: str | None = Field(default=None, max_length=240)
    schema_ref: str | None = Field(default=None, max_length=240)
    tags: list[str] = Field(default_factory=list, max_length=50)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationReferenceProposal(BaseModel):
    asset_id: str
    role: Literal["primary", "supporting"] = "supporting"
    reason: str = Field(default="", max_length=1_000)
    revision_id: str | None = None
    rendition_id: str | None = None
    sha256: str | None = None


class GenerationTaskProposal(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(min_length=1, max_length=160)
    asset: GenerationAssetProposal
    kind: TaskKind
    prompt: str = Field(min_length=1, max_length=20_000)
    provider_profile_id: str | None = None
    model: str | None = Field(default=None, max_length=160)
    output_schema: dict[str, Any] | None = Field(
        default=None, validation_alias="schema", serialization_alias="schema"
    )
    depends_on: list[str] = Field(default_factory=list, max_length=100)
    width: int | None = Field(default=None, ge=1, le=8192)
    height: int | None = Field(default=None, ge=1, le=8192)
    max_bytes: int | None = Field(default=None, ge=1, le=2_000_000_000)
    transparent: bool = False
    reference_task_id: str | None = None
    references: list[GenerationReferenceProposal] = Field(default_factory=list, max_length=20)
    target_path: str | None = Field(default=None, max_length=1_000)
    candidate_path: str | None = Field(default=None, max_length=1_000)
    locked_fields: list[str] = Field(default_factory=list, max_length=100)
    metadata: dict[str, Any] = Field(default_factory=dict)


class GenerationPlanningWarning(BaseModel):
    """A user-visible warning that must be explicitly accepted at confirmation."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=160, pattern=STABLE_KEY_RE.pattern)
    message: str = Field(min_length=1, max_length=2_000)


class GenerationPlanningDraft(BaseModel):
    version: int = Field(default=1, ge=1, le=10)
    title: str = Field(default="新资产生成任务", min_length=1, max_length=200)
    summary: str = Field(default="", max_length=4_000)
    tasks: list[GenerationTaskProposal] = Field(default_factory=list, max_length=200)
    questions: list[str] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    warnings: list[GenerationPlanningWarning] = Field(default_factory=list, max_length=100)
    settings: dict[str, Any] = Field(default_factory=dict)
    context_hash: str | None = None

    @field_validator("warnings", mode="before")
    @classmethod
    def normalize_legacy_warnings(cls, value: Any) -> Any:
        """Keep older/string model output usable without making warnings optional."""

        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if not isinstance(item, str):
                normalized.append(item)
                continue
            message = " ".join(item.split())
            if not message:
                normalized.append(item)
                continue
            digest = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
            normalized.append({"code": f"agent_warning_{digest}", "message": message})
        return normalized


# Public planning vocabulary.  Keep the longer internal names for backwards
# compatibility with the first API implementation, while exposing the concise
# names used by the conversation contract and by client integrations.
AssetProposal = GenerationAssetProposal
ReferenceProposal = GenerationReferenceProposal


class AgentProposal(BaseModel):
    """Versioned adapter output. Unknown fields remain auditable but actions do not."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = Field(default=1, ge=1, le=10)
    finding_ids: list[str] = Field(default_factory=list)
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_additional_calls: int = Field(default=0, ge=0, le=10)
    reason: str = Field(min_length=1, max_length=4_000)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    input_hash: str | None = Field(default=None, min_length=8, max_length=128)
    thread_id: str | None = None
    turn_id: str | None = None


class AgentSessionRead(ORMModel):
    id: str
    project_id: str
    plan_id: str | None
    job_id: str | None
    asset_id: str | None
    thread_id: str | None
    adapter: str
    adapter_version: str | None
    schema_version: int
    status: str
    purpose: str = "diagnosis"
    title: str | None = None
    context_hash: str
    context_path: str | None
    context: dict[str, Any] = Field(default_factory=dict, validation_alias="context_json")
    draft: dict[str, Any] = Field(default_factory=dict, validation_alias="draft_json")
    draft_hash: str | None = None
    draft_version: int = 0
    sandbox: dict[str, Any] = Field(default_factory=dict, validation_alias="sandbox_json")
    allowed_actions: list[str] = Field(default_factory=list, validation_alias="allowed_actions_json")
    writable_allowlist: list[str] = Field(default_factory=list, validation_alias="writable_allowlist_json")
    budget_limit: int
    budget_used: int
    turn_count: int = 0
    diagnostic_reason: str | None
    result: dict[str, Any] = Field(default_factory=dict, validation_alias="result_json")
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class AgentEventRead(ORMModel):
    sequence: int
    id: str
    session_id: str
    project_id: str
    plan_id: str | None
    job_id: str | None
    asset_id: str | None
    event_type: str
    thread_id: str | None
    turn_id: str | None
    data: dict[str, Any] = Field(default_factory=dict, validation_alias="data_json")
    created_at: datetime


class ChangeSetRead(ORMModel):
    id: str
    session_id: str
    project_id: str
    target_repository: str
    baseline_commit: str | None
    patch_path: str
    patch_hash: str
    files: list[str] = Field(default_factory=list, validation_alias="files_json")
    validation_commands: list[list[str]] = Field(
        default_factory=list, validation_alias="validation_commands_json"
    )
    risk: str
    summary: str
    status: str
    decision_reason: str | None
    created_at: datetime
    decided_at: datetime | None


class RemediationRead(ORMModel):
    id: str
    project_id: str
    plan_id: str
    job_id: str
    action: str
    strategy: str
    status: str
    reason: str
    parameters: dict[str, Any] = Field(default_factory=dict, validation_alias="parameters_json")
    finding_ids: list[str] = Field(default_factory=list, validation_alias="finding_ids_json")
    input_hash: str
    expected_additional_calls: int
    actual_additional_calls: int
    created_at: datetime
    completed_at: datetime | None


class RunInspectRead(BaseModel):
    plan: GenerationPlanRead
    jobs: list[GenerationJobRead]
    attempts: list[GenerationAttemptRead]
    findings: list[FindingRead]
    evidence: list[RunEvidenceRead]
    actions: list[RemediationRead]
    events: list[RunEventRead]
    agent_sessions: list[AgentSessionRead] = Field(default_factory=list)
    agent_events: list[AgentEventRead] = Field(default_factory=list)
    changesets: list[ChangeSetRead] = Field(default_factory=list)


class QARunCreate(BaseModel):
    rendition_id: str
    expected_width: int | None = None
    expected_height: int | None = None
    require_alpha: bool = False
    max_bytes: int | None = None


class QARunRead(ORMModel):
    id: str
    rendition_id: str
    verdict: str
    checks: list[dict[str, Any]] = Field(validation_alias="checks_json")
    report_path: str | None
    created_at: datetime


class ReviewCreate(BaseModel):
    revision_id: str
    verdict: ReviewVerdict
    notes: str | None = None


class ReviewArtifactRead(ORMModel):
    artifact_id: str
    role: str
    sha256: str


class ReviewRead(ORMModel):
    id: str
    revision_id: str
    asset_id: str
    verdict: str
    notes: str | None
    dependency_hash: str
    is_valid: bool
    artifacts: list[ReviewArtifactRead] = Field(
        default_factory=list, validation_alias="artifact_bindings"
    )
    created_at: datetime


class ReleaseCreate(BaseModel):
    project_id: str
    name: str
    format_version: Literal[1, 2] = Field(
        default=1,
        description="Release manifest version; v1 remains available for legacy clients, v2 enables Delivery.",
    )
    asset_keys: list[str] | None = Field(
        default=None,
        description=(
            "Optional explicit stable-key subset. When omitted, every approved asset is released; "
            "an empty list is rejected so a Release can never be silently empty."
        ),
    )
    publish_media: bool = Field(
        default=True,
        deprecated=True,
        description="Compatibility field; Release v1 records durable objects and never performs Delivery.",
    )


class LegacyMediaMigrationCreate(BaseModel):
    asset_keys: list[str] = Field(min_length=1, description="Approved legacy media keys to promote")


class LegacyMediaMigrationResult(BaseModel):
    project_id: str
    migrated: list[dict[str, str]] = Field(default_factory=list)
    skipped: list[dict[str, str]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReleaseRead(ORMModel):
    id: str
    project_id: str
    name: str
    manifest_version: int = 1
    manifest_path: str
    manifest_hash: str
    snapshot_hash: str | None = None
    asset_count: int
    created_at: datetime


class ReleasePreflightRead(BaseModel):
    project_id: str
    release_id: str | None = None
    manifest_version: int | None = None
    manifest_hash: str | None = None
    snapshot_hash: str | None = None
    issues: list[str] = Field(default_factory=list)
    blocking: bool = False
    assets: list[dict[str, Any]] = Field(default_factory=list)
    export_config: dict[str, Any] = Field(default_factory=dict)


class ExportRequest(BaseModel):
    project_id: str
    release_id: str
    game_root: str | None = None
    run_commands: bool = True


class ExportVerifyRequest(BaseModel):
    project_id: str
    release_id: str | None = None
    game_root: str | None = None
    run_commands: bool = False


class ExportRollbackRequest(ExportRequest):
    pass


class DeliveryRead(ORMModel):
    id: str
    project_id: str
    release_id: str
    release_manifest_hash: str
    snapshot_hash: str
    checkout_fingerprint: str
    display_path: str
    status: str
    previous_release_id: str | None
    files: list[dict[str, Any]] = Field(default_factory=list, validation_alias="files_json")
    validation_results: list[dict[str, Any]] = Field(
        default_factory=list, validation_alias="validation_results_json"
    )
    rollback: dict[str, Any] | None = Field(default=None, validation_alias="rollback_json")
    created_at: datetime


class ExportConfigRead(BaseModel):
    project_id: str
    project: dict[str, Any] = Field(default_factory=dict)
    local: dict[str, Any] = Field(default_factory=dict)


class ExportConfigUpdate(BaseModel):
    game_root: str | None = None


class ScanReport(BaseModel):
    project_id: str
    assets_indexed: int
    revisions_indexed: int
    errors: list[str]


class SystemInfo(BaseModel):
    projects_root: str
    state_dir: str
    project_workspace: str = "<project>/workspace"
    credential_store: str = "browser IndexedDB"


class Message(BaseModel):
    detail: str


class ProviderCapabilities(BaseModel):
    structured_text: bool
    image_generation: bool
    image_edit: bool
    models: list[str] = []


class JobEvent(BaseModel):
    id: str
    event: Literal["job"] = "job"
    data: GenerationJobRead
