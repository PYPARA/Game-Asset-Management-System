from __future__ import annotations

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


class RemediationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    RUNNING = "running"
    COMPLETED = "completed"
    REJECTED = "rejected"
    FAILED = "failed"


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
    name: str
    kind: ProviderKind = ProviderKind.OPENAI_COMPATIBLE
    base_url: str = "https://api.openai.com/v1"
    text_model: str = "gpt-5.1"
    image_model: str = "gpt-image-2"
    quality: str = "high"
    concurrency: int = Field(default=6, ge=1, le=32)
    max_retries: int = Field(default=2, ge=0, le=8)
    allow_private_network: bool = False
    pricing: dict[str, Any] | None = None


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
    provider_profile_id: str
    name: str = Field(default="未命名生成计划", min_length=1, max_length=200)
    tasks: list[GenerationTask] = Field(min_length=1)
    extra_call_budget: int | None = Field(default=None, ge=0, le=10_000)
    max_paid_remediation_rounds: int = Field(default=2, ge=0, le=20)
    max_transport_retries: int = Field(default=2, ge=0, le=8)
    max_concurrency: int = Field(default=6, ge=1, le=32)


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
    pending_action_id: str | None
    created_at: datetime
    updated_at: datetime


class GenerationAttemptRead(ORMModel):
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
    action: RemediationKind
    strategy: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=1, max_length=2_000)
    parameters: dict[str, Any] = Field(default_factory=dict)
    finding_ids: list[str] = Field(default_factory=list)
    expected_additional_calls: int = Field(default=0, ge=0, le=10)


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
    publish_media: bool = Field(
        default=True,
        deprecated=True,
        description="Compatibility field; Release v1 records durable objects and never performs Delivery.",
    )


class ReleaseRead(ORMModel):
    id: str
    project_id: str
    name: str
    manifest_path: str
    manifest_hash: str
    asset_count: int
    created_at: datetime


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
