from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
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
    AgentEvent,
    AgentSession,
    AssetRevision,
    ChangeSet,
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


def execution_diagnostic(job: GenerationJob, latest: GenerationAttempt | None) -> dict[str, Any]:
    state = latest.dispatch_state if latest else None
    stopped = job.status in {"awaiting_user", "failed", "credentials_locked", "qa_failed"}
    reason = None
    if job.status not in SUCCESS_STATUSES and job.status != "cancelled":
        reason = job.error_message or (latest.error_message if latest else None)
        if stopped and latest:
            if state == "not_sent":
                detail = "供应商地址检查未通过" if latest.error_code == "provider_address_blocked" else (reason or "本地准备失败")
                reason = f"请求未发出：{detail}"
            elif state == "dispatch_started":
                reason = "交付状态未知，需要核对。" + (reason or "请求发送后未收到可确认的响应。")
        if reason and latest and latest.error_hint:
            reason += "；" + latest.error_hint
    return {"blocking_reason": reason, "delivery_state": state,
            "recovery_eligible": bool(stopped and latest and state == "not_sent"
                                      and not job.result_revision_id and not job.pending_action_id)}


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
    agent_sessions = list(
        session.scalars(
            select(AgentSession)
            .where(AgentSession.plan_id == plan.id)
            .order_by(AgentSession.created_at, AgentSession.id)
        ).all()
    )
    agent_session_ids = [item.id for item in agent_sessions]
    agent_events = (
        list(
            session.scalars(
                select(AgentEvent)
                .where(AgentEvent.session_id.in_(agent_session_ids))
                .order_by(AgentEvent.sequence)
            ).all()
        )
        if agent_session_ids
        else []
    )
    changesets = (
        list(
            session.scalars(
                select(ChangeSet)
                .where(ChangeSet.session_id.in_(agent_session_ids))
                .order_by(ChangeSet.created_at, ChangeSet.id)
            ).all()
        )
        if agent_session_ids
        else []
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
    projected_jobs = []
    from .domain import GenerationJobRead
    for job in jobs:
        latest = next((a for a in reversed(attempts) if a.job_id == job.id), None)
        projected_jobs.append(GenerationJobRead.model_validate(job).model_copy(
            update=execution_diagnostic(job, latest)))
    return {
        "plan": plan,
        "jobs": projected_jobs,
        "attempts": attempts,
        "findings": findings,
        "evidence": evidence,
        "actions": actions,
        "events": events,
        "agent_sessions": agent_sessions,
        "agent_events": agent_events,
        "changesets": changesets,
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
    # Serialize decisions before checking the current state (including duplicate clicks).
    session.execute(update(GenerationJob).where(GenerationJob.id == job.id).values(updated_at=utcnow()))
    session.refresh(job)
    action_id = stable_id("remediation", job.id, payload.idempotency_key) if payload.idempotency_key else new_id()
    existing = session.get(RemediationAction, action_id)
    if existing:
        if (existing.action != payload.action.value or existing.strategy != payload.strategy
                or existing.reason != payload.reason or existing.parameters_json != payload.parameters
                or existing.finding_ids_json != payload.finding_ids):
            raise ServiceError(409, "幂等键已用于不同操作。")
        session.commit()
        return existing
    previous_action = session.scalar(select(RemediationAction).where(
        RemediationAction.job_id == job.id).order_by(RemediationAction.created_at.desc()).limit(1))
    if previous_action and previous_action.action == payload.action.value and previous_action.strategy == payload.strategy:
        same = (previous_action.reason == payload.reason and previous_action.parameters_json == payload.parameters
                and previous_action.finding_ids_json == payload.finding_ids)
        if same and ((payload.action == RemediationKind.AWAIT_USER and job.status == "awaiting_user")
                     or job.pending_action_id == previous_action.id):
            session.commit()
            return previous_action
    latest = session.scalar(select(GenerationAttempt).where(
        GenerationAttempt.job_id == job.id).order_by(GenerationAttempt.number.desc()).limit(1))
    if payload.action == RemediationKind.RETRY and latest:
        if latest.dispatch_state in {"dispatch_started", "legacy_unknown"} and not attempt_output_is_valid(session, job, latest):
            raise ServiceError(409, "交付状态未知，请先核对供应商结果；不能直接重试。")
        if payload.parameters:
            raise ServiceError(422, "同请求重试不能修改已确认参数。")

    if job.status not in {
        GenerationStatus.CANDIDATE_READY.value,
        GenerationStatus.SUCCEEDED.value,
        GenerationStatus.QA_FAILED.value,
        GenerationStatus.AWAITING_USER.value,
        GenerationStatus.FAILED.value,
        GenerationStatus.CREDENTIALS_LOCKED.value,
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
    if payload.action == RemediationKind.IMAGE_EDIT and job.task_kind not in {"image", "image_edit"}:
        raise ServiceError(422, "image edit requires an image job")
    if payload.action == RemediationKind.PROPOSE_CHANGESET:
        raise ServiceError(422, "propose_changeset is only accepted through the Agent ChangeSet workflow")
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
        id=action_id,
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
    job.cancel_requested = False
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
        if (job.status in {GenerationStatus.AWAITING_USER.value, GenerationStatus.CREDENTIALS_LOCKED.value}
                and latest and latest.dispatch_state == "not_sent" and not job.pending_action_id):
            create_remediation(session, job=job, payload=RemediationCreate(
                action=RemediationKind.RETRY, strategy="same-request", reason="继续执行已确认任务",
                idempotency_key=f"resume:{job.id}:{job.attempt_count}",
            ))
            continue
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
