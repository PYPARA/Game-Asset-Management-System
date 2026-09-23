"""Audited, idempotent repair of the old pre-send DNS-guard accounting bug."""
from __future__ import annotations

import re
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import GenerationAttempt, GenerationJob, GenerationPlan, RunEvent, utcnow
from .production import record_run_event

_GUARD_ERROR = re.compile(
    r"provider hostname resolves to a private or reserved address \([^)]+\); "
    r"enable private network access explicitly"
)


def reconcile_local_failures(session: Session, plan: GenerationPlan) -> list[str]:
    # The caller selects a plan explicitly. Never requeue work in this repair.
    session.execute(update(GenerationPlan).where(GenerationPlan.id == plan.id).values(name=plan.name))
    session.refresh(plan)
    attempts = session.scalars(select(GenerationAttempt).join(GenerationJob).where(
        GenerationJob.plan_id == plan.id,
        GenerationAttempt.dispatch_state == "legacy_unknown",
        GenerationAttempt.phase == "failed",
        GenerationAttempt.error_category == "validation",
    )).all()
    repaired = []
    for attempt in attempts:
        if (not _GUARD_ERROR.fullmatch(attempt.error_message or "") or attempt.request_id
                or attempt.output_path or attempt.result_revision_id):
            continue
        events = session.scalars(select(RunEvent).where(RunEvent.attempt_id == attempt.id)).all()
        if not any(e.event_type == "provider.call_started" for e in events):
            continue
        if not any(e.event_type == "provider.call_failed"
                   and e.data_json.get("message") == attempt.error_message for e in events):
            continue
        if any(e.event_type in {"artifact.created", "provider.call_completed"} for e in events):
            continue
        previous_cost = attempt.estimated_cost
        previous_billable = attempt.billable
        if previous_billable:
            if plan.actual_calls < 1 or plan.actual_cost + 1e-8 < (previous_cost or 0):
                raise ValueError("accounting totals are inconsistent; manual reconciliation required")
            plan.actual_calls -= 1
            plan.actual_cost = max(0, plan.actual_cost - (previous_cost or 0))
        attempt.dispatch_state = "not_sent"
        attempt.dispatch_count = 0
        attempt.billable = False
        attempt.estimated_cost = None
        attempt.error_code = "provider_address_blocked"
        attempt.error_hint = "请求在本地 DNS 校验阶段被阻止；网络策略已修复，可继续执行已确认任务。"
        job = session.get(GenerationJob, attempt.job_id)
        if job.status in {"awaiting_user", "failed"}:
            latest = session.scalar(select(GenerationAttempt).where(
                GenerationAttempt.job_id == job.id).order_by(GenerationAttempt.number.desc()).limit(1))
            if latest.id == attempt.id:
                job.error_category = attempt.error_category
                job.error_message = attempt.error_message
                job.updated_at = utcnow()
        record_run_event(session, plan_id=plan.id, project_id=plan.project_id,
                         job_id=job.id, attempt_id=attempt.id,
                         event_type="provider.accounting_corrected", stage=job.stage,
                         data={"reason": "legacy_pre_dispatch_dns_guard", "previous_billable": previous_billable,
                               "previous_estimated_cost": previous_cost, "dispatch_state": "not_sent",
                               "actual_calls": plan.actual_calls})
        repaired.append(attempt.id)
    session.commit()
    return repaired
