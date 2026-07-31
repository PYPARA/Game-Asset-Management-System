from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .domain import (
    ContentStatus,
    GenerationStatus,
    PublicationStatus,
)


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    default_language: Mapped[str] = mapped_column(String(20), default="zh-CN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    assets: Mapped[list[Asset]] = relationship(back_populates="project", cascade="all, delete-orphan")


class Asset(Base):
    __tablename__ = "assets"
    __table_args__ = (UniqueConstraint("project_id", "key", name="uq_asset_project_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(240), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    subtype: Mapped[str] = mapped_column(String(80), index=True)
    title: Mapped[str] = mapped_column(String(240))
    schema_ref: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_status: Mapped[str] = mapped_column(String(40), default=ContentStatus.DRAFT.value)
    generation_status: Mapped[str] = mapped_column(String(40), default=GenerationStatus.IDLE.value)
    publication_status: Mapped[str] = mapped_column(String(40), default=PublicationStatus.UNPUBLISHED.value)
    current_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    latest_candidate_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="assets")
    revisions: Mapped[list[AssetRevision]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", foreign_keys="AssetRevision.asset_id"
    )


class AssetRevision(Base):
    __tablename__ = "asset_revisions"
    __table_args__ = (UniqueConstraint("asset_id", "sequence", name="uq_revision_asset_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    format: Mapped[str] = mapped_column(String(20))
    content_json: Mapped[Any] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    parent_revision_id: Mapped[str | None] = mapped_column(ForeignKey("asset_revisions.id"), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    style_revision: Mapped[str | None] = mapped_column(String(240))
    prompt_recipe: Mapped[str | None] = mapped_column(String(240))
    provider_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    file_path: Mapped[str] = mapped_column(Text)
    review_status: Mapped[str] = mapped_column(String(40), default="pending", index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    asset: Mapped[Asset] = relationship(back_populates="revisions", foreign_keys=[asset_id])


class AssetRelation(Base):
    __tablename__ = "asset_relations"
    __table_args__ = (
        UniqueConstraint(
            "project_id", "source_asset_id", "target_asset_id", "relation_type", name="uq_relation"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    source_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    target_asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    relation_type: Mapped[str] = mapped_column(String(40))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Rendition(Base):
    __tablename__ = "renditions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id", ondelete="CASCADE"), index=True)
    media_type: Mapped[str] = mapped_column(String(100))
    source_path: Mapped[str] = mapped_column(Text)
    normalized_path: Mapped[str | None] = mapped_column(Text)
    target_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(40), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    media_type: Mapped[str] = mapped_column(String(100))
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    parent_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id"), nullable=True
    )
    tool_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    file_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    kind: Mapped[str] = mapped_column(String(40))
    base_url: Mapped[str] = mapped_column(Text)
    text_model: Mapped[str] = mapped_column(String(160))
    image_model: Mapped[str] = mapped_column(String(160))
    quality: Mapped[str] = mapped_column(String(40), default="high")
    concurrency: Mapped[int] = mapped_column(Integer, default=6)
    max_retries: Mapped[int] = mapped_column(Integer, default=2)
    allow_private_network: Mapped[bool] = mapped_column(Boolean, default=False)
    pricing: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    models_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    models_refreshed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProviderRoutingDefaults(Base):
    __tablename__ = "provider_routing_defaults"

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="global")
    text_provider_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_profiles.id", ondelete="SET NULL"), nullable=True
    )
    text_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    image_provider_profile_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_profiles.id", ondelete="SET NULL"), nullable=True
    )
    image_model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class GenerationPlan(Base):
    __tablename__ = "generation_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider_profile_id: Mapped[str] = mapped_column(ForeignKey("provider_profiles.id"), index=True)
    name: Mapped[str] = mapped_column(String(200), default="未命名生成计划")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    tasks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    estimated_calls: Mapped[int] = mapped_column(Integer)
    estimated_cost: Mapped[float | None] = mapped_column(Float)
    suggested_extra_calls: Mapped[int] = mapped_column(Integer, default=2)
    extra_call_budget: Mapped[int] = mapped_column(Integer, default=2)
    extra_calls_used: Mapped[int] = mapped_column(Integer, default=0)
    actual_calls: Mapped[int] = mapped_column(Integer, default=0)
    actual_cost: Mapped[float] = mapped_column(Float, default=0.0)
    max_paid_remediation_rounds: Mapped[int] = mapped_column(Integer, default=2)
    max_transport_retries: Mapped[int] = mapped_column(Integer, default=2)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=6)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationJob(Base):
    __tablename__ = "generation_jobs"
    __table_args__ = (UniqueConstraint("plan_id", "task_id", name="uq_job_plan_task"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    provider_profile_id: Mapped[str] = mapped_column(ForeignKey("provider_profiles.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(160))
    task_kind: Mapped[str] = mapped_column(String(40))
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(40), default=GenerationStatus.QUEUED.value, index=True)
    stage: Mapped[str] = mapped_column(String(40), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    result_revision_id: Mapped[str | None] = mapped_column(ForeignKey("asset_revisions.id"))
    error_category: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    paid_remediation_rounds: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    provider_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    pending_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True)


class GenerationAttempt(Base):
    __tablename__ = "generation_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40))
    phase: Mapped[str] = mapped_column(String(40), default="created", index=True)
    purpose: Mapped[str] = mapped_column(String(40), default="base")
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(240))
    error_category: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_revision_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    billable: Mapped[bool] = mapped_column(Boolean, default=True)
    estimated_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RunEvent(Base):
    __tablename__ = "run_events"

    sequence: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(48), unique=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_attempts.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(80), index=True)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    data_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    causation_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProductionFinding(Base):
    __tablename__ = "production_findings"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), nullable=True
    )
    code: Mapped[str] = mapped_column(String(160), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    blocking: Mapped[bool] = mapped_column(Boolean, default=True)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    suggested_action: Mapped[str] = mapped_column(String(40))
    occurrence: Mapped[int] = mapped_column(Integer, default=1)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RunEvidence(Base):
    __tablename__ = "run_evidence"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("asset_revisions.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(80), index=True)
    label: Mapped[str] = mapped_column(String(200))
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RemediationAction(Base):
    __tablename__ = "remediation_actions"

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("generation_plans.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("generation_jobs.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(40))
    strategy: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(Text)
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    finding_ids_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    input_hash: Mapped[str] = mapped_column(String(64))
    expected_additional_calls: Mapped[int] = mapped_column(Integer, default=0)
    actual_additional_calls: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QARun(Base):
    __tablename__ = "qa_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rendition_id: Mapped[str] = mapped_column(ForeignKey("renditions.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(40))
    checks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    report_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewDecision(Base):
    __tablename__ = "review_decisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    verdict: Mapped[str] = mapped_column(String(40))
    notes: Mapped[str | None] = mapped_column(Text)
    dependency_hash: Mapped[str] = mapped_column(String(64))
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    artifact_bindings: Mapped[list[ReviewArtifact]] = relationship(
        back_populates="review", cascade="all, delete-orphan"
    )


class ReviewArtifact(Base):
    __tablename__ = "review_artifacts"
    __table_args__ = (
        UniqueConstraint("review_id", "artifact_id", "role", name="uq_review_artifact_role"),
    )

    id: Mapped[str] = mapped_column(String(48), primary_key=True, default=new_id)
    review_id: Mapped[str] = mapped_column(
        ForeignKey("review_decisions.id", ondelete="CASCADE"), index=True
    )
    artifact_id: Mapped[str] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(40))
    sha256: Mapped[str] = mapped_column(String(64))

    review: Mapped[ReviewDecision] = relationship(back_populates="artifact_bindings")


class Release(Base):
    __tablename__ = "releases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    manifest_version: Mapped[int] = mapped_column(Integer, default=1)
    manifest_path: Mapped[str] = mapped_column(Text)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str | None] = mapped_column(String(80), nullable=True)
    asset_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Delivery(Base):
    """Rebuildable lookup index for immutable Project delivery receipts."""

    __tablename__ = "deliveries"

    id: Mapped[str] = mapped_column(String(48), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    release_id: Mapped[str] = mapped_column(ForeignKey("releases.id", ondelete="CASCADE"), index=True)
    release_manifest_hash: Mapped[str] = mapped_column(String(80))
    snapshot_hash: Mapped[str] = mapped_column(String(80))
    checkout_fingerprint: Mapped[str] = mapped_column(String(80))
    display_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), index=True)
    previous_release_id: Mapped[str | None] = mapped_column(String(48), nullable=True)
    files_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validation_results_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rollback_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
