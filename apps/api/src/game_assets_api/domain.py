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
    transparent: bool = False
    reference_path: str | None = None
    target_path: str | None = None


class GenerationPlanCreate(BaseModel):
    project_id: str
    provider_profile_id: str
    tasks: list[GenerationTask] = Field(min_length=1)


class GenerationPlanRead(ORMModel):
    id: str
    project_id: str
    provider_profile_id: str
    status: str
    tasks: list[dict[str, Any]] = Field(validation_alias="tasks_json")
    estimated_calls: int
    estimated_cost: float | None
    confirmed_at: datetime | None
    created_at: datetime


class GenerationJobRead(ORMModel):
    id: str
    plan_id: str
    project_id: str
    provider_profile_id: str
    task_id: str
    task_kind: str
    status: str
    progress: float
    result_revision_id: str | None
    error_category: str | None
    error_message: str | None
    cancel_requested: bool
    attempt_count: int
    created_at: datetime
    updated_at: datetime


class GenerationAttemptRead(ORMModel):
    id: str
    job_id: str
    number: int
    status: str
    request_id: str | None
    error_category: str | None
    error_message: str | None
    started_at: datetime
    completed_at: datetime | None


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


class ReviewRead(ORMModel):
    id: str
    revision_id: str
    asset_id: str
    verdict: str
    notes: str | None
    dependency_hash: str
    is_valid: bool
    created_at: datetime


class ReleaseCreate(BaseModel):
    project_id: str
    name: str
    publish_media: bool = True


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
