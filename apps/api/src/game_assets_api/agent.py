"""Codex supervision service.

This module is intentionally a thin, fail-closed bridge around the deterministic
Controller.  It builds a redacted read-only context, validates adapter output,
and either submits one of the existing remediation actions or records a review-
only ChangeSet.  It never invokes Git and never changes review, release, delivery,
or SQLite facts directly.
"""

from __future__ import annotations

import inspect
import re
import shlex
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .codex_adapter import (
    CodexAdapter,
    CodexAdapterError,
    CodexProtocolError,
    CodexUnavailable,
)
from .domain import (
    AgentProposal,
    AgentSessionStatus,
    RemediationCreate,
    RemediationKind,
)
from .models import (
    AgentEvent,
    AgentSession,
    Artifact,
    Asset,
    AssetRevision,
    ChangeSet,
    GenerationAttempt,
    GenerationJob,
    GenerationPlan,
    ProductionFinding,
    Project,
    RunEvidence,
    new_id,
    utcnow,
)
from .production import create_remediation, current_job_input_hash, record_run_event
from .services import ServiceError
from .storage import (
    ProjectStore,
    atomic_write_json,
    canonical_json,
    relative_to_root,
    safe_join,
    sha256_bytes,
    stable_id,
    StorageError,
)


ALLOWED_ACTIONS = frozenset(item.value for item in RemediationKind)
AGENT_SCHEMA_VERSION = 1
LOW_CONFIDENCE_THRESHOLD = 0.55

# Paths in a proposed patch are relative to the target repository.  These names
# are rejected even when nested below an otherwise harmless directory.
PROTECTED_PATH_PARTS = frozenset(
    {
        ".git",
        "gams-lock.json",
        "index.sqlite3",
        "index.sqlite3-shm",
        "index.sqlite3-wal",
    }
)
PROTECTED_PREFIXES = (
    "approved/",
    "catalog/",
    "history/",
    "production/sources/",
    "releases/",
    "delivery/",
    "deliveries/",
    "local-state/",
)
AUTO_WRITE_PREFIXES = (
    "production/prompt-recipes/",
    "production/parameters/",
    "production/postprocess/",
    "production/agent/",
)


def _redact(value: Any) -> Any:
    """Remove credential-shaped values from a context recursively."""

    if isinstance(value, str):
        value = re.sub(r"\b(?:sk|rk)-[A-Za-z0-9_-]{8,}", "[redacted]", value)
        value = re.sub(
            r"(?i)\b(?:bearer|authorization)\s+[A-Za-z0-9._~+/=-]{8,}",
            "[redacted]",
            value,
        )
        return value
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(
                token in lowered
                for token in (
                    "api_key",
                    "apikey",
                    "secret",
                    "password",
                    "token",
                    "credential",
                    "authorization",
                    "private_key",
                    "access_key",
                )
            ):
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = _redact(item)
        return result
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _row_dict(row: Any, fields: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        value = getattr(row, field, None)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        result[field] = value
    return result


def build_context_package(
    session: Session,
    job: GenerationJob,
    *,
    finding_ids: Iterable[str] = (),
) -> tuple[dict[str, Any], str]:
    """Build a deterministic, redacted package of read-only production inputs."""

    plan = session.get(GenerationPlan, job.plan_id)
    project = session.get(Project, job.project_id)
    asset = session.get(Asset, str(job.request_json.get("asset_id")))
    revision = session.get(AssetRevision, job.result_revision_id) if job.result_revision_id else None
    attempts = list(
        session.scalars(
            select(GenerationAttempt)
            .where(GenerationAttempt.job_id == job.id)
            .order_by(GenerationAttempt.number)
        ).all()
    )
    finding_filter = {str(value) for value in finding_ids if value}
    finding_statement = select(ProductionFinding).where(ProductionFinding.job_id == job.id)
    findings = list(session.scalars(finding_statement.order_by(ProductionFinding.created_at)).all())
    if finding_filter:
        findings = [finding for finding in findings if finding.id in finding_filter]
    evidence = list(
        session.scalars(
            select(RunEvidence)
            .where(
                RunEvidence.plan_id == job.plan_id,
                (RunEvidence.job_id == job.id) | (RunEvidence.job_id.is_(None)),
            )
            .order_by(RunEvidence.created_at, RunEvidence.id)
        ).all()
    )
    package: dict[str, Any] = {
        "context_version": 1,
        "read_only": True,
        "project": (
            _row_dict(project, ("id", "name", "default_language")) if project else None
        ),
        "plan": (
            _row_dict(
                plan,
                (
                    "id",
                    "name",
                    "status",
                    "tasks_json",
                    "estimated_calls",
                    "estimated_cost",
                    "extra_call_budget",
                    "extra_calls_used",
                    "max_paid_remediation_rounds",
                    "max_transport_retries",
                    "max_concurrency",
                ),
            )
            if plan
            else None
        ),
        "job": _redact(
            _row_dict(
                job,
                (
                    "id",
                    "plan_id",
                    "task_id",
                    "task_kind",
                    "status",
                    "stage",
                    "attempt_count",
                    "paid_remediation_rounds",
                    "request_json",
                    "resolved_request_json",
                    "provider_snapshot_json",
                    "result_revision_id",
                    "error_category",
                    "error_message",
                ),
            )
        ),
        "asset": (
            _redact(
                _row_dict(
                    asset,
                    ("id", "key", "kind", "subtype", "title", "tags", "metadata_json"),
                )
            )
            if asset
            else None
        ),
        "revision": (
            _redact(
                _row_dict(
                    revision,
                    (
                        "id",
                        "sequence",
                        "format",
                        "content_json",
                        "content_hash",
                        "input_hash",
                        "style_revision",
                        "prompt_recipe",
                        "provider_snapshot",
                        "review_status",
                    ),
                )
            )
            if revision
            else None
        ),
        "attempts": [
            _redact(
                _row_dict(
                    attempt,
                    (
                        "id",
                        "number",
                        "status",
                        "phase",
                        "purpose",
                        "request_hash",
                        "request_json",
                        "request_id",
                        "error_category",
                        "error_message",
                        "output_hash",
                        "result_revision_id",
                        "billable",
                        "estimated_cost",
                    ),
                )
            )
            for attempt in attempts
        ],
        "findings": [
            _redact(
                _row_dict(
                    finding,
                    (
                        "id",
                        "code",
                        "severity",
                        "blocking",
                        "evidence_json",
                        "confidence",
                        "suggested_action",
                        "occurrence",
                        "resolved_at",
                        "revision_id",
                    ),
                )
            )
            for finding in findings
        ],
        "evidence": [
            _redact(
                _row_dict(
                    item,
                    (
                        "id",
                        "job_id",
                        "revision_id",
                        "kind",
                        "label",
                        "path",
                        "media_type",
                        "sha256",
                        "byte_size",
                        "metadata_json",
                    ),
                )
            )
            for item in evidence
        ],
        "visual_evidence": {
            "contact_sheets": [item.id for item in evidence if "contact" in item.kind],
            "diff_images": [item.id for item in evidence if "diff" in item.kind],
        },
        "constraints": {
            "allowed_actions": sorted(ALLOWED_ACTIONS),
            "no_credentials": True,
            "no_sqlite_or_git": True,
            "input_hash": current_job_input_hash(session, job),
        },
    }
    package = _redact(package)
    return package, sha256_bytes(canonical_json(package))


def record_agent_event(
    session: Session,
    agent_session: AgentSession,
    event_type: str,
    *,
    data: dict[str, Any] | None = None,
    turn_id: str | None = None,
) -> AgentEvent:
    event = AgentEvent(
        id=new_id(),
        session_id=agent_session.id,
        project_id=agent_session.project_id,
        plan_id=agent_session.plan_id,
        job_id=agent_session.job_id,
        asset_id=agent_session.asset_id,
        event_type=event_type,
        thread_id=agent_session.thread_id,
        turn_id=turn_id,
        data_json=_redact(data or {}),
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    # Project the Agent event into the existing run stream when a Plan exists so
    # the Run Inspector can show the same causal timeline without exposing SDK
    # private event formats.
    if agent_session.plan_id:
        record_run_event(
            session,
            plan_id=agent_session.plan_id,
            project_id=agent_session.project_id,
            job_id=agent_session.job_id,
            asset_id=agent_session.asset_id,
            event_type=event_type,
            data={
                "agent_session_id": agent_session.id,
                "thread_id": agent_session.thread_id,
                "turn_id": turn_id,
                **_redact(data or {}),
            },
        )
    return event


def _persist_agent_audit(session: Session, agent_session: AgentSession) -> None:
    """Mirror audit rows into Project/history so the index can be rebuilt."""

    project = session.get(Project, agent_session.project_id)
    if project is None:
        return
    store = ProjectStore(project.root_path)
    session_path = safe_join(store.root, f"history/agent/sessions/{agent_session.id}.json")
    atomic_write_json(
        session_path,
        {
            "format_version": 1,
            "id": agent_session.id,
            "project_id": agent_session.project_id,
            "plan_id": agent_session.plan_id,
            "job_id": agent_session.job_id,
            "asset_id": agent_session.asset_id,
            "thread_id": agent_session.thread_id,
            "adapter": agent_session.adapter,
            "adapter_version": agent_session.adapter_version,
            "agent_model": agent_session.agent_model,
            "schema_version": agent_session.schema_version,
            "purpose": agent_session.purpose,
            "title": agent_session.title,
            "status": agent_session.status,
            "context_hash": agent_session.context_hash,
            "context_path": agent_session.context_path,
            "context": agent_session.context_json,
            "draft": agent_session.draft_json,
            "draft_hash": agent_session.draft_hash,
            "draft_version": agent_session.draft_version,
            "sandbox": agent_session.sandbox_json,
            "allowed_actions": agent_session.allowed_actions_json,
            "writable_allowlist": agent_session.writable_allowlist_json,
            "budget_limit": agent_session.budget_limit,
            "budget_used": agent_session.budget_used,
            "turn_count": agent_session.turn_count,
            "diagnostic_reason": agent_session.diagnostic_reason,
            "result": agent_session.result_json,
            "stop_reason": agent_session.stop_reason,
            "created_at": agent_session.created_at.isoformat(),
            "updated_at": agent_session.updated_at.isoformat(),
            "completed_at": agent_session.completed_at.isoformat() if agent_session.completed_at else None,
            "archived_at": agent_session.archived_at.isoformat() if agent_session.archived_at else None,
        },
    )
    events = session.scalars(
        select(AgentEvent)
        .where(AgentEvent.session_id == agent_session.id)
        .order_by(AgentEvent.sequence)
    ).all()
    for event in events:
        atomic_write_json(
            safe_join(store.root, f"history/agent/events/{agent_session.id}/{event.sequence}.json"),
            {
                "format_version": 1,
                "id": event.id,
                "sequence": event.sequence,
                "session_id": event.session_id,
                "project_id": event.project_id,
                "plan_id": event.plan_id,
                "job_id": event.job_id,
                "asset_id": event.asset_id,
                "event_type": event.event_type,
                "thread_id": event.thread_id,
                "turn_id": event.turn_id,
                "data": event.data_json,
                "created_at": event.created_at.isoformat(),
            },
        )
    for changeset in session.scalars(
        select(ChangeSet).where(ChangeSet.session_id == agent_session.id)
    ).all():
        atomic_write_json(
            safe_join(store.root, f"history/changesets/{changeset.id}.json"),
            {
                "format_version": 1,
                "id": changeset.id,
                "session_id": changeset.session_id,
                "project_id": changeset.project_id,
                "target_repository": changeset.target_repository,
                "baseline_commit": changeset.baseline_commit,
                "patch_path": changeset.patch_path,
                "patch_hash": changeset.patch_hash,
                "files": changeset.files_json,
                "validation_commands": changeset.validation_commands_json,
                "risk": changeset.risk,
                "summary": changeset.summary,
                "status": changeset.status,
                "decision_reason": changeset.decision_reason,
                "created_at": changeset.created_at.isoformat(),
                "decided_at": changeset.decided_at.isoformat() if changeset.decided_at else None,
            },
        )


def _safe_patch_path(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("ChangeSet file paths must be strings")
    path = value.replace("\\", "/").strip()
    if not path or path.startswith("/") or re.match(r"^[A-Za-z]:/", path):
        raise ValueError("ChangeSet paths must be relative")
    parts = [part for part in path.split("/") if part]
    if ".." in parts or any(part in PROTECTED_PATH_PARTS for part in parts):
        raise ValueError("ChangeSet path is outside the allowed patch boundary")
    normalized = "/".join(parts)
    if normalized in {"", "."}:
        raise ValueError("ChangeSet path must identify a file")
    if normalized.startswith(PROTECTED_PREFIXES):
        raise ValueError("ChangeSet cannot modify protected project facts")
    if normalized.lower().endswith((".sqlite", ".sqlite3", ".db")):
        raise ValueError("ChangeSet cannot modify a database")
    return normalized


def _commands(value: Any) -> list[list[str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("validation_commands must be a list")
    output: list[list[str]] = []
    for command in value:
        argv = shlex.split(command) if isinstance(command, str) else command
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            raise ValueError("validation commands must be non-empty argv lists")
        if any(token in {"git", "add", "commit", "push", "reset", "checkout"} for token in argv):
            raise ValueError("Git commands are never allowed in an Agent ChangeSet")
        if any(token in {";", "&&", "||", "|", ">", ">>"} for token in argv):
            raise ValueError("shell composition is not allowed in validation commands")
        output.append(list(argv))
    return output


def validate_changeset_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize a review-only patch proposal."""

    patch = parameters.get("patch", parameters.get("diff"))
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("propose_changeset requires a non-empty patch")
    if "\x00" in patch or re.search(
        r"(?i)(?:git\s+(?:add|commit|push)|gams-lock\.json|(?:^|[/\\])[^\s]+\.sqlite3?(?:\b|[/\\])|(?:^|[/\\])[^\s]+\.db\b)",
        patch,
    ):
        raise ValueError("patch contains a protected database, lock, or Git operation")
    # Validate paths in every common unified-diff header as well as the explicit
    # file list; otherwise a seemingly harmless file list could hide a traversal
    # in a rename/copy or ``diff --git`` header.
    for header in re.findall(r"^(?:---|\+\+\+)\s+([^\n]+)", patch, re.MULTILINE):
        path = header.split("\t", 1)[0].strip()
        if path == "/dev/null":
            continue
        if path.startswith(("a/", "b/")):
            path = path[2:]
        _safe_patch_path(path.split("\t", 1)[0])
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            try:
                tokens = shlex.split(line)
            except ValueError as exc:
                raise ValueError("invalid diff --git header") from exc
            if len(tokens) != 4:
                raise ValueError("invalid diff --git header")
            for path in tokens[2:]:
                if path == "/dev/null":
                    continue
                _safe_patch_path(path[2:] if path[:2] in {"a/", "b/"} else path)
        for prefix in ("rename from ", "rename to ", "copy from ", "copy to ", "Index: "):
            if line.startswith(prefix):
                _safe_patch_path(line[len(prefix) :].strip())
    raw_files = parameters.get("files", [])
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("propose_changeset requires affected files")
    files = sorted({_safe_patch_path(item) for item in raw_files})
    commands = _commands(parameters.get("validation_commands"))
    target = str(parameters.get("target_repository") or "project")
    if (
        "\n" in target
        or "\r" in target
        or len(target) > 240
        or target.startswith(("/", "\\"))
        or ".." in target.replace("\\", "/").split("/")
    ):
        raise ValueError("invalid ChangeSet target repository")
    baseline = parameters.get("baseline_commit")
    if baseline is not None and (not isinstance(baseline, str) or "\n" in baseline or len(baseline) > 240):
        raise ValueError("invalid ChangeSet baseline")
    return {
        "patch": patch,
        "files": files,
        "validation_commands": commands,
        "target_repository": target,
        "baseline_commit": baseline,
        "summary": str(parameters.get("summary") or "Agent proposed repository change")[:4_000],
        "risk": str(parameters.get("risk") or "Requires human review before application")[:4_000],
    }


def apply_whitelisted_writes(project: Project, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    """Apply explicitly supplied production-config files, never repository code.

    This is the only automatic file-write path available to an Agent.  It accepts
    complete file contents (not shell commands or an arbitrary patch) and records
    hashes for the caller's audit event.  Everything else must go through a
    pending ChangeSet.
    """

    raw = parameters.get("write_files", parameters.get("files_content"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("auto_apply requires a non-empty write_files object")
    store = ProjectStore(project.root_path)
    written: list[dict[str, Any]] = []
    from .storage import atomic_write_bytes

    with store.lock():
        pending: list[tuple[str, Path, str, str | None, str]] = []
        for raw_path, content in raw.items():
            path = _safe_patch_path(raw_path)
            if not path.startswith(AUTO_WRITE_PREFIXES):
                raise ValueError("automatic Agent writes are limited to production configuration")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 2_000_000:
                raise ValueError("automatic Agent file content is invalid or too large")
            destination = safe_join(store.root, path)
            previous_hash = sha256_bytes(destination.read_bytes()) if destination.is_file() else None
            new_hash = sha256_bytes(content.encode("utf-8"))
            pending.append((path, destination, content, previous_hash, new_hash))
        for path, destination, content, previous_hash, new_hash in pending:
            atomic_write_bytes(destination, content.encode("utf-8"))
            atomic_write_json(
                safe_join(store.root, f"history/agent/config/{new_id()}.json"),
                {
                    "format_version": 1,
                    "path": path,
                    "previous_hash": previous_hash,
                    "new_hash": new_hash,
                    "created_at": utcnow().isoformat(),
                },
            )
            written.append({"path": path, "sha256": new_hash, "previous_hash": previous_hash})
    return written


def _persist_proposed_findings(
    session: Session,
    job: GenerationJob,
    proposals: list[dict[str, Any]],
) -> list[ProductionFinding]:
    """Persist Agent findings as ordinary auditable ProductionFinding rows."""

    created: list[ProductionFinding] = []
    revision_id = job.result_revision_id
    for raw in proposals:
        if not isinstance(raw, dict):
            raise ValueError("agent finding must be an object")
        code = str(raw.get("code", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{1,159}", code):
            raise ValueError("agent finding code must use a stable lowercase namespace")
        severity = str(raw.get("severity", "warning"))
        if severity not in {"info", "warning", "error"}:
            raise ValueError("agent finding severity is invalid")
        confidence = float(raw.get("confidence", 0.0))
        if not 0 <= confidence <= 1:
            raise ValueError("agent finding confidence is invalid")
        evidence = raw.get("evidence", [])
        if not isinstance(evidence, list):
            raise ValueError("agent finding evidence must be a list")
        for item in evidence:
            if not isinstance(item, dict):
                raise ValueError("agent finding evidence items must be objects")
            evidence_id = item.get("evidence_id")
            if evidence_id:
                evidence_row = session.get(RunEvidence, str(evidence_id))
                if (
                    evidence_row is None
                    or evidence_row.plan_id != job.plan_id
                    or (evidence_row.job_id is not None and evidence_row.job_id != job.id)
                ):
                    raise ValueError("agent finding references an unknown Evidence")
            artifact_id = item.get("artifact_id")
            if artifact_id:
                artifact_row = session.get(Artifact, str(artifact_id))
                if artifact_row is None or artifact_row.project_id != job.project_id:
                    raise ValueError("agent finding references an unknown Artifact")
        previous = session.scalar(
            select(ProductionFinding)
            .where(ProductionFinding.job_id == job.id, ProductionFinding.code == code)
            .order_by(ProductionFinding.occurrence.desc())
            .limit(1)
        )
        occurrence = (previous.occurrence if previous else 0) + 1
        suggested_action = str(raw.get("suggested_action") or RemediationKind.AWAIT_USER.value)
        if suggested_action not in ALLOWED_ACTIONS:
            raise ValueError("agent finding suggested_action is not whitelisted")
        finding = ProductionFinding(
            id=stable_id(
                "finding",
                job.id,
                str(revision_id or "agent"),
                code,
                str(occurrence),
            ),
            project_id=job.project_id,
            plan_id=job.plan_id,
            job_id=job.id,
            revision_id=revision_id,
            code=code,
            severity=severity,
            blocking=bool(raw.get("blocking", severity == "error")),
            evidence_json=_redact(evidence),
            confidence=confidence,
            suggested_action=suggested_action,
            occurrence=occurrence,
        )
        session.add(finding)
        session.flush()
        created.append(finding)
        record_run_event(
            session,
            plan_id=job.plan_id,
            project_id=job.project_id,
            job_id=job.id,
            asset_id=str(job.request_json.get("asset_id")) if job.request_json.get("asset_id") else None,
            event_type="finding.created",
            stage=job.stage,
            data={
                "finding_id": finding.id,
                "code": code,
                "occurrence": occurrence,
                "source": "codex",
            },
        )
    return created


def _write_changeset(
    session: Session,
    agent_session: AgentSession,
    project: Project,
    parameters: dict[str, Any],
) -> ChangeSet:
    normalized = validate_changeset_parameters(parameters)
    identifier = new_id()
    store = ProjectStore(project.root_path)
    # ChangeSet patches are review artifacts, so keep the immutable patch in
    # Project history rather than the disposable generation workspace.
    patch_relative = f"history/changesets/{identifier}.patch"
    patch_path = safe_join(store.root, patch_relative)
    # A patch is intentionally kept in the durable ChangeSet area.  The helper
    # uses an atomic write rather than a shell or Git operation.
    from .storage import atomic_write_bytes

    with store.lock():
        atomic_write_json(
            safe_join(store.root, f"workspace/agent/changesets/{identifier}.json"),
            {
                "format_version": 1,
                "id": identifier,
                "project_id": project.id,
                "session_id": agent_session.id,
                "files": normalized["files"],
                "summary": normalized["summary"],
                "risk": normalized["risk"],
                "patch_hash": sha256_bytes(normalized["patch"].encode("utf-8")),
            },
        )
        atomic_write_bytes(patch_path, normalized["patch"].encode("utf-8"), immutable=True)
    changeset = ChangeSet(
        id=identifier,
        session_id=agent_session.id,
        project_id=project.id,
        target_repository=normalized["target_repository"],
        baseline_commit=normalized["baseline_commit"],
        patch_path=patch_relative,
        patch_hash=sha256_bytes(normalized["patch"].encode("utf-8")),
        files_json=normalized["files"],
        validation_commands_json=normalized["validation_commands"],
        risk=normalized["risk"],
        summary=normalized["summary"],
        status="pending_approval",
    )
    session.add(changeset)
    session.flush()
    return changeset


def _finish_manual(
    session: Session,
    agent_session: AgentSession,
    *,
    reason: str,
    event_type: str = "agent.awaiting_user",
    result: dict[str, Any] | None = None,
) -> AgentSession:
    safe_reason = str(_redact(reason))
    agent_session.status = AgentSessionStatus.AWAITING_USER.value
    agent_session.stop_reason = safe_reason
    agent_session.diagnostic_reason = safe_reason
    if result is not None:
        agent_session.result_json = {
            **(agent_session.result_json or {}),
            **_redact(result),
        }
    agent_session.completed_at = utcnow()
    agent_session.updated_at = utcnow()
    record_agent_event(session, agent_session, event_type, data={"reason": safe_reason, **(result or {})})
    _persist_agent_audit(session, agent_session)
    session.commit()
    return agent_session


async def diagnose_job(
    session: Session,
    job: GenerationJob,
    *,
    adapter: CodexAdapter,
    finding_ids: Iterable[str] = (),
    budget: int = 1,
    requested_reason: str | None = None,
) -> AgentSession:
    """Run one Codex turn and submit at most one validated Controller action."""

    from .services import require

    budget_is_valid = isinstance(budget, int) and 1 <= budget <= 20
    normalized_budget = int(budget) if isinstance(budget, int) else 0
    if normalized_budget < 0:
        normalized_budget = 0

    project = require(session, Project, job.project_id, "project")
    plan = require(session, GenerationPlan, job.plan_id, "generation plan")
    context, context_hash = build_context_package(session, job, finding_ids=finding_ids)
    session_id = new_id()
    store = ProjectStore(project.root_path)
    work_dir = store.workspace / "agent" / "sessions" / session_id
    work_dir.mkdir(parents=True, exist_ok=True)
    context_relative = relative_to_root(store.root, work_dir / "context.json")
    atomic_write_json(work_dir / "context.json", context, immutable=True)
    adapter_name = str(getattr(adapter, "name", adapter.__class__.__name__))
    adapter_version = str(getattr(adapter, "version", "unknown"))
    agent_session = AgentSession(
        id=session_id,
        project_id=project.id,
        plan_id=plan.id,
        job_id=job.id,
        asset_id=str(job.request_json.get("asset_id")) if job.request_json.get("asset_id") else None,
        adapter=adapter_name,
        adapter_version=adapter_version,
        schema_version=AGENT_SCHEMA_VERSION,
        status=AgentSessionStatus.RUNNING.value,
        context_hash=context_hash,
        context_path=context_relative,
        context_json=context,
        sandbox_json={"read_only": True, "work_dir": context_relative.rsplit("/", 1)[0]},
        allowed_actions_json=sorted(ALLOWED_ACTIONS),
        writable_allowlist_json=[
            "production/prompt-recipes/**",
            "production/parameters/**",
            "production/postprocess/**",
            "production/agent/**",
            "workspace/agent/**",
        ],
        budget_limit=normalized_budget,
        diagnostic_reason=requested_reason,
    )
    session.add(agent_session)
    session.flush()
    record_agent_event(
        session,
        agent_session,
        "agent.turn_started",
        data={"context_hash": context_hash, "budget_limit": budget, "budget_used": 0},
    )
    _persist_agent_audit(session, agent_session)
    session.commit()
    if not budget_is_valid:
        reason = (
            "agent.budget_exhausted: no Agent turn budget remains"
            if normalized_budget < 1
            else "agent.schema_invalid: Agent budget must be between 1 and 20"
        )
        return _finish_manual(session, agent_session, reason=reason)
    # A diagnosis turn consumes one Agent budget unit even when the external
    # adapter fails; this prevents silent retry loops from hiding cost or time.
    agent_session.budget_used = 1
    session.commit()
    try:
        try:
            raw = adapter.diagnose(context, budget=budget, work_dir=work_dir)
        except TypeError as exc:
            # Small test/local adapters often implement the minimal
            # ``diagnose(context, budget=...)`` protocol.  The production
            # adapter accepts ``work_dir``; retain compatibility without
            # treating an unrelated adapter exception as a valid response.
            if "work_dir" not in str(exc):
                raise
            raw = adapter.diagnose(context, budget=budget)  # type: ignore[call-arg]
        if inspect.isawaitable(raw):
            try:
                raw = await raw
            except TypeError as exc:
                if "work_dir" not in str(exc):
                    raise
                raw = adapter.diagnose(context, budget=budget)  # type: ignore[call-arg]
                if inspect.isawaitable(raw):
                    raw = await raw
    except CodexUnavailable as exc:
        return _finish_manual(session, agent_session, reason=f"codex.unavailable: {exc}", event_type="agent.unavailable")
    except (CodexProtocolError, CodexAdapterError) as exc:
        return _finish_manual(session, agent_session, reason=f"codex.adapter_error: {exc}")
    except Exception as exc:  # adapter failures must never escape into execution
        return _finish_manual(session, agent_session, reason=f"codex.adapter_error: {type(exc).__name__}")

    if not isinstance(raw, dict):
        return _finish_manual(session, agent_session, reason="agent.schema_invalid: response must be an object")
    thread_value = raw.get("thread_id")
    if thread_value is None and isinstance(raw.get("thread"), dict):
        thread_value = raw["thread"].get("id")
    agent_session.thread_id = str(thread_value) if thread_value else None
    adapter_events = raw.get("events", [])
    if isinstance(adapter_events, list):
        for adapter_event in adapter_events:
            if not isinstance(adapter_event, dict):
                continue
            event_thread = adapter_event.get("thread_id")
            event_turn = adapter_event.get("turn_id")
            if event_thread and not agent_session.thread_id:
                agent_session.thread_id = str(event_thread)
            record_agent_event(
                session,
                agent_session,
                "agent.adapter_event",
                data={
                    "type": str(adapter_event.get("type", "unknown")),
                    "payload": adapter_event,
                },
                turn_id=str(event_turn) if event_turn else None,
            )
    proposal_raw = raw.get("proposal", raw.get("result", raw))
    if not isinstance(proposal_raw, dict):
        return _finish_manual(session, agent_session, reason="agent.schema_invalid: proposal must be an object")
    try:
        proposal = AgentProposal.model_validate(proposal_raw)
    except ValidationError as exc:
        return _finish_manual(session, agent_session, reason=f"agent.schema_invalid: {exc.errors()[0].get('msg', 'invalid')}")
    if proposal.schema_version != AGENT_SCHEMA_VERSION:
        return _finish_manual(session, agent_session, reason="agent.schema_invalid: unsupported schema version")
    safe_proposal_reason = str(_redact(proposal.reason))
    if proposal.action not in ALLOWED_ACTIONS:
        return _finish_manual(session, agent_session, reason=f"action.unknown: {proposal.action}", event_type="action.rejected")
    current_hash = current_job_input_hash(session, job)
    if proposal.input_hash != current_hash:
        return _finish_manual(
            session,
            agent_session,
            reason="agent.input_stale: proposal input hash does not match the current Job",
            event_type="action.rejected",
            result={"expected_input_hash": current_hash, "provided_input_hash": proposal.input_hash},
        )
    if proposal.confidence < LOW_CONFIDENCE_THRESHOLD:
        return _finish_manual(
            session,
            agent_session,
            reason="agent.confidence_insufficient: human diagnosis required",
            result={"confidence": proposal.confidence},
        )
    try:
        proposed_findings = _persist_proposed_findings(session, job, proposal.findings)
    except (TypeError, ValueError) as exc:
        session.rollback()
        reloaded = session.get(AgentSession, agent_session.id)
        if reloaded is None:
            raise ServiceError(500, "agent session disappeared while validating findings") from exc
        return _finish_manual(
            session,
            reloaded,
            reason=f"agent.schema_invalid: finding proposal is invalid ({exc})",
        )
    if proposed_findings:
        proposal.finding_ids = list(
            dict.fromkeys(
                [*proposal.finding_ids]
                + [finding.id for finding in proposed_findings if finding.blocking]
            )
        )
        if any(finding.blocking for finding in proposed_findings):
            job.status = "awaiting_user"
            job.error_message = "Codex reported a blocking semantic Finding; choose a remediation action"
            job.updated_at = utcnow()
        if any(finding.blocking and finding.confidence < LOW_CONFIDENCE_THRESHOLD for finding in proposed_findings):
            return _finish_manual(
                session,
                agent_session,
                reason="agent.confidence_insufficient: blocking Finding needs human confirmation",
                result={"finding_ids": [finding.id for finding in proposed_findings]},
            )
    selected_findings = list(
        session.scalars(select(ProductionFinding).where(ProductionFinding.job_id == job.id)).all()
    )
    selected_ids = set(proposal.finding_ids)
    if selected_ids - {finding.id for finding in selected_findings}:
        return _finish_manual(session, agent_session, reason="action.rejected: unknown finding reference", event_type="action.rejected")
    selected_blocking = [
        finding
        for finding in selected_findings
        if finding.id in selected_ids and finding.blocking and finding.resolved_at is None
    ]
    if any(finding.occurrence >= 3 for finding in selected_blocking):
        return _finish_manual(
            session,
            agent_session,
            reason="agent.strategy_exhausted: the same blocking Finding occurred three times",
            event_type="action.rejected",
            result={"finding_ids": [finding.id for finding in selected_blocking]},
        )
    expected = 1 if proposal.action in {RemediationKind.REGENERATE.value, RemediationKind.IMAGE_EDIT.value} else 0
    if proposal.action == RemediationKind.PROPOSE_CHANGESET.value and proposal.expected_additional_calls != 0:
        return _finish_manual(
            session,
            agent_session,
            reason="action.rejected: ChangeSet proposals cannot reserve provider calls",
            event_type="action.rejected",
        )
    if proposal.action != RemediationKind.PROPOSE_CHANGESET.value and proposal.expected_additional_calls != expected:
        return _finish_manual(session, agent_session, reason="action.rejected: expected additional calls do not match action", event_type="action.rejected")
    record_agent_event(
        session,
        agent_session,
        "action.proposed",
        data={
            "action": proposal.action,
            "finding_ids": proposal.finding_ids,
            "confidence": proposal.confidence,
            "reason": safe_proposal_reason,
        },
        turn_id=proposal.turn_id,
    )
    agent_session.result_json = _redact(proposal.model_dump(mode="json"))
    try:
        if proposal.action == RemediationKind.PROPOSE_CHANGESET.value:
            if bool(proposal.parameters.get("auto_apply")):
                try:
                    written = apply_whitelisted_writes(project, proposal.parameters)
                except (TypeError, ValueError, OSError) as exc:
                    return _finish_manual(
                        session,
                        agent_session,
                        reason=f"action.rejected: automatic write is outside the production whitelist ({exc})",
                        event_type="action.rejected",
                    )
                agent_session.status = AgentSessionStatus.COMPLETED.value
                agent_session.stop_reason = None
                agent_session.diagnostic_reason = safe_proposal_reason
                agent_session.result_json = {
                    **agent_session.result_json,
                    "auto_applied": True,
                    "written_files": written,
                }
                agent_session.completed_at = utcnow()
                record_agent_event(
                    session,
                    agent_session,
                    "agent.config_applied",
                    data={"files": written, "reason": safe_proposal_reason},
                    turn_id=proposal.turn_id,
                )
                _persist_agent_audit(session, agent_session)
                session.commit()
                return agent_session
            changeset = _write_changeset(session, agent_session, project, proposal.parameters)
            agent_session.status = AgentSessionStatus.AWAITING_USER.value
            agent_session.stop_reason = "changeset.pending_approval"
            agent_session.diagnostic_reason = safe_proposal_reason
            agent_session.result_json = {
                **agent_session.result_json,
                "changeset_id": changeset.id,
                "changeset_status": changeset.status,
            }
            agent_session.completed_at = utcnow()
            record_agent_event(
                session,
                agent_session,
                "changeset.created",
                data={"changeset_id": changeset.id, "status": changeset.status},
                turn_id=proposal.turn_id,
            )
            _persist_agent_audit(session, agent_session)
            session.commit()
            return agent_session
        action = create_remediation(
            session,
            job=job,
            payload=RemediationCreate(
                action=RemediationKind(proposal.action),
                strategy=str(proposal.parameters.get("strategy") or proposal.action),
                reason=safe_proposal_reason,
                parameters=proposal.parameters,
                finding_ids=proposal.finding_ids,
                expected_additional_calls=proposal.expected_additional_calls,
            ),
        )
    except (ServiceError, ValueError, OSError, StorageError) as exc:
        return _finish_manual(
            session,
            agent_session,
            reason=f"action.rejected: {exc}",
            event_type="action.rejected",
            result={"action": proposal.action},
        )
    agent_session.status = AgentSessionStatus.COMPLETED.value
    agent_session.diagnostic_reason = safe_proposal_reason
    agent_session.stop_reason = None
    agent_session.completed_at = utcnow()
    agent_session.updated_at = utcnow()
    record_agent_event(
        session,
        agent_session,
        "agent.diagnosis_ready",
        data={
            "action_id": action.id,
            "action": action.action,
            "status": action.status,
            "budget_used": agent_session.budget_used,
        },
        turn_id=proposal.turn_id,
    )
    _persist_agent_audit(session, agent_session)
    session.commit()
    return agent_session


def list_agent_events(session: Session, agent_session_id: str) -> list[AgentEvent]:
    return list(
        session.scalars(
            select(AgentEvent)
            .where(AgentEvent.session_id == agent_session_id)
            .order_by(AgentEvent.sequence)
        ).all()
    )
