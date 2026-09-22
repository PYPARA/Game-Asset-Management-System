from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

from PIL import Image
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from .domain import (
    ErrorCategory,
    GenerationStatus,
    QARunCreate,
    QAVerdict,
    RemediationKind,
    RemediationStatus,
    RevisionCreate,
    RevisionFormat,
    RunStage,
    TaskKind,
)
from .media_worker import apply_media_worker, make_comparison_bundle, make_contact_sheets
from .models import (
    Asset,
    AssetRevision,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    Project,
    ProviderProfile,
    QARun,
    RemediationAction,
    Rendition,
    RunEvidence,
    new_id,
    utcnow,
)
from .production import (
    attempt_output_is_valid,
    current_job_input_hash,
    findings_from_qa,
    record_run_event,
    refresh_plan_status,
)
from .providers import (
    CredentialVault,
    ProviderError,
    ProviderResult,
    build_provider,
    provider_runtime_config,
)
from .provider_catalog import provider_credentials_ready
from .qa import normalize_image
from .services import ServiceError, asset_descriptor, create_revision, run_qa
from .settings import Settings
from .storage import (
    ProjectStore,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    relative_to_root,
    safe_join,
    sha256_bytes,
    sha256_file,
    stable_id,
)


READY_DEPENDENCY_STATUSES = {
    GenerationStatus.CANDIDATE_READY.value,
    GenerationStatus.SUCCEEDED.value,
}
LEASED_STATUSES = {
    GenerationStatus.RUNNING.value,
    GenerationStatus.OUTPUT_RECEIVED.value,
    GenerationStatus.HARD_QA.value,
    GenerationStatus.SEMANTIC_QA.value,
    GenerationStatus.REMEDIATING.value,
}
RETRYABLE_ERROR_CATEGORIES = {
    ErrorCategory.RATE_LIMIT.value,
    ErrorCategory.SERVER.value,
    ErrorCategory.NETWORK.value,
    ErrorCategory.EMPTY.value,
}


class JobRunner:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        vault: CredentialVault,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.vault = vault
        self.settings = settings
        self.runner_id = f"runner-{uuid.uuid4()}"
        self._task: asyncio.Task[None] | None = None
        self._active: dict[str, asyncio.Task[None]] = {}
        self._evidence_locks: dict[str, asyncio.Lock] = {}
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._stopping.clear()
        self.recover_interrupted_jobs()
        self._task = asyncio.create_task(self._loop(), name="game-assets-job-runner")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()

    def _attempt_output_valid(
        self,
        session: Session,
        job: GenerationJob,
        attempt: GenerationAttempt,
    ) -> bool:
        return attempt_output_is_valid(session, job, attempt)

    @staticmethod
    def _attempt_action_id(attempt: GenerationAttempt | None) -> str | None:
        if attempt is None:
            return None
        request = attempt.request_json if isinstance(attempt.request_json, dict) else {}
        remediation = request.get("remediation")
        if isinstance(remediation, dict) and remediation.get("action_id"):
            return str(remediation["action_id"])
        if request.get("action_id"):
            return str(request["action_id"])
        return None

    @staticmethod
    def _known_retry_available(
        session: Session,
        job: GenerationJob,
        attempt: GenerationAttempt | None,
    ) -> bool:
        if (
            attempt is None
            or attempt.phase != "failed"
            or attempt.error_category not in RETRYABLE_ERROR_CATEGORIES
            or not attempt.request_hash
        ):
            return False
        plan = session.get(GenerationPlan, job.plan_id)
        if plan is None:
            return False
        used = session.scalar(
            select(func.count(GenerationAttempt.id)).where(
                GenerationAttempt.job_id == job.id,
                GenerationAttempt.request_hash == attempt.request_hash,
                GenerationAttempt.billable.is_(True),
            )
        ) or 0
        return int(used) < plan.max_transport_retries + 1

    def _recovery_state(
        self,
        session: Session,
        job: GenerationJob,
        latest: GenerationAttempt | None,
    ) -> tuple[str, str, bool, RemediationAction | None]:
        action = (
            session.get(RemediationAction, job.pending_action_id)
            if job.pending_action_id
            else None
        )
        recoverable_output = attempt_output_is_valid(session, job, latest)
        action_attempted = bool(
            action and self._attempt_action_id(latest) == action.id
        )
        tool_repair = bool(
            action and action.action == RemediationKind.TOOL_REPAIR.value
        )
        safe_before_dispatch = bool(action and not action_attempted)
        known_retry = self._known_retry_available(session, job, latest)

        if latest and latest.phase == "succeeded" and latest.result_revision_id and not action:
            return (
                GenerationStatus.CANDIDATE_READY.value,
                "recovered completed candidate",
                recoverable_output,
                action,
            )
        if (
            latest is None
            or latest.phase == "created"
            or recoverable_output
            or tool_repair
            or safe_before_dispatch
            or known_retry
        ):
            requires_provider = not (recoverable_output or tool_repair)
            profile = session.get(ProviderProfile, job.provider_profile_id)
            credentials_ready = bool(
                profile
                and (
                    not requires_provider
                    or provider_credentials_ready(
                        profile, unlocked=self.vault.is_unlocked(profile.id)
                    )
                )
            )
            message = (
                "recovered from persisted output"
                if recoverable_output
                else "recovered deterministic worker"
                if tool_repair
                else "recovered known retryable failure"
                if known_retry
                else "recovered before provider dispatch"
            )
            return (
                GenerationStatus.QUEUED.value
                if credentials_ready
                else GenerationStatus.CREDENTIALS_LOCKED.value,
                message,
                recoverable_output,
                action,
            )
        return (
            GenerationStatus.AWAITING_USER.value,
            "provider delivery is unknown after interruption; choose an explicit remediation action",
            recoverable_output,
            action,
        )

    def recover_interrupted_jobs(self) -> None:
        with self.sessions() as session:
            jobs = list(
                session.scalars(
                    select(GenerationJob).where(GenerationJob.status.in_(LEASED_STATUSES))
                ).all()
            )
            for job in jobs:
                previous_runner = job.lease_owner
                latest = session.scalar(
                    select(GenerationAttempt)
                    .where(GenerationAttempt.job_id == job.id)
                    .order_by(GenerationAttempt.number.desc())
                    .limit(1)
                )
                target, message, recoverable_output, action = self._recovery_state(
                    session, job, latest
                )
                job.status = target
                if target == GenerationStatus.CANDIDATE_READY.value and latest:
                    job.result_revision_id = latest.result_revision_id
                    job.stage = RunStage.CANDIDATE_READY.value
                job.error_category = (
                    ErrorCategory.NETWORK.value
                    if target == GenerationStatus.AWAITING_USER.value
                    else None
                )
                job.error_message = (
                    message
                    if target
                    in {
                        GenerationStatus.AWAITING_USER.value,
                        GenerationStatus.CREDENTIALS_LOCKED.value,
                    }
                    else None
                )
                if target == GenerationStatus.AWAITING_USER.value and action:
                    action.status = RemediationStatus.FAILED.value
                    action.completed_at = utcnow()
                    job.pending_action_id = None
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.updated_at = utcnow()
                plan = session.get(GenerationPlan, job.plan_id)
                if plan:
                    record_run_event(
                        session,
                        plan_id=job.plan_id,
                        project_id=job.project_id,
                        job_id=job.id,
                        asset_id=str(job.request_json.get("asset_id")),
                        attempt_id=latest.id if latest else None,
                        event_type=(
                            "run.recovered"
                            if job.status != GenerationStatus.AWAITING_USER.value
                            else "run.awaiting_user"
                        ),
                        stage=job.stage,
                        data={
                            "previous_runner": previous_runner,
                            "recoverable_output": recoverable_output,
                            "reason": message,
                        },
                    )
                    refresh_plan_status(session, plan)
                asset = session.get(Asset, str(job.request_json.get("asset_id")))
                if asset:
                    asset.generation_status = job.status
            session.commit()

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            self._reap_active()
            try:
                claimed = self._claim_ready_jobs()
            except asyncio.CancelledError:
                raise
            except Exception:
                claimed = []
            for job_id, lease_token in claimed:
                task = asyncio.create_task(
                    self._execute_claimed_job(job_id, lease_token),
                    name=f"game-assets-job-{job_id}",
                )
                self._active[job_id] = task
            if not claimed:
                await asyncio.sleep(self.settings.job_poll_interval)

    def _reap_active(self) -> None:
        for job_id, task in list(self._active.items()):
            if not task.done():
                continue
            with suppress(asyncio.CancelledError, Exception):
                task.result()
            self._active.pop(job_id, None)

    def _expire_stale_leases(self, session: Session) -> None:
        now = utcnow()
        stale = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.status.in_(LEASED_STATUSES),
                    GenerationJob.lease_expires_at.is_not(None),
                    GenerationJob.lease_expires_at < now,
                )
            ).all()
        )
        for job in stale:
            latest = session.scalar(
                select(GenerationAttempt)
                .where(GenerationAttempt.job_id == job.id)
                .order_by(GenerationAttempt.number.desc())
                .limit(1)
            )
            target, message, _recoverable_output, action = self._recovery_state(
                session, job, latest
            )
            job.status = target
            job.error_category = (
                ErrorCategory.NETWORK.value
                if target == GenerationStatus.AWAITING_USER.value
                else None
            )
            if target == GenerationStatus.AWAITING_USER.value and action:
                action.status = RemediationStatus.FAILED.value
                action.completed_at = utcnow()
                job.pending_action_id = None
            job.error_message = message
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.updated_at = now
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                attempt_id=latest.id if latest else None,
                event_type=(
                    "run.recovered"
                    if job.status == GenerationStatus.QUEUED.value
                    else "run.awaiting_user"
                ),
                stage=job.stage,
                data={"reason": message},
            )
            plan = session.get(GenerationPlan, job.plan_id)
            if plan:
                refresh_plan_status(session, plan)
            asset = session.get(Asset, str(job.request_json.get("asset_id")))
            if asset:
                asset.generation_status = job.status

    def _dependencies_ready(self, session: Session, job: GenerationJob) -> bool:
        dependencies = list(job.request_json.get("depends_on", []))
        if not dependencies:
            return True
        dependency_jobs = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.plan_id == job.plan_id,
                    GenerationJob.task_id.in_(dependencies),
                )
            ).all()
        )
        by_task = {dependency.task_id: dependency for dependency in dependency_jobs}
        blocked = next(
            (
                task_id
                for task_id in dependencies
                if task_id in by_task
                and by_task[task_id].status
                in {
                    GenerationStatus.AWAITING_USER.value,
                    GenerationStatus.FAILED.value,
                    GenerationStatus.CANCELLED.value,
                    GenerationStatus.QA_FAILED.value,
                }
            ),
            None,
        )
        if blocked:
            job.status = GenerationStatus.AWAITING_USER.value
            job.error_category = ErrorCategory.VALIDATION.value
            job.error_message = f"dependency {blocked} requires attention"
            job.updated_at = utcnow()
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                event_type="run.awaiting_user",
                stage=job.stage,
                data={"dependency": blocked},
            )
            return False
        return all(
            task_id in by_task and by_task[task_id].status in READY_DEPENDENCY_STATUSES
            for task_id in dependencies
        )

    def _claim_ready_jobs(self, *, limit: int = 64) -> list[tuple[str, str]]:
        with self.sessions() as session:
            self._expire_stale_leases(session)
            now = utcnow()
            active_jobs = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.status.in_(LEASED_STATUSES),
                        GenerationJob.lease_expires_at.is_not(None),
                        GenerationJob.lease_expires_at >= now,
                    )
                ).all()
            )
            provider_counts: dict[str, int] = {}
            plan_counts: dict[str, int] = {}
            for active in active_jobs:
                provider_counts[active.provider_profile_id] = provider_counts.get(active.provider_profile_id, 0) + 1
                plan_counts[active.plan_id] = plan_counts.get(active.plan_id, 0) + 1

            queued = list(
                session.scalars(
                    select(GenerationJob)
                    .where(GenerationJob.status == GenerationStatus.QUEUED.value)
                    .order_by(GenerationJob.created_at, GenerationJob.task_id)
                ).all()
            )
            claimed: list[tuple[str, str]] = []
            for job in queued:
                if len(claimed) >= limit:
                    break
                if job.cancel_requested:
                    job.status = GenerationStatus.CANCELLED.value
                    job.error_category = ErrorCategory.CANCELLED.value
                    job.error_message = "cancelled before execution"
                    continue
                if not self._dependencies_ready(session, job):
                    continue
                profile = session.get(ProviderProfile, job.provider_profile_id)
                plan = session.get(GenerationPlan, job.plan_id)
                asset = session.get(Asset, str(job.request_json.get("asset_id")))
                if profile is None or plan is None or asset is None:
                    job.status = GenerationStatus.FAILED.value
                    job.error_category = ErrorCategory.VALIDATION.value
                    job.error_message = "job references a missing provider, plan, or asset"
                    continue
                plan_limit = max(1, plan.max_concurrency)
                snapshot = job.provider_snapshot_json or {}
                try:
                    runtime_policy_version = int(snapshot.get("runtime_policy_version", 1))
                except (TypeError, ValueError):
                    runtime_policy_version = 1
                if runtime_policy_version >= 2:
                    # New jobs are governed only by their frozen plan limit.
                    provider_limit = None
                else:
                    try:
                        provider_limit = max(
                            1,
                            int(snapshot.get("concurrency", profile.concurrency)),
                        )
                    except (TypeError, ValueError):
                        provider_limit = max(1, profile.concurrency)
                if (
                    provider_limit is not None
                    and provider_counts.get(profile.id, 0) >= provider_limit
                ):
                    continue
                if plan_counts.get(plan.id, 0) >= plan_limit:
                    continue
                token = str(uuid.uuid4())
                expires = now + timedelta(seconds=self.settings.job_lease_seconds)
                result = session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job.id,
                        GenerationJob.status == GenerationStatus.QUEUED.value,
                    )
                    .values(
                        status=GenerationStatus.RUNNING.value,
                        lease_owner=self.runner_id,
                        lease_token=token,
                        lease_expires_at=expires,
                        heartbeat_at=now,
                        progress=max(job.progress, 0.03),
                        error_category=None,
                        error_message=None,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    continue
                job.status = GenerationStatus.RUNNING.value
                job.lease_owner = self.runner_id
                job.lease_token = token
                job.lease_expires_at = expires
                job.heartbeat_at = now
                asset.generation_status = GenerationStatus.RUNNING.value
                provider_counts[profile.id] = provider_counts.get(profile.id, 0) + 1
                plan_counts[plan.id] = plan_counts.get(plan.id, 0) + 1
                refresh_plan_status(session, plan)
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=asset.id,
                    event_type="run.lease_acquired",
                    stage=job.stage,
                    data={
                        "runner": self.runner_id,
                        "expires_at": expires.isoformat(),
                        "provider_concurrency": provider_limit,
                        "plan_concurrency": plan_limit,
                    },
                )
                claimed.append((job.id, token))
            session.commit()
            return claimed

    async def run_once(self) -> bool:
        claimed = self._claim_ready_jobs(limit=1)
        if not claimed:
            return False
        await self._execute_claimed_job(*claimed[0])
        return True

    async def _heartbeat(self, job_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(self.settings.job_heartbeat_interval)
            now = utcnow()
            with self.sessions() as session:
                result = session.execute(
                    update(GenerationJob)
                    .where(
                        GenerationJob.id == job_id,
                        GenerationJob.lease_token == lease_token,
                        GenerationJob.status.in_(LEASED_STATUSES),
                    )
                    .values(
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=self.settings.job_lease_seconds),
                        updated_at=now,
                    )
                )
                session.commit()
                if result.rowcount != 1:
                    return

    async def _execute_claimed_job(self, job_id: str, lease_token: str) -> None:
        heartbeat = asyncio.create_task(self._heartbeat(job_id, lease_token))
        try:
            with self.sessions() as session:
                job = session.get(GenerationJob, job_id)
                if job is None or job.lease_token != lease_token:
                    return
                action = (
                    session.get(RemediationAction, job.pending_action_id)
                    if job.pending_action_id
                    else None
                )
            if action and action.action == RemediationKind.TOOL_REPAIR.value:
                await asyncio.to_thread(self._execute_tool_remediation, job_id, lease_token, action.id)
            elif action and action.action == RemediationKind.AWAIT_USER.value:
                self._set_awaiting_user(job_id, "the accepted action requires user input")
            else:
                await self._execute_provider_job(job_id, lease_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._set_awaiting_user(
                job_id,
                f"controller could not safely continue: {type(exc).__name__}: {exc}",
            )
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def _resolve_request(
        self,
        session: Session,
        job: GenerationJob,
        action: RemediationAction | None,
    ) -> dict[str, Any]:
        request = dict(job.request_json)
        dependencies = list(request.get("depends_on", []))
        dependency_jobs = list(
            session.scalars(
                select(GenerationJob).where(
                    GenerationJob.plan_id == job.plan_id,
                    GenerationJob.task_id.in_(dependencies),
                )
            ).all()
        ) if dependencies else []
        by_task = {dependency.task_id: dependency for dependency in dependency_jobs}
        upstream: list[dict[str, Any]] = []
        for task_id in dependencies:
            dependency = by_task.get(task_id)
            revision = (
                session.get(AssetRevision, dependency.result_revision_id)
                if dependency and dependency.result_revision_id
                else None
            )
            if dependency is None or revision is None or dependency.status not in READY_DEPENDENCY_STATUSES:
                raise ServiceError(409, f"dependency {task_id} has no candidate output")
            upstream.append(
                {
                    "task_id": task_id,
                    "job_id": dependency.id,
                    "revision_id": revision.id,
                    "content_hash": revision.content_hash,
                    "content": revision.content_json,
                }
            )
        request["upstream_outputs"] = upstream
        prompt = str(request.get("prompt", ""))
        if upstream:
            prompt += "\n\n[UPSTREAM_OUTPUTS]\n" + json.dumps(
                upstream, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        if action:
            parameters = action.parameters_json
            if isinstance(parameters.get("prompt"), str):
                prompt = str(parameters["prompt"])
            if isinstance(parameters.get("prompt_suffix"), str):
                prompt += "\n" + str(parameters["prompt_suffix"])
            if action.action == RemediationKind.IMAGE_EDIT.value:
                request["kind"] = TaskKind.IMAGE_EDIT.value
        request["prompt"] = prompt

        reference_path = request.get("reference_path")
        reference_task_id = request.get("reference_task_id")
        if not reference_path and reference_task_id:
            reference_path = self._job_rendition_path(session, by_task.get(str(reference_task_id)))
        if not reference_path and request.get("kind") == TaskKind.IMAGE_EDIT.value:
            reference_path = self._job_rendition_path(session, job)
            if not reference_path and dependency_jobs:
                reference_path = self._job_rendition_path(session, dependency_jobs[0])
        if reference_path:
            request["reference_path"] = reference_path
        request["remediation"] = (
            {
                "action_id": action.id,
                "action": action.action,
                "strategy": action.strategy,
                "reason": action.reason,
                "parameters": action.parameters_json,
            }
            if action
            else None
        )
        job.resolved_request_json = request
        record_run_event(
            session,
            plan_id=job.plan_id,
            project_id=job.project_id,
            job_id=job.id,
            asset_id=str(job.request_json.get("asset_id")),
            event_type="run.inputs_resolved",
            stage=job.stage,
            data={
                "upstream": [
                    {
                        "task_id": item["task_id"],
                        "revision_id": item["revision_id"],
                        "content_hash": item["content_hash"],
                    }
                    for item in upstream
                ],
                "reference_path": reference_path,
            },
        )
        session.flush()
        return request

    @staticmethod
    def _job_rendition_path(session: Session, job: GenerationJob | None) -> str | None:
        if job is None or not job.result_revision_id:
            return None
        rendition = session.scalar(
            select(Rendition).where(Rendition.revision_id == job.result_revision_id).limit(1)
        )
        return (rendition.normalized_path or rendition.source_path) if rendition else None

    @staticmethod
    def _call_price(
        profile: ProviderProfile,
        task_kind: str,
        snapshot: dict[str, Any] | None = None,
    ) -> float | None:
        pricing = (snapshot or {}).get("pricing", profile.pricing)
        if not isinstance(pricing, dict):
            return None
        key = "text_call" if task_kind == TaskKind.TEXT.value else "image_call"
        if task_kind == TaskKind.IMAGE_EDIT.value:
            key = "image_edit_call" if "image_edit_call" in pricing else "image_call"
        value = pricing.get(key)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _start_provider_attempt(
        self,
        *,
        job_id: str,
        lease_token: str,
        request: dict[str, Any],
        request_hash: str,
        idempotency_key: str,
        purpose: str,
    ) -> tuple[str, int, float | None]:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None or job.lease_token != lease_token:
                raise ProviderError("job lease was lost", ErrorCategory.CANCELLED)
            plan = session.get(GenerationPlan, job.plan_id)
            profile = session.get(ProviderProfile, job.provider_profile_id)
            if plan is None or profile is None:
                raise ProviderError("job plan or provider is missing", ErrorCategory.VALIDATION)
            number = job.attempt_count + 1
            price = self._call_price(
                profile,
                str(request["kind"]),
                job.provider_snapshot_json,
            )
            attempt = GenerationAttempt(
                id=new_id(),
                job_id=job.id,
                number=number,
                status=GenerationStatus.RUNNING.value,
                phase="dispatched",
                purpose=purpose,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                request_json=request,
                billable=True,
                estimated_cost=price,
            )
            job.attempt_count = number
            job.updated_at = utcnow()
            plan.actual_calls += 1
            if price is not None:
                plan.actual_cost += price
            session.add(attempt)
            session.flush()
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                attempt_id=attempt.id,
                event_type="provider.call_started",
                stage=job.stage,
                data={
                    "idempotency_key": idempotency_key,
                    "purpose": purpose,
                    "actual_calls": plan.actual_calls,
                    "estimated_cost": price,
                    "provider_profile_id": job.provider_profile_id,
                    "model": request.get("model"),
                },
            )
            session.commit()
            return attempt.id, number, price

    def _recoverable_attempt(
        self,
        session: Session,
        *,
        job: GenerationJob,
        idempotency_key: str,
        request_hash: str,
    ) -> GenerationAttempt | None:
        attempts = list(
            session.scalars(
                select(GenerationAttempt)
                .where(
                    GenerationAttempt.job_id == job.id,
                    GenerationAttempt.idempotency_key == idempotency_key,
                    GenerationAttempt.request_hash == request_hash,
                )
                .order_by(GenerationAttempt.number.desc())
            ).all()
        )
        for attempt in attempts:
            if attempt.phase in {"output_received", "staged"} and self._attempt_output_valid(
                session, job, attempt
            ):
                return attempt
            if attempt.phase == "succeeded" and attempt.result_revision_id:
                return attempt
        return None

    async def _execute_provider_job(self, job_id: str, lease_token: str) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None or job.lease_token != lease_token:
                return
            profile = session.get(ProviderProfile, job.provider_profile_id)
            plan = session.get(GenerationPlan, job.plan_id)
            project = session.get(Project, job.project_id)
            asset = session.get(Asset, str(job.request_json.get("asset_id")))
            action = (
                session.get(RemediationAction, job.pending_action_id)
                if job.pending_action_id
                else None
            )
            if profile is None or plan is None or project is None or asset is None:
                raise ServiceError(422, "job references missing records")
            if action and current_job_input_hash(session, job) != action.input_hash:
                action.status = RemediationStatus.REJECTED.value
                action.completed_at = utcnow()
                job.pending_action_id = None
                session.commit()
                raise ServiceError(409, "remediation input changed after the action was accepted")
            request = self._resolve_request(session, job, action)
            if not request.get("model"):
                request["model"] = str(
                    job.provider_snapshot_json.get("model")
                    or (
                        profile.text_model
                        if request["kind"] == TaskKind.TEXT.value
                        else profile.image_model
                    )
                )
            schema = request.get("schema")
            if request["kind"] == TaskKind.TEXT.value and schema is None:
                schema = ProjectStore(project.root_path).read_schema(asset.schema_ref)
            request_hash = sha256_bytes(canonical_json({"request": request, "schema": schema}))
            purpose = action.action if action else "base"
            logical_id = action.id if action else "base"
            idempotency_key = stable_id("call", job.id, logical_id, request_hash)
            recoverable = self._recoverable_attempt(
                session,
                job=job,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
            )
            prior_calls = session.scalar(
                select(func.count(GenerationAttempt.id)).where(
                    GenerationAttempt.job_id == job.id,
                    GenerationAttempt.request_hash == request_hash,
                    GenerationAttempt.billable.is_(True),
                )
            ) or 0
            remaining_calls = max(
                0,
                plan.max_transport_retries + 1 - int(prior_calls),
            )
            session.commit()

        if recoverable:
            if recoverable.phase == "succeeded" and recoverable.result_revision_id:
                self._complete_job(
                    job_id,
                    recoverable.id,
                    recoverable.result_revision_id,
                    qa_verdict=None,
                    qa_checks=[],
                )
                return
            result = self._load_provider_result(job_id, recoverable.id)
            await self._stage_and_complete(
                job_id=job_id,
                lease_token=lease_token,
                attempt_id=recoverable.id,
                attempt_number=recoverable.number,
                result=result,
                schema=schema,
            )
            return

        if remaining_calls == 0:
            self._set_awaiting_user(
                job_id,
                "transport retry limit is exhausted for this exact request",
                category=ErrorCategory.NETWORK,
            )
            return

        last_error: ProviderError | None = None
        for retry_index in range(remaining_calls):
            attempt_id, attempt_number, _price = self._start_provider_attempt(
                job_id=job_id,
                lease_token=lease_token,
                request=request,
                request_hash=request_hash,
                idempotency_key=idempotency_key,
                purpose=purpose,
            )
            try:
                with self.sessions() as session:
                    job_context = session.get(GenerationJob, job_id)
                    profile = session.get(ProviderProfile, profile.id)
                    if profile is None or job_context is None:
                        raise ProviderError("provider profile was removed", ErrorCategory.VALIDATION)
                    provider = build_provider(
                        provider_runtime_config(
                            profile, job_context.provider_snapshot_json
                        ),
                        self.vault,
                    )
                result = await self._invoke(
                    provider,
                    job_id=job_id,
                    request=request,
                    schema=schema,
                    idempotency_key=idempotency_key,
                )
                self._persist_provider_result(attempt_id, result)
                await self._stage_and_complete(
                    job_id=job_id,
                    lease_token=lease_token,
                    attempt_id=attempt_id,
                    attempt_number=attempt_number,
                    result=result,
                    schema=schema,
                )
                return
            except ProviderError as exc:
                last_error = exc
                self._finish_attempt_error(attempt_id, exc)
                if exc.category == ErrorCategory.AUTH:
                    self._pause_provider_queue(job_id, GenerationStatus.CREDENTIALS_LOCKED.value, exc)
                    return
                if exc.category in {ErrorCategory.BILLING, ErrorCategory.QUOTA}:
                    self._pause_provider_queue(job_id, GenerationStatus.AWAITING_USER.value, exc)
                    return
                if exc.category == ErrorCategory.CONTENT_POLICY:
                    self._set_awaiting_user(job_id, str(exc), category=exc.category)
                    return
                if (
                    exc.category == ErrorCategory.VALIDATION
                    and "model" in str(exc).lower()
                ):
                    with self.sessions() as session:
                        failed_job = session.get(GenerationJob, job_id)
                        if failed_job:
                            record_run_event(
                                session,
                                plan_id=failed_job.plan_id,
                                project_id=failed_job.project_id,
                                job_id=failed_job.id,
                                asset_id=str(failed_job.request_json.get("asset_id")),
                                attempt_id=attempt_id,
                                event_type="provider.model_unavailable",
                                stage=failed_job.stage,
                                data={
                                    "provider_profile_id": failed_job.provider_profile_id,
                                    "model": request.get("model"),
                                    "message": str(exc),
                                },
                            )
                            session.commit()
                    self._set_awaiting_user(job_id, str(exc), category=exc.category)
                    return
                if not exc.retryable or retry_index >= remaining_calls - 1:
                    break
                stable_jitter = int(request_hash[:2], 16) / 2550
                delay = exc.retry_after if exc.retry_after is not None else min(0.1 * 2**retry_index + stable_jitter, 2.0)
                await asyncio.sleep(delay)
            except (ServiceError, OSError, ValueError) as exc:
                last_error = ProviderError(str(exc), ErrorCategory.VALIDATION)
                self._finish_attempt_error(attempt_id, last_error)
                break
        self._set_awaiting_user(
            job_id,
            str(last_error or ProviderError("provider request could not be completed")),
            category=(last_error.category if last_error else ErrorCategory.UNKNOWN),
        )

    async def _invoke(
        self,
        provider,
        *,
        job_id: str,
        request: dict[str, Any],
        schema: dict[str, Any] | None,
        idempotency_key: str,
    ) -> ProviderResult:
        if request["kind"] == TaskKind.TEXT.value:
            if not schema:
                raise ProviderError("text task has no schema", ErrorCategory.VALIDATION)
            return await provider.structured_text(
                prompt=str(request["prompt"]),
                schema=schema,
                model=str(request["model"]),
                idempotency_key=idempotency_key,
            )
        reference = None
        if request["kind"] == TaskKind.IMAGE_EDIT.value:
            reference_path = request.get("reference_path")
            if not reference_path:
                raise ProviderError("image edit task needs a reference input", ErrorCategory.VALIDATION)
            with self.sessions() as session:
                job = session.get(GenerationJob, job_id)
                project = session.get(Project, job.project_id) if job else None
                if project is None:
                    raise ProviderError("project is missing", ErrorCategory.VALIDATION)
                path = safe_join(Path(project.root_path), str(reference_path))
                if not path.is_file():
                    raise ProviderError("reference image does not exist", ErrorCategory.VALIDATION)
                reference = path.read_bytes()
        return await provider.image(
            prompt=str(request["prompt"]),
            width=request.get("width"),
            height=request.get("height"),
            model=str(request["model"]),
            reference=reference,
            idempotency_key=idempotency_key,
        )

    def _persist_provider_result(self, attempt_id: str, result: ProviderResult) -> None:
        with self.sessions() as session:
            attempt = session.get(GenerationAttempt, attempt_id)
            job = session.get(GenerationJob, attempt.job_id) if attempt else None
            project = session.get(Project, job.project_id) if job else None
            if attempt is None or job is None or project is None:
                raise ServiceError(404, "attempt context is missing")
            store = ProjectStore(project.root_path)
            attempt_dir = store.candidate_dir(job.id) / f"attempt-{attempt.number}"
            if job.task_kind == TaskKind.TEXT.value:
                path = attempt_dir / "provider-output.json"
                digest = atomic_write_json(path, result.value, immutable=not path.exists())
            else:
                if not isinstance(result.value, bytes):
                    raise ServiceError(422, "image provider did not return bytes")
                path = attempt_dir / "source.bin"
                if path.exists():
                    if sha256_file(path) != sha256_bytes(result.value):
                        raise ServiceError(409, "persisted provider output does not match retry output")
                else:
                    atomic_write_bytes(path, result.value, immutable=True)
                digest = sha256_file(path)
            attempt.output_path = relative_to_root(store.root, path)
            attempt.output_hash = digest
            attempt.request_id = result.request_id
            attempt.status = GenerationStatus.SUCCEEDED.value
            attempt.phase = "output_received"
            attempt.completed_at = utcnow()
            job.status = GenerationStatus.OUTPUT_RECEIVED.value
            job.stage = RunStage.OUTPUT_RECEIVED.value
            job.progress = 0.35
            job.updated_at = utcnow()
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                attempt_id=attempt.id,
                event_type="artifact.created",
                stage=job.stage,
                data={
                    "role": "provider_output",
                    "path": attempt.output_path,
                    "sha256": attempt.output_hash,
                    "request_id": attempt.request_id,
                },
            )
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                attempt_id=attempt.id,
                event_type="run.stage_changed",
                stage=job.stage,
                data={"stage": job.stage},
            )
            session.commit()

    def _load_provider_result(self, job_id: str, attempt_id: str) -> ProviderResult:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            attempt = session.get(GenerationAttempt, attempt_id)
            project = session.get(Project, job.project_id) if job else None
            if job is None or attempt is None or project is None or not attempt.output_path:
                raise ServiceError(409, "persisted provider output is incomplete")
            path = safe_join(Path(project.root_path), attempt.output_path)
            if not path.is_file() or sha256_file(path) != attempt.output_hash:
                raise ServiceError(409, "persisted provider output hash is invalid")
            value: Any = json.loads(path.read_text("utf-8")) if job.task_kind == TaskKind.TEXT.value else path.read_bytes()
            return ProviderResult(value=value, request_id=attempt.request_id)

    def _finish_attempt_error(self, attempt_id: str, error: ProviderError) -> None:
        with self.sessions() as session:
            attempt = session.get(GenerationAttempt, attempt_id)
            job = session.get(GenerationJob, attempt.job_id) if attempt else None
            if attempt is None:
                return
            attempt.status = GenerationStatus.FAILED.value
            attempt.phase = "failed"
            attempt.request_id = error.request_id
            attempt.error_category = error.category.value
            attempt.error_message = str(error)
            attempt.completed_at = utcnow()
            if job:
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=str(job.request_json.get("asset_id")),
                    attempt_id=attempt.id,
                    event_type="provider.call_failed",
                    stage=job.stage,
                    data={"category": error.category.value, "message": str(error)},
                )
            session.commit()

    async def _stage_and_complete(
        self,
        *,
        job_id: str,
        lease_token: str,
        attempt_id: str,
        attempt_number: int,
        result: ProviderResult,
        schema: dict[str, Any] | None,
    ) -> None:
        revision_id, verdict, checks, rendition_path = await asyncio.to_thread(
            self._stage_result,
            job_id,
            lease_token,
            attempt_id,
            attempt_number,
            result,
            schema,
        )
        if rendition_path:
            await asyncio.to_thread(self._record_job_evidence, job_id, revision_id, rendition_path)
            lock = self._evidence_locks.setdefault(
                self._plan_id(job_id), asyncio.Lock()
            )
            async with lock:
                await asyncio.to_thread(self._refresh_contact_sheets, self._plan_id(job_id))
        self._complete_job(
            job_id,
            attempt_id,
            revision_id,
            qa_verdict=verdict,
            qa_checks=checks,
        )

    def _plan_id(self, job_id: str) -> str:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                raise ServiceError(404, "generation job not found")
            return job.plan_id

    @staticmethod
    def _normalized_info(path: Path) -> dict[str, Any]:
        with Image.open(path) as image:
            image.load()
            width, height = image.size
        return {
            "width": width,
            "height": height,
            "byte_size": path.stat().st_size,
            "sha256": sha256_file(path),
            "media_type": "image/webp",
            "color_key_removed": None,
        }

    def _stage_result(
        self,
        job_id: str,
        lease_token: str,
        attempt_id: str,
        attempt_number: int,
        result: ProviderResult,
        schema: dict[str, Any] | None,
    ) -> tuple[str, str | None, list[dict[str, Any]], str | None]:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            attempt = session.get(GenerationAttempt, attempt_id)
            if job is None or attempt is None or job.lease_token != lease_token:
                raise ServiceError(409, "job lease was lost before staging")
            project = session.get(Project, job.project_id)
            profile = session.get(ProviderProfile, job.provider_profile_id)
            asset = session.get(Asset, str(job.request_json["asset_id"]))
            if project is None or profile is None or asset is None or not attempt.output_path:
                raise ServiceError(422, "job references missing staging records")
            request = job.resolved_request_json or job.request_json
            provider_snapshot = {
                **(job.provider_snapshot_json or {}),
                "profile_id": profile.id,
                "model": request.get("model"),
                "request_id": result.request_id,
                "attempt_id": attempt.id,
                "idempotency_key": attempt.idempotency_key,
            }
            existing = next(
                (
                    revision
                    for revision in session.scalars(
                        select(AssetRevision).where(AssetRevision.asset_id == asset.id)
                    ).all()
                    if revision.provider_snapshot.get("attempt_id") == attempt.id
                ),
                None,
            )
            job.status = GenerationStatus.HARD_QA.value
            job.stage = RunStage.HARD_QA.value
            job.progress = 0.55
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=asset.id,
                attempt_id=attempt.id,
                event_type="run.stage_changed",
                stage=job.stage,
                data={"stage": job.stage},
            )
            session.commit()

            if job.task_kind == TaskKind.TEXT.value:
                revision = existing or create_revision(
                    session,
                    RevisionCreate(
                        asset_id=asset.id,
                        format=RevisionFormat.JSON,
                        content=result.value,
                        prompt_recipe=request.get("prompt_recipe"),
                        provider_snapshot=provider_snapshot,
                    ),
                )
                job.result_revision_id = revision.id
                attempt.result_revision_id = revision.id
                attempt.phase = "staged"
                session.commit()
                return revision.id, None, [], None

            if not isinstance(result.value, bytes):
                raise ServiceError(422, "image provider did not return bytes")
            store = ProjectStore(project.root_path)
            source = safe_join(store.root, attempt.output_path)
            attempt_dir = store.candidate_dir(job.id) / f"attempt-{attempt_number}"
            normalized = attempt_dir / "normalized.webp"
            if normalized.exists():
                info = self._normalized_info(normalized)
            else:
                info = normalize_image(
                    result.value,
                    normalized,
                    expected_width=request.get("width"),
                    expected_height=request.get("height"),
                    transparent=bool(request.get("transparent")),
                )
            rendition_data = {
                "media_type": info["media_type"],
                "source_path": relative_to_root(store.root, source),
                "normalized_path": relative_to_root(store.root, normalized),
                "target_path": request.get("target_path"),
                "sha256": info["sha256"],
                "width": info["width"],
                "height": info["height"],
                "byte_size": info["byte_size"],
            }
            revision = existing or create_revision(
                session,
                RevisionCreate(
                    asset_id=asset.id,
                    format=RevisionFormat.MEDIA,
                    content={
                        "prompt": request["prompt"],
                        "width": info["width"],
                        "height": info["height"],
                        "color_key_removed": info["color_key_removed"],
                        "upstream_outputs": request.get("upstream_outputs", []),
                        "rendition": rendition_data,
                    },
                    provider_snapshot=provider_snapshot,
                ),
            )
            rendition = session.scalar(
                select(Rendition).where(Rendition.revision_id == revision.id).limit(1)
            )
            if rendition is None:
                rendition = Rendition(
                    id=stable_id("rendition", revision.id, info["sha256"]),
                    revision_id=revision.id,
                    **rendition_data,
                )
                session.add(rendition)
                session.flush()
                session.commit()
            qa = session.scalar(
                select(QARun)
                .where(QARun.rendition_id == rendition.id)
                .order_by(QARun.created_at.desc())
                .limit(1)
            )
            if qa is None:
                qa = run_qa(
                    session,
                    QARunCreate(
                        rendition_id=rendition.id,
                        expected_width=request.get("width"),
                        expected_height=request.get("height"),
                        require_alpha=bool(request.get("transparent")),
                        max_bytes=request.get("max_bytes"),
                    ),
                )
            job.result_revision_id = revision.id
            attempt.result_revision_id = revision.id
            attempt.phase = "staged"
            session.commit()
            return revision.id, qa.verdict, list(qa.checks_json), rendition.normalized_path or rendition.source_path

    def _add_evidence_file(
        self,
        session: Session,
        *,
        job: GenerationJob,
        revision_id: str | None,
        kind: str,
        label: str,
        path: str | None,
        sha256: str | None,
        byte_size: int | None,
        metadata: dict[str, Any],
    ) -> RunEvidence:
        identifier = stable_id(
            "evidence",
            job.plan_id,
            job.id,
            revision_id or "none",
            kind,
        )
        evidence = session.get(RunEvidence, identifier)
        if evidence is None:
            evidence = RunEvidence(
                id=identifier,
                project_id=job.project_id,
                plan_id=job.plan_id,
                job_id=job.id,
                revision_id=revision_id,
                kind=kind,
                label=label,
            )
            session.add(evidence)
        evidence.path = path
        evidence.media_type = "image/webp" if path else None
        evidence.sha256 = sha256
        evidence.byte_size = byte_size
        evidence.metadata_json = metadata
        session.flush()
        return evidence

    def _record_job_evidence(self, job_id: str, revision_id: str, rendition_path: str) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            project = session.get(Project, job.project_id) if job else None
            if job is None or project is None:
                raise ServiceError(404, "job evidence context is missing")
            store = ProjectStore(project.root_path)
            candidate = safe_join(store.root, rendition_path)
            candidate_evidence = self._add_evidence_file(
                session,
                job=job,
                revision_id=revision_id,
                kind="candidate",
                label="当前候选",
                path=rendition_path,
                sha256=sha256_file(candidate),
                byte_size=candidate.stat().st_size,
                metadata={},
            )
            reference_path = (job.resolved_request_json or {}).get("reference_path")
            created = [candidate_evidence]
            if reference_path:
                reference = safe_join(store.root, str(reference_path))
                if reference.is_file():
                    destination = store.workspace / "runs" / job.plan_id / "jobs" / job.id / "evidence" / revision_id
                    for item in make_comparison_bundle(reference, candidate, destination):
                        created.append(
                            self._add_evidence_file(
                                session,
                                job=job,
                                revision_id=revision_id,
                                kind=item.kind,
                                label=item.label,
                                path=relative_to_root(store.root, item.path),
                                sha256=item.sha256,
                                byte_size=item.byte_size,
                                metadata=item.metadata,
                            )
                        )
            else:
                created.append(
                    self._add_evidence_file(
                        session,
                        job=job,
                        revision_id=revision_id,
                        kind="comparison_unavailable",
                        label="无参考图，跳过对比证据",
                        path=None,
                        sha256=None,
                        byte_size=None,
                        metadata={"reason": "task has no reference input"},
                    )
                )
            for evidence in created:
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=str(job.request_json.get("asset_id")),
                    event_type="artifact.created",
                    stage=job.stage,
                    data={
                        "evidence_id": evidence.id,
                        "kind": evidence.kind,
                        "path": evidence.path,
                        "sha256": evidence.sha256,
                    },
                )
            session.commit()

    @staticmethod
    def _task_batches(tasks: list[dict[str, Any]]) -> list[list[str]]:
        remaining = {str(task["id"]): task for task in tasks}
        completed: set[str] = set()
        batches: list[list[str]] = []
        while remaining:
            ready = sorted(
                task_id
                for task_id, task in remaining.items()
                if set(task.get("depends_on", [])) <= completed
            )
            if not ready:
                break
            for offset in range(0, len(ready), 6):
                batch = ready[offset : offset + 6]
                batches.append(batch)
            for task_id in ready:
                completed.add(task_id)
                remaining.pop(task_id)
        return batches

    def _refresh_contact_sheets(self, plan_id: str) -> None:
        with self.sessions() as session:
            plan = session.get(GenerationPlan, plan_id)
            project = session.get(Project, plan.project_id) if plan else None
            if plan is None or project is None:
                return
            store = ProjectStore(project.root_path)
            jobs = list(
                session.scalars(select(GenerationJob).where(GenerationJob.plan_id == plan.id)).all()
            )
            by_task = {job.task_id: job for job in jobs}
            for batch_index, task_ids in enumerate(self._task_batches(plan.tasks_json), start=1):
                items: list[tuple[str, Path]] = []
                owner_job: GenerationJob | None = None
                for task_id in task_ids:
                    job = by_task.get(task_id)
                    if job is None or not job.result_revision_id:
                        continue
                    rendition = session.scalar(
                        select(Rendition).where(Rendition.revision_id == job.result_revision_id).limit(1)
                    )
                    if rendition is None:
                        continue
                    path = safe_join(store.root, rendition.normalized_path or rendition.source_path)
                    if path.is_file():
                        items.append((task_id, path))
                        owner_job = owner_job or job
                if not items or owner_job is None:
                    continue
                destination = store.workspace / "runs" / plan.id / "evidence" / "contact-sheets"
                for item in make_contact_sheets(items, destination, batch_index=batch_index):
                    identifier = stable_id("evidence", plan.id, f"batch-{batch_index}", item.kind)
                    evidence = session.get(RunEvidence, identifier)
                    if evidence is None:
                        evidence = RunEvidence(
                            id=identifier,
                            project_id=plan.project_id,
                            plan_id=plan.id,
                            job_id=None,
                            revision_id=None,
                            kind=item.kind,
                            label=item.label,
                        )
                        session.add(evidence)
                    evidence.path = relative_to_root(store.root, item.path)
                    evidence.media_type = "image/webp"
                    evidence.sha256 = item.sha256
                    evidence.byte_size = item.byte_size
                    evidence.metadata_json = {**item.metadata, "task_ids": task_ids}
                    session.flush()
                    record_run_event(
                        session,
                        plan_id=plan.id,
                        project_id=plan.project_id,
                        event_type="artifact.created",
                        data={
                            "evidence_id": evidence.id,
                            "kind": evidence.kind,
                            "path": evidence.path,
                            "sha256": evidence.sha256,
                        },
                    )
            session.commit()

    def _complete_job(
        self,
        job_id: str,
        attempt_id: str,
        revision_id: str,
        *,
        qa_verdict: str | None,
        qa_checks: list[dict[str, Any]],
    ) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            attempt = session.get(GenerationAttempt, attempt_id)
            if job is None or attempt is None:
                return
            plan = session.get(GenerationPlan, job.plan_id)
            asset = session.get(Asset, str(job.request_json["asset_id"]))
            action = (
                session.get(RemediationAction, job.pending_action_id)
                if job.pending_action_id
                else None
            )
            job.result_revision_id = revision_id
            attempt.result_revision_id = revision_id
            attempt.phase = "succeeded"
            attempt.status = GenerationStatus.SUCCEEDED.value
            attempt.completed_at = attempt.completed_at or utcnow()
            if action and action.expected_additional_calls:
                action.actual_additional_calls = 1
            if job.cancel_requested:
                job.status = GenerationStatus.CANCELLED.value
                job.error_category = ErrorCategory.CANCELLED.value
                job.error_message = "candidate was staged after cancellation"
            elif qa_verdict == QAVerdict.FAIL.value:
                findings = findings_from_qa(
                    session,
                    job=job,
                    revision_id=revision_id,
                    verdict=qa_verdict,
                    checks=qa_checks,
                )
                job.status = GenerationStatus.AWAITING_USER.value
                job.stage = RunStage.HARD_QA.value
                job.error_category = ErrorCategory.VALIDATION.value
                job.error_message = "hard QA found blocking evidence; choose a remediation action"
                for finding in findings:
                    record_run_event(
                        session,
                        plan_id=job.plan_id,
                        project_id=job.project_id,
                        job_id=job.id,
                        asset_id=asset.id if asset else None,
                        attempt_id=attempt.id,
                        event_type="finding.created",
                        stage=job.stage,
                        data={
                            "finding_id": finding.id,
                            "code": finding.code,
                            "occurrence": finding.occurrence,
                            "suggested_action": finding.suggested_action,
                        },
                    )
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=asset.id if asset else None,
                    attempt_id=attempt.id,
                    event_type="run.awaiting_user",
                    stage=job.stage,
                    data={"reason": job.error_message},
                )
                if action:
                    action.status = RemediationStatus.FAILED.value
                    action.completed_at = utcnow()
            else:
                findings_from_qa(
                    session,
                    job=job,
                    revision_id=revision_id,
                    verdict=qa_verdict or QAVerdict.PASS.value,
                    checks=qa_checks,
                )
                job.status = GenerationStatus.SEMANTIC_QA.value
                job.stage = RunStage.SEMANTIC_QA.value
                job.progress = 0.85
                semantic_event = record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=asset.id if asset else None,
                    attempt_id=attempt.id,
                    event_type="run.stage_changed",
                    stage=job.stage,
                    data={"stage": job.stage, "mode": "manual-capable deterministic evidence"},
                )
                job.status = GenerationStatus.CANDIDATE_READY.value
                job.stage = RunStage.CANDIDATE_READY.value
                job.progress = 1.0
                job.error_category = None
                job.error_message = None
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=asset.id if asset else None,
                    attempt_id=attempt.id,
                    event_type="run.stage_changed",
                    stage=job.stage,
                    causation_id=semantic_event.id,
                    data={"stage": job.stage},
                )
                if action:
                    action.status = RemediationStatus.COMPLETED.value
                    action.completed_at = utcnow()
            job.pending_action_id = None
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.updated_at = utcnow()
            if asset:
                asset.generation_status = job.status
                project = session.get(Project, asset.project_id)
                if project:
                    ProjectStore(project.root_path).write_asset(asset_descriptor(asset))
            if plan:
                refresh_plan_status(session, plan)
            session.commit()

    def _execute_tool_remediation(
        self,
        job_id: str,
        lease_token: str,
        action_id: str,
    ) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            action = session.get(RemediationAction, action_id)
            plan = session.get(GenerationPlan, job.plan_id) if job else None
            project = session.get(Project, job.project_id) if job else None
            parent_revision = (
                session.get(AssetRevision, job.result_revision_id)
                if job and job.result_revision_id
                else None
            )
            parent_rendition = (
                session.scalar(
                    select(Rendition).where(Rendition.revision_id == parent_revision.id).limit(1)
                )
                if parent_revision
                else None
            )
            if (
                job is None
                or job.lease_token != lease_token
                or action is None
                or plan is None
                or project is None
                or parent_revision is None
                or parent_rendition is None
            ):
                raise ServiceError(409, "tool remediation context is stale or incomplete")
            if current_job_input_hash(session, job) != action.input_hash:
                action.status = RemediationStatus.REJECTED.value
                action.completed_at = utcnow()
                session.commit()
                raise ServiceError(409, "tool remediation input changed after the action was accepted")
            number = job.attempt_count + 1
            attempt = GenerationAttempt(
                id=new_id(),
                job_id=job.id,
                number=number,
                status=GenerationStatus.RUNNING.value,
                phase="worker_running",
                purpose=RemediationKind.TOOL_REPAIR.value,
                idempotency_key=stable_id("worker", job.id, action.id, action.input_hash),
                request_hash=action.input_hash,
                request_json={
                    "action_id": action.id,
                    "strategy": action.strategy,
                    "parameters": action.parameters_json,
                },
                billable=False,
            )
            job.attempt_count = number
            job.status = GenerationStatus.REMEDIATING.value
            job.stage = RunStage.HARD_QA.value
            action.status = RemediationStatus.RUNNING.value
            session.add(attempt)
            session.flush()
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                attempt_id=attempt.id,
                event_type="worker.started",
                stage=job.stage,
                data={"strategy": action.strategy, "worker_version": "m2.1"},
            )
            session.commit()

            store = ProjectStore(project.root_path)
            source_path = safe_join(
                store.root,
                parent_rendition.normalized_path or parent_rendition.source_path,
            )
            output = store.candidate_dir(job.id) / f"remediation-{action.id}" / f"{action.strategy}.webp"
            parameters = dict(action.parameters_json)
            parameters.setdefault("width", parent_rendition.width)
            parameters.setdefault("height", parent_rendition.height)
            parameters.setdefault("transparent", bool(job.request_json.get("transparent")))
            info = apply_media_worker(
                source_path,
                output,
                strategy=action.strategy,
                parameters=parameters,
            )
            rendition_data = {
                "media_type": info["media_type"],
                "source_path": parent_rendition.source_path,
                "normalized_path": relative_to_root(store.root, output),
                "target_path": parent_rendition.target_path,
                "sha256": info["sha256"],
                "width": info["width"],
                "height": info["height"],
                "byte_size": info["byte_size"],
            }
            content = dict(parent_revision.content_json) if isinstance(parent_revision.content_json, dict) else {}
            content["rendition"] = rendition_data
            content["remediation"] = {
                "action_id": action.id,
                "strategy": action.strategy,
                "parameters": parameters,
                "parent_revision_id": parent_revision.id,
            }
            asset = session.get(Asset, parent_revision.asset_id)
            existing = next(
                (
                    revision
                    for revision in session.scalars(
                        select(AssetRevision).where(AssetRevision.asset_id == parent_revision.asset_id)
                    ).all()
                    if revision.provider_snapshot.get("remediation_action_id") == action.id
                ),
                None,
            )
            revision = existing or create_revision(
                session,
                RevisionCreate(
                    asset_id=parent_revision.asset_id,
                    format=RevisionFormat.MEDIA,
                    content=content,
                    parent_revision_id=parent_revision.id,
                    input_hash=action.input_hash,
                    provider_snapshot={
                        **parent_revision.provider_snapshot,
                        "producer": "media_worker",
                        "worker": action.strategy,
                        "worker_version": info["worker_version"],
                        "remediation_action_id": action.id,
                    },
                ),
            )
            rendition = session.scalar(
                select(Rendition).where(Rendition.revision_id == revision.id).limit(1)
            )
            if rendition is None:
                rendition = Rendition(
                    id=stable_id("rendition", revision.id, info["sha256"]),
                    revision_id=revision.id,
                    **rendition_data,
                )
                session.add(rendition)
                session.flush()
                session.commit()
            qa = run_qa(
                session,
                QARunCreate(
                    rendition_id=rendition.id,
                    expected_width=job.request_json.get("width"),
                    expected_height=job.request_json.get("height"),
                    require_alpha=bool(job.request_json.get("transparent")),
                    max_bytes=job.request_json.get("max_bytes"),
                ),
            )
            attempt.output_path = rendition_data["normalized_path"]
            attempt.output_hash = info["sha256"]
            attempt.result_revision_id = revision.id
            attempt.phase = "staged"
            attempt.status = GenerationStatus.SUCCEEDED.value
            attempt.completed_at = utcnow()
            job.result_revision_id = revision.id
            session.commit()
        self._record_job_evidence(job_id, revision.id, rendition_data["normalized_path"])
        self._refresh_contact_sheets(plan.id)
        self._complete_job(
            job_id,
            attempt.id,
            revision.id,
            qa_verdict=qa.verdict,
            qa_checks=list(qa.checks_json),
        )

    def _pause_provider_queue(
        self,
        job_id: str,
        target_status: str,
        error: ProviderError,
    ) -> None:
        with self.sessions() as session:
            source = session.get(GenerationJob, job_id)
            if source is None:
                return
            jobs = list(
                session.scalars(
                    select(GenerationJob).where(
                        GenerationJob.provider_profile_id == source.provider_profile_id,
                        GenerationJob.status.in_(
                            {
                                GenerationStatus.QUEUED.value,
                                GenerationStatus.RUNNING.value,
                                GenerationStatus.OUTPUT_RECEIVED.value,
                            }
                        ),
                    )
                ).all()
            )
            for job in jobs:
                job.status = target_status
                job.error_category = error.category.value
                job.error_message = str(error)
                job.lease_owner = None
                job.lease_token = None
                job.lease_expires_at = None
                job.heartbeat_at = None
                job.updated_at = utcnow()
                record_run_event(
                    session,
                    plan_id=job.plan_id,
                    project_id=job.project_id,
                    job_id=job.id,
                    asset_id=str(job.request_json.get("asset_id")),
                    event_type=(
                        "run.awaiting_user"
                        if target_status == GenerationStatus.AWAITING_USER.value
                        else "provider.credentials_locked"
                    ),
                    stage=job.stage,
                    data={"category": error.category.value, "message": str(error)},
                )
                plan = session.get(GenerationPlan, job.plan_id)
                if plan:
                    refresh_plan_status(session, plan)
            session.commit()

    def _set_awaiting_user(
        self,
        job_id: str,
        message: str,
        *,
        category: ErrorCategory = ErrorCategory.UNKNOWN,
    ) -> None:
        with self.sessions() as session:
            job = session.get(GenerationJob, job_id)
            if job is None:
                return
            job.status = GenerationStatus.AWAITING_USER.value
            job.error_category = category.value
            job.error_message = message
            job.lease_owner = None
            job.lease_token = None
            job.lease_expires_at = None
            job.heartbeat_at = None
            job.updated_at = utcnow()
            action = (
                session.get(RemediationAction, job.pending_action_id)
                if job.pending_action_id
                else None
            )
            if action and action.status not in {
                RemediationStatus.COMPLETED.value,
                RemediationStatus.REJECTED.value,
            }:
                action.status = RemediationStatus.FAILED.value
                action.completed_at = utcnow()
            if action:
                job.pending_action_id = None
            record_run_event(
                session,
                plan_id=job.plan_id,
                project_id=job.project_id,
                job_id=job.id,
                asset_id=str(job.request_json.get("asset_id")),
                event_type="run.awaiting_user",
                stage=job.stage,
                data={"category": category.value, "message": message},
            )
            plan = session.get(GenerationPlan, job.plan_id)
            if plan:
                refresh_plan_status(session, plan)
            session.commit()
