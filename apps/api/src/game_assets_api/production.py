from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .domain import (
    GenerationStatus,
    QAVerdict,
    RemediationCreate,
    RemediationKind,
    RemediationStatus,
    RunStage,
)
from .models import (
    AssetRevision,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    Project,
    ProductionFinding,
    RemediationAction,
    RunEvent,
    RunEvidence,
    new_id,
    utcnow,
)
from .storage import canonical_json, safe_join, sha256_bytes, sha256_file, stable_id


SUCCESS_STATUSES = {
    GenerationStatus.CANDIDATE_READY.value,
    GenerationStatus.SUCCEEDED.value,
}
ACTIVE_STATUSES = {
    GenerationStatus.QUEUED.value,
    GenerationStatus.RUNNING.value,
    GenerationStatus.OUTPUT_RECEIVED.value,
    GenerationStatus.HARD_QA.value,
    GenerationStatus.SEMANTIC_QA.value,
    GenerationStatus.REMEDIATING.value,
}
RECOVERABLE_OUTPUT_PHASES = {"output_received", "staged"}


def attempt_output_is_valid(
    session: Session,
    job: GenerationJob,
    attempt: GenerationAttempt | None,
) -> bool:
    if (
        attempt is None
        or attempt.phase not in RECOVERABLE_OUTPUT_PHASES
        or not attempt.output_path
        or not attempt.output_hash
    ):
        return False
    project = session.get(Project, job.project_id)
    if project is None:
        return False
    try:
        path = safe_join(Path(project.root_path), attempt.output_path)
        return path.is_file() and sha256_file(path) == attempt.output_hash
    except (OSError, ValueError):
        return False


def record_run_event(
    session: Session,
    *,
    plan_id: str,
    project_id: str,
    event_type: str,
    job_id: str | None = None,
    asset_id: str | None = None,
    attempt_id: str | None = None,
    stage: str | None = None,
    data: dict[str, Any] | None = None,
    causation_id: str | None = None,
) -> RunEvent:
    event = RunEvent(
        id=new_id(),
        project_id=project_id,
        plan_id=plan_id,
        job_id=job_id,
        asset_id=asset_id,
        attempt_id=attempt_id,
        event_type=event_type,
        stage=stage,
        data_json=data or {},
        causation_id=causation_id,
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def refresh_plan_status(session: Session, plan: GenerationPlan) -> str:
    jobs = list(
        session.scalars(
            select(GenerationJob).where(GenerationJob.plan_id == plan.id)
        ).all()
    )
    statuses = {job.status for job in jobs}
    if not jobs:
        return plan.status
    if statuses <= SUCCESS_STATUSES:
        plan.status = GenerationStatus.CANDIDATE_READY.value
    elif statuses & {
        GenerationStatus.AWAITING_USER.value,
        GenerationStatus.QA_FAILED.value,
    }:
        plan.status = GenerationStatus.AWAITING_USER.value
    elif statuses & ACTIVE_STATUSES:
        plan.status = GenerationStatus.RUNNING.value
    elif GenerationStatus.CREDENTIALS_LOCKED.value in statuses:
        plan.status = GenerationStatus.CREDENTIALS_LOCKED.value
    elif GenerationStatus.FAILED.value in statuses:
        plan.status = GenerationStatus.FAILED.value
    elif GenerationStatus.CANCELLED.value in statuses:
        plan.status = GenerationStatus.CANCELLED.value
    return plan.status


def inspect_run(session: Session, plan: GenerationPlan, *, event_limit: int = 500) -> dict[str, Any]:
    jobs = list(
        session.scalars(
            select(GenerationJob)
            .where(GenerationJob.plan_id == plan.id)
            .order_by(GenerationJob.created_at, GenerationJob.task_id)
        ).all()
    )
    job_ids = [job.id for job in jobs]
    attempts = (
        list(
            session.scalars(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id.in_(job_ids))
                .order_by(GenerationAttempt.started_at, GenerationAttempt.number)
            ).all()
        )
        if job_ids
        else []
    )
    findings = list(
        session.scalars(
            select(ProductionFinding)
            .where(ProductionFinding.plan_id == plan.id)
            .order_by(ProductionFinding.created_at, ProductionFinding.id)
        ).all()
    )
    evidence = list(
        session.scalars(
            select(RunEvidence)
            .where(RunEvidence.plan_id == plan.id)
            .order_by(RunEvidence.created_at, RunEvidence.id)
        ).all()
    )
    actions = list(
        session.scalars(
            select(RemediationAction)
            .where(RemediationAction.plan_id == plan.id)
            .order_by(RemediationAction.created_at, RemediationAction.id)
        ).all()
    )
    events = list(
        session.scalars(
            select(RunEvent)
            .where(RunEvent.plan_id == plan.id)
            .order_by(RunEvent.sequence.desc())
            .limit(event_limit)
        ).all()
    )
    events.reverse()
    return {
        "plan": plan,
        "jobs": jobs,
        "attempts": attempts,
        "findings": findings,
        "evidence": evidence,
        "actions": actions,
        "events": events,
    }


def finding_code(check_name: str) -> str:
    return {
        "decodable": "media.decode_failed",
        "width": "media.width_mismatch",
        "height": "media.height_mismatch",
        "alpha": "alpha.missing",
        "fileSize": "media.file_too_large",
        "transparentCorners": "alpha.opaque_corners",
        "subjectCoverage": "composition.subject_coverage",
        "residualColorKey": "alpha.background_residue",
    }.get(check_name, f"qa.{check_name}")


def findings_from_qa(
    session: Session,
    *,
    job: GenerationJob,
    revision_id: str,
    verdict: str,
    checks: Iterable[dict[str, Any]],
    evidence_id: str | None = None,
) -> list[ProductionFinding]:
    created: list[ProductionFinding] = []
    check_list = list(checks)
    unresolved = list(
        session.scalars(
            select(ProductionFinding).where(
                ProductionFinding.job_id == job.id,
                ProductionFinding.blocking.is_(True),
                ProductionFinding.resolved_at.is_(None),
            )
        ).all()
    )
    if verdict in {QAVerdict.PASS.value, QAVerdict.WARNING.value}:
        for finding in unresolved:
            finding.resolved_at = utcnow()
        return created

    checked_codes = {
        finding_code(str(check.get("name") or "unknown"))
        for check in check_list
    }
    # Each QA run supersedes the previous unresolved occurrence for every
    # check it actually evaluated. Historical rows remain available for audit.
    for finding in unresolved:
        if finding.code in checked_codes:
            finding.resolved_at = utcnow()

    for check in check_list:
        if bool(check.get("passed", False)):
            continue
        name = str(check.get("name") or "unknown")
        code = finding_code(name)
        previous_count = session.scalar(
            select(func.count(ProductionFinding.id)).where(
                ProductionFinding.job_id == job.id,
                ProductionFinding.code == code,
            )
        ) or 0
        occurrence = int(previous_count) + 1
        suggested = (
            RemediationKind.TOOL_REPAIR.value
            if name in {"width", "height", "alpha", "fileSize", "residualColorKey"}
            else RemediationKind.AWAIT_USER.value
        )
        finding = ProductionFinding(
            id=stable_id("finding", job.id, revision_id, code, str(occurrence)),
            project_id=job.project_id,
            plan_id=job.plan_id,
            job_id=job.id,
            revision_id=revision_id,
            code=code,
            severity="error",
            blocking=True,
            evidence_json=(
                [{"evidence_id": evidence_id, "check": check}] if evidence_id else [{"check": check}]
            ),
            confidence=1.0,
            suggested_action=suggested,
            occurrence=occurrence,
        )
        session.add(finding)
        created.append(finding)
    session.flush()
    return created


def current_job_input_hash(session: Session, job: GenerationJob) -> str:
    revision = session.get(AssetRevision, job.result_revision_id) if job.result_revision_id else None
    return sha256_bytes(
        canonical_json(
            {
                "job_id": job.id,
                "request": job.request_json,
                "resolved_request": job.resolved_request_json,
                "revision_id": revision.id if revision else None,
                "content_hash": revision.content_hash if revision else None,
            }
        )
    )


def _enforce_strategy_change(
    session: Session,
    *,
    job: GenerationJob,
    payload: RemediationCreate,
) -> None:
    finding_ids = set(payload.finding_ids)
    if not finding_ids:
        return
    findings = list(
        session.scalars(
            select(ProductionFinding).where(
                ProductionFinding.job_id == job.id,
                ProductionFinding.id.in_(finding_ids),
            )
        ).all()
    )
    if len(findings) != len(finding_ids):
        from .services import ServiceError

        raise ServiceError(422, "remediation references unknown findings")
    if any(finding.occurrence >= 3 for finding in findings):
        from .services import ServiceError

        raise ServiceError(409, "the same blocking finding occurred three times; user input is required")
    if not any(finding.occurrence >= 2 for finding in findings):
        return
    previous = session.scalar(
        select(RemediationAction)
        .where(RemediationAction.job_id == job.id)
        .order_by(RemediationAction.created_at.desc())
        .limit(1)
    )
    if previous and previous.action == payload.action.value and previous.strategy == payload.strategy:
        from .services import ServiceError

        raise ServiceError(409, "a repeated blocking finding requires a different strategy")


def create_remediation(
    session: Session,
    *,
    job: GenerationJob,
    payload: RemediationCreate,
) -> RemediationAction:
    from .services import ServiceError

    plan = session.get(GenerationPlan, job.plan_id)
    if plan is None:
        raise ServiceError(404, "generation plan not found")
    if job.status not in {
        GenerationStatus.CANDIDATE_READY.value,
        GenerationStatus.SUCCEEDED.value,
        GenerationStatus.QA_FAILED.value,
        GenerationStatus.AWAITING_USER.value,
        GenerationStatus.FAILED.value,
    }:
        raise ServiceError(409, "job is not ready for a remediation decision")
    if job.pending_action_id:
        raise ServiceError(409, "job already has a pending remediation action")
    if payload.action == RemediationKind.TOOL_REPAIR:
        from .media_worker import REGISTERED_STRATEGIES

        if job.task_kind not in {"image", "image_edit"}:
            raise ServiceError(422, "tool repair requires an image job")
        if payload.strategy not in REGISTERED_STRATEGIES:
            raise ServiceError(422, f"unknown media worker strategy: {payload.strategy}")
    _enforce_strategy_change(session, job=job, payload=payload)

    paid = payload.action in {RemediationKind.REGENERATE, RemediationKind.IMAGE_EDIT}
    if payload.action == RemediationKind.RETRY:
        paid = False
    expected_calls = 1 if paid else 0
    if payload.expected_additional_calls not in {0, expected_calls}:
        raise ServiceError(422, "expected additional calls do not match the selected action")
    if paid:
        if plan.extra_calls_used + 1 > plan.extra_call_budget:
            job.status = GenerationStatus.AWAITING_USER.value
            refresh_plan_status(session, plan)
            session.commit()
            raise ServiceError(409, "plan extra-call budget is exhausted")
        if job.paid_remediation_rounds + 1 > plan.max_paid_remediation_rounds:
            job.status = GenerationStatus.AWAITING_USER.value
            refresh_plan_status(session, plan)
            session.commit()
            raise ServiceError(409, "asset paid-remediation round limit is exhausted")

    action = RemediationAction(
        id=new_id(),
        project_id=job.project_id,
        plan_id=job.plan_id,
        job_id=job.id,
        action=payload.action.value,
        strategy=payload.strategy,
        status=RemediationStatus.ACCEPTED.value,
        reason=payload.reason,
        parameters_json=payload.parameters,
        finding_ids_json=payload.finding_ids,
        input_hash=current_job_input_hash(session, job),
        expected_additional_calls=expected_calls,
    )
    session.add(action)
    session.flush()
    job.pending_action_id = action.id
    job.error_category = None
    job.error_message = None
    job.cancel_requested = False
    job.progress = 0.0
    job.lease_owner = None
    job.lease_token = None
    job.lease_expires_at = None
    job.heartbeat_at = None
    if payload.action == RemediationKind.AWAIT_USER:
        job.status = GenerationStatus.AWAITING_USER.value
        action.status = RemediationStatus.COMPLETED.value
        action.completed_at = utcnow()
        job.pending_action_id = None
    else:
        job.status = GenerationStatus.QUEUED.value
        job.stage = (
            RunStage.HARD_QA.value
            if payload.action == RemediationKind.TOOL_REPAIR
            else RunStage.QUEUED.value
        )
        if paid:
            plan.extra_calls_used += 1
            job.paid_remediation_rounds += 1
    event = record_run_event(
        session,
        plan_id=job.plan_id,
        project_id=job.project_id,
        job_id=job.id,
        asset_id=str(job.request_json.get("asset_id")),
        event_type="action.accepted",
        stage=job.stage,
        data={
            "action_id": action.id,
            "action": action.action,
            "strategy": action.strategy,
            "expected_additional_calls": action.expected_additional_calls,
        },
    )
    record_run_event(
        session,
        plan_id=job.plan_id,
        project_id=job.project_id,
        job_id=job.id,
        asset_id=str(job.request_json.get("asset_id")),
        event_type="budget.changed",
        stage=job.stage,
        causation_id=event.id,
        data={
            "extra_call_budget": plan.extra_call_budget,
            "extra_calls_used": plan.extra_calls_used,
            "paid_remediation_rounds": job.paid_remediation_rounds,
        },
    )
    refresh_plan_status(session, plan)
    session.commit()
    return action


def update_extra_call_budget(
    session: Session, *, plan: GenerationPlan, extra_call_budget: int
) -> GenerationPlan:
    from .services import ServiceError

    if extra_call_budget < plan.extra_calls_used:
        raise ServiceError(409, "budget cannot be lower than calls already reserved")
    previous = plan.extra_call_budget
    plan.extra_call_budget = extra_call_budget
    record_run_event(
        session,
        plan_id=plan.id,
        project_id=plan.project_id,
        event_type="budget.changed",
        data={"previous": previous, "current": extra_call_budget},
    )
    session.commit()
    return plan


def resume_recoverable_jobs(
    session: Session,
    *,
    plan: GenerationPlan,
    available_provider_ids: set[str],
) -> list[GenerationJob]:
    jobs = list(
        session.scalars(
            select(GenerationJob)
            .where(GenerationJob.plan_id == plan.id)
            .order_by(GenerationJob.created_at)
        ).all()
    )
    for job in jobs:
        latest = session.scalar(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == job.id)
            .order_by(GenerationAttempt.number.desc())
            .limit(1)
        )
        recoverable_output = attempt_output_is_valid(session, job, latest)
        action = (
            session.get(RemediationAction, job.pending_action_id)
            if job.pending_action_id
            else None
        )
        recoverable_action = bool(
            action
            and action.action == RemediationKind.TOOL_REPAIR.value
            and action.status
            in {
                RemediationStatus.ACCEPTED.value,
                RemediationStatus.RUNNING.value,
            }
        )
        if (
            job.status == GenerationStatus.CREDENTIALS_LOCKED.value
            and job.provider_profile_id in available_provider_ids
        ):
            job.status = GenerationStatus.QUEUED.value
        elif job.status == GenerationStatus.AWAITING_USER.value and (
            recoverable_output or recoverable_action
        ):
            job.status = GenerationStatus.QUEUED.value
        else:
            continue
        job.error_category = None
        job.error_message = None
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None
        job.updated_at = utcnow()
        record_run_event(
            session,
            plan_id=plan.id,
            project_id=plan.project_id,
            job_id=job.id,
            asset_id=str(job.request_json.get("asset_id")),
            event_type="run.resumed",
            stage=job.stage,
            data={"recoverable_output": recoverable_output},
        )
    refresh_plan_status(session, plan)
    session.commit()
    return jobs
