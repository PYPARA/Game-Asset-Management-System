"""Conversational generation planning services.

The planning workbench is deliberately a planning boundary, not another
execution engine.  This module owns the redacted read-only context package,
durable conversation events, deterministic draft validation, and the final
all-or-nothing hand-off to the existing Controller/Runner.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import shutil
from collections.abc import AsyncIterable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from .agent import _redact, _persist_agent_audit, record_agent_event
from .codex_adapter import (
    CodexAdapterError,
    CodexUnavailable,
    PlanningCodexAdapter,
    UnavailableCodexAdapter,
)
from .domain import (
    AssetKind,
    GenerationAssetProposal,
    GenerationConversationConfirm,
    GenerationConversationCreate,
    GenerationConversationDraftUpdate,
    GenerationPlanningDraft,
    GenerationInputQuestion,
    GenerationReferenceProposal,
    GenerationTaskProposal,
    GenerationTask,
    GenerationPlanCreate,
    TaskKind,
    AgentSessionStatus,
    STABLE_KEY_RE,
)
from .models import (
    AgentEvent,
    PlanningTurn,
    PlanningAttempt,
    ConversationBatch,
    AgentInputRequest,
    AgentSession,
    Asset,
    AssetRelation,
    AssetRevision,
    GenerationJob,
    GenerationPlan,
    Project,
    ProviderProfile,
    ProviderRoutingDefaults,
    Rendition,
    new_id,
    utcnow,
)
from .provider_catalog import (
    default_route,
    model_is_compatible,
    provider_credentials_ready,
    required_modality,
)
from .services import (
    ServiceError,
    asset_descriptor,
    confirm_plan,
    create_plan,
    require,
)
from .storage import (
    ProjectStore,
    StorageError,
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    json_bytes,
    relative_to_root,
    safe_join,
    sha256_bytes,
    sha256_file,
)


PLANNING_PURPOSE = "generation_planning"
PLANNING_SCHEMA_VERSION = 2
PUBLIC_EVENT_TYPES = frozenset(
    {
        "turn.started",
        "turn.recovering",
        "thread.rebuilt",
        "draft.conflict",
        "assistant.delta",
        "assistant.message",
        "reasoning.summary",
        "tool.started",
        "tool.progress",
        "tool.result",
        "usage.updated",
        "draft.updated",
        "turn.completed",
        "turn.failed",
        "agent.unavailable",
        "conversation.confirmed",
        "user_input.requested",
        "user_input.resolved",
        "user_input.fallback",
        "user_input.cancelled",
    }
)
USER_EVENT_TYPE = "user.message"
ACTIVE_PLANNING_STATUSES = frozenset({"running", "awaiting_input"})


def public_input_request(item: AgentInputRequest) -> dict[str, Any]:
    return {
        "id": item.id,
        "turn_id": item.turn_id,
        "response_mode": item.response_mode,
        "status": item.status,
        "questions": item.questions_json,
        "answers": item.answers_json or {},
        "auto_resolution_ms": item.auto_resolution_ms,
        "fallback_reason": item.fallback_reason,
    }


def pending_input_request(session: Session, session_id: str) -> AgentInputRequest | None:
    return session.scalar(
        select(AgentInputRequest).where(
            AgentInputRequest.session_id == session_id,
            AgentInputRequest.status == "pending",
        ).order_by(AgentInputRequest.created_at.desc()).limit(1)
    )


def _fallback_input_request(session: Session, row: AgentSession, *, reason: str) -> AgentInputRequest | None:
    questions = (row.draft_json or {}).get("questions") or []
    if not questions or pending_input_request(session, row.id):
        return None
    item_id = f"draft-questions-{row.draft_version}"
    existing = session.scalar(select(AgentInputRequest).where(
        AgentInputRequest.session_id == row.id, AgentInputRequest.item_id == item_id,
    ))
    if existing:
        return None
    last = session.scalar(select(AgentEvent).where(
        AgentEvent.session_id == row.id, AgentEvent.event_type == USER_EVENT_TYPE,
    ).order_by(AgentEvent.sequence.desc()).limit(1))
    item = AgentInputRequest(
        session_id=row.id, turn_id=last.turn_id if last and last.turn_id else f"legacy-{row.id}",
        item_id=item_id, response_mode="new_turn", status="pending", fallback_reason=reason,
        questions_json=[{"id": "clarification", "header": "补充设定", "question": "请回答以下问题：\n" + "\n".join(str(q) for q in questions), "options": None, "isOther": False}],
    )
    session.add(item)
    session.flush()
    record_agent_event(session, row, "user_input.requested", data=public_input_request(item), turn_id=item.turn_id)
    return item
HIDDEN_REASONING_KEYS = frozenset(
    {
        "analysis",
        "analysis_text",
        "reasoning",
        "reasoning_content",
        "thinking",
        "thoughts",
        "chain_of_thought",
        "internal_reasoning",
        "hidden_thoughts",
    }
)


def _now_iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _relative_or_none(root: Path, path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        return relative_to_root(root, candidate)
    except (OSError, UnsafePathError, ValueError):
        return None


def _context_value(value: Any, root: Path, *, key_hint: str = "") -> Any:
    """Redact nested metadata while keeping path-shaped values project-relative.

    Catalog metadata is user-authored and may contain an absolute source path or
    a stale path that points outside the project.  Such values are useful to the
    catalog scanner, but they must not cross the planning boundary.  Keep safe
    relative paths (and ordinary metadata) while replacing unsafe path values
    with ``None``.  Credential-shaped strings are handled by ``_redact`` below.
    """

    if isinstance(value, dict):
        return {
            str(key): _context_value(item, root, key_hint=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_context_value(item, root, key_hint=key_hint) for item in value]
    if isinstance(value, tuple):
        return [_context_value(item, root, key_hint=key_hint) for item in value]
    if "path" in key_hint.lower() and isinstance(value, (str, Path)):
        return _relative_or_none(root, value)
    return value


def _strip_hidden_reasoning(value: Any) -> Any:
    """Remove provider-private reasoning fields at every nesting level.

    Visible decision summaries use explicit fields such as ``reason`` and
    ``decision_basis`` and remain intact.  Provider-specific analysis payloads
    are neither persisted nor streamed, even when nested inside a tool result
    or a draft metadata object.
    """

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in HIDDEN_REASONING_KEYS:
                continue
            result[str(key)] = _strip_hidden_reasoning(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_strip_hidden_reasoning(item) for item in value]
    return value


def _safe_text(value: Any, limit: int = 8_000) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}\n[truncated]"


def _planning_error(reason: Any, *, code: str | None = None) -> dict[str, Any]:
    """Map runtime/provider text to the stable public planning error contract."""

    detail = _safe_text(reason, 500) or "planning turn failed"
    lowered = detail.lower()
    error_code = code
    if error_code is None:
        if "planning_provider_" in lowered:
            error_code = "provider_configuration"
        elif "invalid_json_schema" in lowered or "invalid schema for response_format" in lowered:
            error_code = "output_schema_invalid"
        elif any(token in lowered for token in ("login", "auth", "unauthorized", "401", "登录")):
            error_code = "auth_required"
        elif any(token in lowered for token in ("overload", "server busy", "rate limit", "429", "过载", "限流")):
            error_code = "overloaded"
        elif "draft.invalid" in lowered or "draft schema" in lowered:
            error_code = "draft_invalid"
        elif any(token in lowered for token in ("context", "100kb", "stale")):
            error_code = "context_invalid"
        elif any(token in lowered for token in ("user.cancelled", "interrupted", "cancelled")):
            error_code = "interrupted"
        elif "timed out" in lowered or "timeout" in lowered:
            error_code = "planning_timeout"
        elif "transport" in lowered or "connection" in lowered:
            error_code = "connection_lost"
        elif "paginated_threads" in lowered or "unknown thread" in lowered or "thread/resume" in lowered:
            error_code = "resume_incompatible"
        elif any(token in lowered for token in ("unable to start", "无法启动", "executable", "固定运行时")):
            error_code = "runtime_unavailable"
        else:
            error_code = "turn_failed"
    messages = {
        "planning_timeout": "规划服务长时间没有返回事件，已保存现场；请检查供应商响应后继续。",
        "provider_configuration": detail.split(":", 1)[-1].strip(),
        "auth_required": "规划供应商认证失败，请检查供应商凭据后重试。",
        "runtime_unavailable": "Codex 固定运行时当前不可用，请检查安装后重试。",
        "overloaded": "Codex 当前过载，本次内容已保留，可以直接重试。",
        "context_invalid": "规划上下文无效或已变化，请刷新上下文后重试。",
        "draft_invalid": "Codex 返回的方案未通过校验，可重试或转为人工编辑。",
        "output_schema_invalid": "Codex 结构化输出配置无效，请更新服务后重试。",
        "interrupted": "本轮已由用户停止，已有消息和草案仍然保留。",
        "process_restarted": "服务已重启，正在恢复刚才的规划；你的回答已保存。",
        "connection_lost": "规划连接中断，已保存现场并等待恢复。",
        "resume_incompatible": "原会话恢复协议不兼容，可以保留上下文继续规划。",
        "turn_failed": "规划回合未完成，可以重试或继续人工编辑。",
    }
    return {
        "error_code": error_code,
        "reason": detail,
        "message": messages.get(error_code, messages["turn_failed"]),
        "retryable": error_code not in {"auth_required", "output_schema_invalid"},
    }


def _validation_path(location: Iterable[Any]) -> str:
    path = ""
    for item in location:
        if isinstance(item, int):
            path += f"[{item}]"
        elif path:
            path += f".{item}"
        else:
            path = str(item)
    return path


def _draft_validation_diagnostic(exc: BaseException) -> dict[str, str]:
    """Return a safe field-level diagnostic without exposing the rejected draft."""

    cause = exc.__cause__
    if not isinstance(cause, ValidationError):
        return {}
    detail = cause.errors()[0]
    field_path = _validation_path(detail.get("loc", ()))
    if field_path.startswith("warnings["):
        message = "必须是包含有效 code 和非空 message 的对象"
    else:
        message = str(detail.get("msg") or "字段值无效")[:300]
    return {"field_path": field_path, "validation_message": message}


def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True)
    return dict(value)


def draft_hash(draft: dict[str, Any] | GenerationPlanningDraft) -> str:
    """Return the stable hash used by optimistic edits and confirmation."""

    data = _model_dict(draft) if not isinstance(draft, dict) else draft
    return sha256_bytes(canonical_json(_redact(data)))


def _slug(value: str, fallback: str = "asset") -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9._-]+", "-", value)
    value = re.sub(r"[-_.]{2,}", "-", value).strip("-_.")
    return value or fallback


def _ensure_relative_path(value: str | None, *, label: str, allow_empty: bool = True) -> str | None:
    if value is None:
        return None
    normalized = value.replace("\\", "/").strip()
    if not normalized and allow_empty:
        return None
    if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise ServiceError(422, f"{label} must be a project-relative path")
    parts = [part for part in normalized.split("/") if part]
    if not parts or any(part in {".", ".."} for part in parts):
        raise ServiceError(422, f"{label} contains an unsafe path segment")
    if any("\x00" in part for part in parts):
        raise ServiceError(422, f"{label} contains an unsafe character")
    return "/".join(parts)


def _context_snapshot_path(store: ProjectStore, session_id: str, version: int = 1) -> Path:
    """Return an immutable, project-relative context snapshot path.

    A planning conversation can gain additional seed assets between turns.  Do
    not overwrite the first snapshot (it is useful audit evidence); write a
    monotonically versioned snapshot and point the session at the newest one.
    """

    suffix = "" if version <= 1 else f"-v{version}"
    return store.workspace / "agent" / "sessions" / session_id / f"context{suffix}.json"


def _context_index_path(store: ProjectStore, session_id: str, version: int = 1) -> Path:
    suffix = "" if version <= 1 else f"-v{version}"
    return store.workspace / "agent" / "sessions" / session_id / f"context-index{suffix}.json"


def _context_manifest(context: dict[str, Any], *, index_filename: str) -> dict[str, Any]:
    """Build the small file Codex reads first for a Context v2 snapshot."""

    seed_ids = set(context.get("seed_asset_ids", []))
    project_spec_assets = {
        str(item.get("asset", {}).get("id"))
        for item in context.get("project_specs", [])
        if isinstance(item, dict) and isinstance(item.get("asset"), dict)
    }
    fixed_asset_ids = seed_ids | project_spec_assets
    fixed_assets = [
        item for item in context.get("assets", [])
        if isinstance(item, dict) and str(item.get("id")) in fixed_asset_ids
    ]
    fixed_revision_ids = {
        str(item.get("id"))
        for item in context.get("fixed_revisions", [])
        if isinstance(item, dict) and item.get("id")
    }
    return {
        "context_manifest_version": 1,
        "context_version": context.get("context_version", 2),
        "context_hash": context.get("_context_hash"),
        "purpose": context.get("purpose"),
        "read_only": True,
        "project": context.get("project", {}),
        "seed_asset_ids": sorted(seed_ids),
        "counts": {
            "assets": len(context.get("assets", [])),
            "relations": len(context.get("relations", [])),
            "revisions": len(context.get("revision_index", [])),
            "renditions": len(context.get("rendition_index", [])),
        },
        "fixed_assets": fixed_assets,
        "fixed_revisions": context.get("fixed_revisions", []),
        "fixed_renditions": [
            item for item in context.get("rendition_index", [])
            if isinstance(item, dict) and str(item.get("revision_id")) in fixed_revision_ids
        ],
        "project_specs": context.get("project_specs", []),
        "provider_routes": context.get("provider_routes", {}),
        "providers": context.get("providers", []),
        "constraints": context.get("constraints", {}),
        "index_files": {
            "full": index_filename,
            "usage": "Use rg or a targeted JSON query for a specific key/id. Never print the complete index.",
        },
    }


def _write_context_snapshot(
    store: ProjectStore,
    session_id: str,
    version: int,
    context: dict[str, Any],
) -> Path:
    context_path = _context_snapshot_path(store, session_id, version)
    index_path = _context_index_path(store, session_id, version)
    atomic_write_json(index_path, context, immutable=True)
    atomic_write_json(
        context_path,
        _context_manifest(context, index_filename=index_path.name),
        immutable=True,
    )
    return context_path


def _context_snapshot_is_compact(store: ProjectStore, relative_path: str | None) -> bool:
    if not relative_path:
        return False
    try:
        value = json.loads(safe_join(store.root, relative_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, StorageError, ServiceError):
        return False
    return isinstance(value, dict) and int(value.get("context_manifest_version", 0)) >= 1


def _asset_snapshot(asset: Asset, root: Path) -> dict[str, Any]:
    return _redact(
        {
            "id": asset.id,
            "key": asset.key,
            "kind": asset.kind,
            "subtype": asset.subtype,
            "title": asset.title,
            "schema_ref": asset.schema_ref,
            "tags": list(asset.tags or []),
            "metadata": _context_value(dict(asset.metadata_json or {}), root),
            "content_status": asset.content_status,
            "generation_status": asset.generation_status,
            "publication_status": asset.publication_status,
            "current_revision_id": asset.current_revision_id,
            "latest_candidate_revision_id": asset.latest_candidate_revision_id,
            "paths": {
                "source": _relative_or_none(root, (asset.metadata_json or {}).get("source_path")),
            },
        }
    )


def _revision_snapshot(revision: AssetRevision, root: Path) -> dict[str, Any]:
    return _redact(
        {
            "id": revision.id,
            "asset_id": revision.asset_id,
            "sequence": revision.sequence,
            "format": revision.format,
            "content": _context_value(revision.content_json, root),
            "content_hash": revision.content_hash,
            "input_hash": revision.input_hash,
            "style_revision": revision.style_revision,
            "prompt_recipe": revision.prompt_recipe,
            "provider_snapshot": _context_value(revision.provider_snapshot, root),
            "file_path": _relative_or_none(root, revision.file_path),
            "review_status": revision.review_status,
            "created_at": _now_iso(revision.created_at),
        }
    )


def _revision_index_snapshot(revision: AssetRevision, root: Path) -> dict[str, Any]:
    """Small immutable revision locator for Context v2.

    Full historical content was the dominant source of multi-megabyte list and
    prompt payloads.  Codex only receives full content for fixed seed/reference
    revisions; the complete project history remains represented by IDs/hashes.
    """

    return _redact(
        {
            "id": revision.id,
            "asset_id": revision.asset_id,
            "sequence": revision.sequence,
            "format": revision.format,
            "content_hash": revision.content_hash,
            "input_hash": revision.input_hash,
            "style_revision": revision.style_revision,
            "file_path": _relative_or_none(root, revision.file_path),
            "review_status": revision.review_status,
            "created_at": _now_iso(revision.created_at),
        }
    )


def _rendition_snapshot(rendition: Rendition, root: Path) -> dict[str, Any]:
    return _redact(
        {
            "id": rendition.id,
            "revision_id": rendition.revision_id,
            "media_type": rendition.media_type,
            "source_path": _relative_or_none(root, rendition.source_path),
            "normalized_path": _relative_or_none(root, rendition.normalized_path),
            "target_path": _relative_or_none(root, rendition.target_path),
            "sha256": rendition.sha256,
            "width": rendition.width,
            "height": rendition.height,
            "byte_size": rendition.byte_size,
            "created_at": _now_iso(rendition.created_at),
        }
    )


def build_planning_context(
    session: Session,
    project: Project,
    *,
    seed_asset_ids: Iterable[str] = (),
) -> tuple[dict[str, Any], str]:
    """Build the compact, redacted Context v2 read-only package.

    Only project-relative paths, immutable hashes, and catalog metadata are
    exposed.  The package is intentionally deterministic so a later confirmation
    can detect a changed reference or route with a context-hash comparison.
    """

    store = ProjectStore(project.root_path)
    seed_ids = [str(value) for value in seed_asset_ids if value]
    all_assets = list(
        session.scalars(select(Asset).where(Asset.project_id == project.id).order_by(Asset.key)).all()
    )
    by_id = {asset.id: asset for asset in all_assets}
    missing_seed = sorted(set(seed_ids) - set(by_id))
    if missing_seed:
        raise ServiceError(422, f"seed assets belong to another project or do not exist: {', '.join(missing_seed)}")
    relations = list(
        session.scalars(
            select(AssetRelation)
            .where(AssetRelation.project_id == project.id)
            .order_by(AssetRelation.source_asset_id, AssetRelation.target_asset_id, AssetRelation.relation_type)
        ).all()
    )
    revisions = list(
        session.scalars(
            select(AssetRevision)
            .where(AssetRevision.asset_id.in_(list(by_id)))
            .order_by(AssetRevision.asset_id, AssetRevision.sequence)
        ).all()
        if by_id
        else []
    )
    revision_ids = {revision.id for revision in revisions}
    renditions = list(
        session.scalars(
            select(Rendition)
            .where(Rendition.revision_id.in_(revision_ids))
            .order_by(Rendition.revision_id, Rendition.id)
        ).all()
        if revision_ids
        else []
    )

    # Project specs are included as bounded source text plus a fixed hash.  The
    # agent can explain a style or recipe choice without ever receiving secrets.
    specs: list[dict[str, Any]] = []
    for asset in all_assets:
        if asset.subtype not in {"style_bible", "prompt_recipe", "visual_anchor"}:
            continue
        metadata = dict(asset.metadata_json or {})
        source_path = (
            metadata.get("style_markdown_path")
            or metadata.get("recipe_path")
            or metadata.get("source_path")
        )
        try:
            source_relative = (
                _ensure_relative_path(str(source_path), label="spec source")
                if source_path
                else None
            )
        except ServiceError:
            # Unsafe or stale source metadata is not a reason to make the whole
            # planning conversation unavailable; omit it from the read-only
            # package and let the user repair the project specification.
            source_relative = None
        source_content = None
        source_hash = None
        if source_relative:
            try:
                source_file = safe_join(store.root, source_relative)
                if source_file.is_file():
                    raw = source_file.read_bytes()
                    source_hash = sha256_bytes(raw)
                    source_content = _safe_text(raw.decode("utf-8", "replace"), 12_000)
            except (OSError, StorageError, ServiceError):
                source_content = None
        specs.append(
            {
                "asset": _asset_snapshot(asset, store.root),
                "source_path": source_relative,
                "source_sha256": source_hash,
                "source": source_content,
            }
        )

    defaults = session.get(ProviderRoutingDefaults, "global")
    providers = []
    for profile in session.scalars(select(ProviderProfile).order_by(ProviderProfile.name, ProviderProfile.id)).all():
        providers.append(
            _redact(
                {
                    "id": profile.id,
                    "name": profile.name,
                    "kind": profile.kind,
                    "is_active": profile.is_active,
                    "access_state": "ready" if provider_credentials_ready(profile, unlocked=False) else "locked",
                    "models": [
                        {
                            "id": item.get("id"),
                            "modalities": item.get("modalities", []),
                            "classification": item.get("classification", "unknown"),
                            "available": item.get("available", True),
                            "enabled": item.get("enabled", True),
                        }
                        for item in (profile.models_json or [])
                        if isinstance(item, dict) and item.get("id")
                    ],
                    "route_available": provider_credentials_ready(profile, unlocked=False),
                }
            )
        )

    target_paths = sorted(
        {
            relative_target
            for rendition in renditions
            if rendition.target_path
            for relative_target in [_relative_or_none(store.root, rendition.target_path)]
            if relative_target
        }
    )
    reference_asset_ids = set(seed_ids)
    for relation in relations:
        if relation.source_asset_id in reference_asset_ids:
            reference_asset_ids.add(relation.target_asset_id)
        if relation.target_asset_id in reference_asset_ids:
            reference_asset_ids.add(relation.source_asset_id)
    reference_asset_ids.update(
        asset.id
        for asset in all_assets
        if asset.subtype in {"style_bible", "prompt_recipe", "visual_anchor"}
    )
    fixed_revision_ids = {
        revision_id
        for asset_id in reference_asset_ids
        for revision_id in (
            getattr(by_id.get(asset_id), "current_revision_id", None),
            getattr(by_id.get(asset_id), "latest_candidate_revision_id", None),
        )
        if revision_id
    }
    fixed_revisions = [revision for revision in revisions if revision.id in fixed_revision_ids]

    context: dict[str, Any] = {
        "context_version": 2,
        "purpose": PLANNING_PURPOSE,
        "read_only": True,
        "project": {
            "id": project.id,
            "name": project.name,
            "default_language": project.default_language,
            "relative_root": ".",
        },
        "seed_asset_ids": sorted(set(seed_ids)),
        "assets": [_asset_snapshot(asset, store.root) for asset in all_assets],
        "relations": [
            _redact(
                {
                    "id": row.id,
                    "source_asset_id": row.source_asset_id,
                    "target_asset_id": row.target_asset_id,
                    "relation_type": row.relation_type,
                    "metadata": _context_value(row.metadata_json or {}, store.root),
                }
            )
            for row in relations
        ],
        "revision_index": [_revision_index_snapshot(revision, store.root) for revision in revisions],
        "fixed_revisions": [_revision_snapshot(revision, store.root) for revision in fixed_revisions],
        "rendition_index": [_rendition_snapshot(rendition, store.root) for rendition in renditions],
        "project_specs": specs,
        "provider_routes": {
            "text": {
                "provider_profile_id": getattr(defaults, "text_provider_profile_id", None),
                "model": getattr(defaults, "text_model", None),
            },
            "image": {
                "provider_profile_id": getattr(defaults, "image_provider_profile_id", None),
                "model": getattr(defaults, "image_model", None),
            },
            "max_concurrency": getattr(defaults, "max_concurrency", 3),
            "max_transport_retries": getattr(defaults, "max_transport_retries", 2),
        },
        "providers": providers,
        "target_paths": target_paths,
        "constraints": {
            "safe_context": True,
            "read_only": True,
            "database_access": False,
            "version_control_access": False,
            "write_tools": False,
            "candidate_root": "workspace/candidates",
            "allowed_task_kinds": [item.value for item in TaskKind],
        },
    }
    context = _redact(context)
    context_hash = sha256_bytes(canonical_json(context))
    context["_context_hash"] = context_hash
    return context, context_hash


def _default_draft(context_hash: str, title: str | None = None, defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    provider_defaults = defaults or {}
    route_defaults = {
        modality: (
            {
                "provider_profile_id": value.get("provider_profile_id"),
                "model": value.get("model"),
            }
            if isinstance(value, dict) and (value.get("provider_profile_id") or value.get("model"))
            else None
        )
        for modality, value in provider_defaults.items()
        if modality in {"text", "image", "video", "audio"}
    }
    draft = GenerationPlanningDraft(
        title=(title or "新资产生成任务").strip() or "新资产生成任务",
        context_hash=context_hash,
        settings={
            "extra_call_budget": 2,
            "max_paid_remediation_rounds": 2,
            "max_transport_retries": int((defaults or {}).get("max_transport_retries", 2)),
            "max_concurrency": int((defaults or {}).get("max_concurrency", 3)),
            "route_defaults": route_defaults,
        },
    )
    return draft.model_dump(mode="json", by_alias=True)


def _normalize_draft(raw: dict[str, Any], *, context_hash: str) -> tuple[GenerationPlanningDraft, dict[str, Any]]:
    try:
        draft = GenerationPlanningDraft.model_validate(raw)
    except ValidationError as exc:
        detail = exc.errors()[0]
        field_path = _validation_path(detail.get("loc", ()))
        prefix = f"{field_path}: " if field_path else ""
        raise ServiceError(
            422,
            f"draft schema invalid: {prefix}{detail.get('msg', 'invalid value')}",
        ) from exc
    # A draft may arrive from a v1 adapter without a context hash.  Stamp the
    # current hash deterministically; a non-matching explicit hash is stale.
    if draft.context_hash and draft.context_hash != context_hash:
        raise ServiceError(409, "generation planning context is stale; refresh the project context")
    draft.context_hash = context_hash
    normalized = draft.model_dump(mode="json", by_alias=True)
    return draft, normalized


def _reference_rows(session: Session, project_id: str, reference: GenerationReferenceProposal) -> tuple[Asset, AssetRevision, Rendition | None]:
    asset = session.get(Asset, reference.asset_id)
    if asset is None or asset.project_id != project_id:
        raise ServiceError(422, f"reference asset {reference.asset_id} is outside this project")
    revision_id = reference.revision_id or asset.latest_candidate_revision_id or asset.current_revision_id
    if not revision_id:
        raise ServiceError(422, f"reference asset {asset.key} has no fixed revision")
    revision = session.get(AssetRevision, revision_id)
    if revision is None or revision.asset_id != asset.id:
        raise ServiceError(422, f"reference revision {revision_id} is invalid for {asset.key}")
    rendition = None
    if reference.rendition_id:
        rendition = session.get(Rendition, reference.rendition_id)
        if rendition is None or rendition.revision_id != revision.id:
            raise ServiceError(422, f"reference rendition {reference.rendition_id} is invalid")
    elif revision.format == "media":
        renditions = list(session.scalars(select(Rendition).where(Rendition.revision_id == revision.id)).all())
        if len(renditions) != 1:
            raise ServiceError(422, f"reference revision {revision.id} must have exactly one rendition")
        rendition = renditions[0]
    expected_hash = reference.sha256
    if expected_hash is not None and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
        raise ServiceError(422, f"reference hash is invalid for {asset.key}")
    actual_hash = rendition.sha256 if rendition else revision.content_hash
    # Legacy model output sometimes copied the source/input hash from the
    # same immutable document revision. Resolve only that provable alias;
    # never accept an arbitrary mismatching hash or switch revision IDs.
    if (reference.revision_id and rendition is None and expected_hash
            and expected_hash.lower() == str(revision.input_hash or "").lower()):
        reference.sha256 = actual_hash
        expected_hash = actual_hash
    if expected_hash and expected_hash.lower() != str(actual_hash or "").lower():
        raise ServiceError(409, f"reference hash is stale for {asset.key}")
    return asset, revision, rendition


def _validate_dependencies(tasks: list[GenerationTaskProposal]) -> None:
    by_id = {task.id: task for task in tasks}
    if len(by_id) != len(tasks):
        raise ServiceError(422, "task IDs must be unique")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(task_id: str) -> None:
        if task_id not in by_id:
            raise ServiceError(422, f"task dependency does not exist: {task_id}")
        if task_id in visiting:
            raise ServiceError(422, "generation dependency graph must be acyclic")
        if task_id in visited:
            return
        visiting.add(task_id)
        for dependency in by_id[task_id].depends_on:
            visit(dependency)
        visiting.remove(task_id)
        visited.add(task_id)

    for task in tasks:
        visit(task.id)


def _validate_draft_settings(settings: dict[str, Any]) -> None:
    """Apply the same bounds as ``GenerationPlanCreate`` during every edit."""

    route = (settings.get("route_defaults") or {}).get("text") or {}
    effort = route.get("reasoning_effort")
    if effort is not None and effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
        raise ServiceError(422, "unsupported reasoning effort")
    bounds = {
        "extra_call_budget": (0, 10_000),
        "max_paid_remediation_rounds": (0, 20),
        "max_transport_retries": (0, 8),
        "max_concurrency": (1, 32),
    }
    for key, (minimum, maximum) in bounds.items():
        if key not in settings or settings[key] is None:
            continue
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ServiceError(422, f"draft setting {key} must be an integer")
        if value < minimum or value > maximum:
            raise ServiceError(422, f"draft setting {key} must be between {minimum} and {maximum}")


def validate_planning_draft(
    session: Session,
    project: Project,
    raw_draft: dict[str, Any] | GenerationPlanningDraft,
    *,
    context_hash: str,
    for_confirm: bool = False,
) -> dict[str, Any]:
    """Validate and normalize a draft without creating any production rows."""

    raw = _model_dict(raw_draft) if not isinstance(raw_draft, dict) else raw_draft
    draft, normalized = _normalize_draft(raw, context_hash=context_hash)
    _validate_draft_settings(dict(draft.settings or {}))
    _validate_dependencies(draft.tasks)
    store = ProjectStore(project.root_path)
    assets = list(session.scalars(select(Asset).where(Asset.project_id == project.id)).all())
    by_id = {asset.id: asset for asset in assets}
    by_key = {asset.key: asset for asset in assets}
    proposed_keys: set[str] = set()
    target_paths: dict[str, str] = {}
    existing_targets: dict[str, str] = {}
    unresolved_schema_questions: set[str] = set()
    for rendition in session.scalars(select(Rendition)).all():
        if not rendition.target_path:
            continue
        revision = session.get(AssetRevision, rendition.revision_id)
        asset = session.get(Asset, revision.asset_id) if revision else None
        if asset and asset.project_id == project.id:
            normalized_target = rendition.target_path.replace("\\", "/").strip()
            existing_targets[normalized_target.casefold()] = asset.key

    for task in draft.tasks:
        if not STABLE_KEY_RE.fullmatch(task.id):
            raise ServiceError(422, f"task ID is not stable: {task.id}")
        proposal = task.asset
        if proposal.mode == "existing":
            if not proposal.asset_id or proposal.asset_id not in by_id:
                raise ServiceError(422, f"task {task.id} must reference an existing project asset")
            asset = by_id[proposal.asset_id]
        else:
            key = proposal.key or f"{task.kind.value}.{_slug(proposal.title or task.id)}"
            if not STABLE_KEY_RE.fullmatch(key):
                raise ServiceError(422, f"new asset key is invalid: {key}")
            if key in by_key or key in proposed_keys:
                raise ServiceError(409, f"asset key already exists or is duplicated: {key}")
            proposed_keys.add(key)
            # Persist the generated stable key in the normalized draft so the
            # user can review exactly what will be registered at confirmation.
            proposal.key = key
            if proposal.kind is None or not proposal.subtype or not proposal.title:
                raise ServiceError(422, f"new asset proposal for task {task.id} is incomplete")
            if proposal.kind == AssetKind.MEDIA and task.kind == TaskKind.TEXT:
                raise ServiceError(422, f"text task {task.id} cannot create a media asset")
            asset = None
        if task.kind == TaskKind.TEXT:
            schema = task.output_schema
            if schema is None and asset is not None:
                schema_ref = asset.schema_ref
                if schema_ref:
                    try:
                        schema = store.read_schema(schema_ref)
                    except StorageError as exc:
                        raise ServiceError(422, f"task {task.id} schema is unavailable: {exc}") from exc
            if schema is None and asset is None and proposal.schema_ref:
                try:
                    schema = store.read_schema(proposal.schema_ref)
                except StorageError as exc:
                    raise ServiceError(422, f"task {task.id} schema is unavailable: {exc}") from exc
            if schema is None:
                if for_confirm:
                    raise ServiceError(422, f"task {task.id} requires a project JSON Schema before confirmation")
                unresolved_schema_questions.add(f"schema:{task.id}")
                if f"schema:{task.id}" not in draft.questions:
                    draft.questions.append(f"schema:{task.id}")
            else:
                try:
                    if not isinstance(schema, dict):
                        raise TypeError("schema must be an object")
                    jsonschema.Draft202012Validator.check_schema(schema)
                except (TypeError, jsonschema.SchemaError) as exc:
                    raise ServiceError(422, f"task {task.id} has an invalid JSON Schema: {exc}") from exc
        else:
            if task.width is None or task.height is None:
                if for_confirm:
                    raise ServiceError(422, f"task {task.id} requires width and height")
            if task.width and task.height and (task.width > 8192 or task.height > 8192):
                raise ServiceError(422, f"task {task.id} dimensions exceed 8192")

        provider_id = task.provider_profile_id
        model = (task.model or "").strip()
        modality = required_modality(task.kind.value)
        route_defaults = dict(draft.settings or {}).get("route_defaults", {})
        if route_defaults is None:
            route_defaults = {}
        if not isinstance(route_defaults, dict):
            raise ServiceError(422, "draft setting route_defaults must be an object")
        session_route = route_defaults.get(modality)
        if session_route is not None and not isinstance(session_route, dict):
            raise ServiceError(422, f"draft route default for {modality} must be an object or null")
        session_provider = str(session_route.get("provider_profile_id") or "").strip() if isinstance(session_route, dict) else ""
        session_model = str(session_route.get("model") or "").strip() if isinstance(session_route, dict) else ""
        system_provider, system_model = default_route(session, task.kind.value)
        # Fixed precedence: task fields > current-session defaults > system
        # defaults. When a task explicitly selects a provider, only a model
        # from that same provider may be inherited from a session/system route;
        # this prevents a stale route from pairing the wrong provider/model.
        explicit_provider = provider_id
        default_provider = session_provider or system_provider
        provider_id = provider_id or default_provider
        if provider_id:
            profile = session.get(ProviderProfile, provider_id)
            if profile is None or not profile.is_active:
                raise ServiceError(422, f"task {task.id} references an unavailable provider")
            if not model:
                if session_model and (not explicit_provider or session_provider == profile.id):
                    model = session_model
                elif system_model and (not explicit_provider or system_provider == profile.id):
                    model = system_model
            if model:
                if not model_is_compatible(profile, model, modality):
                    raise ServiceError(422, f"task {task.id} model {model} is not compatible with {task.kind.value}")
            elif for_confirm:
                raise ServiceError(422, f"task {task.id} requires a provider model")
        elif for_confirm:
            raise ServiceError(422, f"task {task.id} requires a provider route")
        effort = session_route.get("reasoning_effort") if isinstance(session_route, dict) and modality == "text" else None
        if effort is not None:
            if effort not in {"none", "minimal", "low", "medium", "high", "xhigh"}:
                raise ServiceError(422, "unsupported reasoning effort")
            task.metadata = {**task.metadata, "reasoning_effort": effort}
        if provider_id and not task.provider_profile_id:
            task.provider_profile_id = provider_id
        if model and not task.model:
            task.model = model

        primary_count = 0
        for reference in task.references:
            _asset, revision, rendition = _reference_rows(session, project.id, reference)
            if reference.role == "primary":
                primary_count += 1
            if rendition is not None and not _relative_or_none(store.root, rendition.normalized_path or rendition.source_path):
                raise ServiceError(422, f"reference rendition {rendition.id} is outside the project")
            if for_confirm:
                # Freeze the exact revision/rendition/hash in the persisted
                # draft.  This prevents a later scan from silently changing the
                # image that the Runner receives.
                reference.revision_id = revision.id
                reference.rendition_id = rendition.id if rendition else None
                reference.sha256 = rendition.sha256 if rendition else revision.content_hash
        if primary_count > 1:
            raise ServiceError(422, f"task {task.id} can have only one primary reference")
        if task.kind == TaskKind.IMAGE_EDIT and not task.references and not task.reference_task_id:
            if for_confirm:
                raise ServiceError(422, f"image edit task {task.id} requires a primary reference")
        if task.kind == TaskKind.IMAGE_EDIT and task.references and primary_count != 1 and not task.reference_task_id:
            raise ServiceError(422, f"image edit task {task.id} requires exactly one primary reference")
        if task.reference_task_id and task.reference_task_id not in task.depends_on:
            raise ServiceError(422, f"image edit task {task.id} reference task must be a dependency")

        if task.target_path:
            target = _ensure_relative_path(task.target_path, label=f"task {task.id} target_path", allow_empty=False)
            assert target is not None
            if target.casefold().startswith("workspace/candidates/"):
                raise ServiceError(422, f"task {task.id} target_path cannot be a candidate staging path")
            task.target_path = target
            target_paths[task.id] = target
            owner = existing_targets.get(target.casefold())
            if owner and (asset is None or owner != asset.key):
                raise ServiceError(409, f"target path conflicts with {owner}: {target}")
            previous = next(
                (
                    task_id
                    for task_id, value in target_paths.items()
                    if task_id != task.id and value.casefold() == target.casefold()
                ),
                None,
            )
            if previous:
                raise ServiceError(409, f"target path is used by tasks {previous} and {task.id}: {target}")
        elif for_confirm:
            raise ServiceError(422, f"task {task.id} requires an approved target path")
        if task.candidate_path:
            candidate = _ensure_relative_path(
                task.candidate_path,
                label=f"task {task.id} candidate_path",
                allow_empty=False,
            )
            assert candidate is not None
            if not candidate.startswith("workspace/candidates/"):
                raise ServiceError(422, f"task {task.id} candidate_path must stay under workspace/candidates")
            task.candidate_path = candidate

    # Questions generated by a previous validation pass should disappear once
    # the user supplies the missing schema.  Preserve all non-schema questions
    # authored by the Agent.
    draft.questions = [
        question
        for question in draft.questions
        if not question.startswith("schema:") or question in unresolved_schema_questions
    ]

    # Keep the normalized model values in the persisted draft, while preserving
    # incomplete values (the UI can still edit them) when this is not confirm.
    for task_data, task in zip(normalized["tasks"], draft.tasks, strict=False):
        if task.provider_profile_id is None and task_data.get("provider_profile_id") is None:
            continue
        task_data["provider_profile_id"] = task.provider_profile_id or task_data.get("provider_profile_id")
        if task.model:
            task_data["model"] = task.model
    normalized = draft.model_dump(mode="json", by_alias=True)
    normalized["context_hash"] = context_hash
    # User edits share the same no-credentials boundary as adapter output.
    # This also guarantees that a later planning turn never receives a secret
    # pasted into a Prompt or metadata field through the manual editor.
    redacted = _redact(normalized)
    return redacted if isinstance(redacted, dict) else normalized


def _event_payload(event: AgentEvent) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "id": event.id,
        "session_id": event.session_id,
        "event_type": event.event_type,
        "thread_id": event.thread_id,
        "turn_id": event.turn_id,
        "data": event.data_json or {},
        "created_at": _now_iso(event.created_at),
    }


def _messages_from_events(events: Iterable[AgentEvent]) -> list[dict[str, Any]]:
    """Return only the latest user instruction for the durable Codex thread.

    The SDK thread already owns prior model-visible history. Replaying every
    persisted assistant message duplicated the full conversation on each turn
    and caused prompt growth unrelated to the user's latest instruction.
    """

    latest: dict[str, Any] | None = None
    for event in events:
        data = event.data_json or {}
        if event.event_type == USER_EVENT_TYPE:
            latest = {
                "role": "user",
                "content": _safe_text(data.get("content"), 20_000),
                "turn_id": event.turn_id,
            }
    return [latest] if latest else []


def _normalize_adapter_event(raw: Any, *, turn_id: str) -> tuple[str, dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    event_type = str(raw.get("type", raw.get("event_type", ""))).strip()
    if event_type.startswith("agent."):
        event_type = event_type.removeprefix("agent.")
    if event_type not in PUBLIC_EVENT_TYPES:
        # Common adapter aliases are intentionally narrow and visible.
        event_type = {
            "assistant_delta": "assistant.delta",
            "assistant_message": "assistant.message",
            "tool_started": "tool.started",
            "tool_result": "tool.result",
            "draft": "draft.updated",
            "completed": "turn.completed",
            "failed": "turn.failed",
        }.get(event_type, "")
    if not event_type:
        return None
    payload = raw.get("data") if isinstance(raw.get("data"), dict) else {
        key: value for key, value in raw.items() if key not in {"type", "event_type"}
    }
    payload = _strip_hidden_reasoning(
        _redact(payload if isinstance(payload, dict) else {"value": payload})
    )
    # The planning protocol exposes only visible assistant output.  Adapters
    # occasionally attach provider-specific analysis fields; drop those before
    # the event is persisted or sent to the browser so hidden chain-of-thought
    # cannot become part of the audit trail.
    if raw.get("thread_id") and "thread_id" not in payload:
        payload["thread_id"] = str(raw["thread_id"])
    payload.setdefault("turn_id", turn_id)
    return event_type, payload


async def _collect_adapter_result(result: Any) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Collect JSON/NDJSON/async-stream adapter forms."""

    if inspect.isawaitable(result):
        result = await result
    events: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    if isinstance(result, str):
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = __import__("json").loads(line)
            except Exception as exc:
                raise CodexAdapterError("planning adapter returned invalid NDJSON") from exc
            if isinstance(item, dict):
                events.append(item)
        return events, final
    if isinstance(result, dict):
        raw_events = result.get("events")
        if isinstance(raw_events, list):
            events.extend(item for item in raw_events if isinstance(item, dict))
        # A single event object is accepted too.
        if result.get("type"):
            events.append(result)
        if isinstance(result.get("draft"), dict):
            final = result["draft"]
        elif isinstance(result.get("result"), dict) and (
            "tasks" in result["result"] or "version" in result["result"]
        ):
            final = result["result"]
        elif "tasks" in result or "version" in result:
            final = result
        return events, final
    if isinstance(result, AsyncIterable) or hasattr(result, "__aiter__"):
        async for item in result:
            if isinstance(item, dict):
                events.append(item)
        return events, final
    if isinstance(result, Iterable) and not isinstance(result, (bytes, bytearray)):
        for item in result:
            if isinstance(item, dict):
                events.append(item)
        return events, final
    raise CodexAdapterError("planning adapter returned an unsupported value")


async def _iter_adapter_result(result: Any) -> AsyncIterable[dict[str, Any]]:
    """Yield adapter events as soon as they arrive.

    The adapter boundary accepts the same JSON, NDJSON, iterable and async
    iterable forms as the legacy collector, but this projection deliberately
    does not buffer the whole turn.  Each yielded object can therefore be
    persisted and delivered over SSE before the model finishes thinking.
    """

    if inspect.isawaitable(result):
        result = await result

    def normalize_item(item: Any) -> list[dict[str, Any]]:
        if not isinstance(item, dict):
            return []
        if isinstance(item.get("events"), list):
            values = [dict(value) for value in item["events"] if isinstance(value, dict)]
            if item.get("thread_id"):
                for value in values:
                    value.setdefault("thread_id", item["thread_id"])
            if isinstance(item.get("draft"), dict):
                values.append({"type": "draft.updated", "draft": item["draft"], "thread_id": item.get("thread_id")})
            return values
        if isinstance(item.get("draft"), dict) and not item.get("type") and not item.get("event_type"):
            return [{"type": "draft.updated", "draft": item["draft"], "thread_id": item.get("thread_id")}]
        if isinstance(item.get("result"), dict) and (
            "tasks" in item["result"] or "version" in item["result"]
        ) and not item.get("type") and not item.get("event_type"):
            return [{"type": "draft.updated", "draft": item["result"], "thread_id": item.get("thread_id")}]
        if ("tasks" in item or "version" in item) and not item.get("type") and not item.get("event_type"):
            return [{"type": "draft.updated", "draft": item}]
        return [item]

    if isinstance(result, str):
        for line in result.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except (TypeError, json.JSONDecodeError) as exc:
                raise CodexAdapterError("planning adapter returned invalid NDJSON") from exc
            for item in normalize_item(value):
                yield item
        return
    if isinstance(result, dict):
        for item in normalize_item(result):
            yield item
        return
    if isinstance(result, AsyncIterable) or hasattr(result, "__aiter__"):
        async for item in result:
            for value in normalize_item(item):
                yield value
        return
    if isinstance(result, Iterable) and not isinstance(result, (bytes, bytearray)):
        for item in result:
            for value in normalize_item(item):
                yield value
        return
    raise CodexAdapterError("planning adapter returned an unsupported value")


class GenerationPlanningRunner:
    """One in-process async turn runner with durable event projections."""

    def __init__(self, session_factory: Any, adapter: PlanningCodexAdapter, settings: Any):
        self.session_factory = session_factory
        self.adapter = adapter
        self.settings = settings
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._turn_ids: dict[str, str] = {}

    async def start(self) -> None:
        from .planning_runtime import backfill_planning, recover_turn
        resume = []
        with self.session_factory() as session:
            backfill_planning(session)
            for row in session.scalars(select(AgentSession).where(AgentSession.purpose == PLANNING_PURPOSE, AgentSession.status.in_(ACTIVE_PLANNING_STATUSES))).all():
                pending = pending_input_request(session, row.id)
                if pending:
                    pending.response_mode = "new_turn"
                    pending.fallback_reason = "process_restarted"
                    row.status = "awaiting_user"
                    record_agent_event(session, row, "user_input.fallback", data=public_input_request(pending), turn_id=pending.turn_id)
                    continue
                turn = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == row.id).order_by(PlanningTurn.created_at.desc()))
                if turn and row.stop_reason != "user.cancelled":
                    row.status = "awaiting_user"
                    recover_turn(session, row, turn.id, "startup-" + new_id())
                    resume.append((row.id, turn.id))
            session.commit()
        for session_id, turn_id in resume:
            self.submit(session_id, turn_id)

    async def stop(self) -> None:
        for event in self._cancel_events.values():
            event.set()
        tasks = list(self._tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._cancel_events.clear()
        self._turn_ids.clear()

    def submit(self, session_id: str, turn_id: str) -> None:
        existing = self._tasks.get(session_id)
        if existing and not existing.done():
            raise ServiceError(409, "this generation planning session already has an active turn")
        from .planning_runtime import ensure_attempt
        with self.session_factory() as session:
            ensure_attempt(session, session_id, turn_id)
            session.commit()
        cancel_event = asyncio.Event()
        self._cancel_events[session_id] = cancel_event
        self._turn_ids[session_id] = turn_id
        self._tasks[session_id] = asyncio.create_task(self._run(session_id, turn_id, cancel_event))

    async def cancel(self, session_id: str) -> bool:
        event = self._cancel_events.get(session_id)
        if event is None:
            return False
        event.set()
        thread_id: str | None = None
        active_turn_id = self._turn_ids.get(session_id)
        with self.session_factory() as current_session:
            current = current_session.get(AgentSession, session_id)
            if current is not None:
                thread_id = current.thread_id
        interrupt = getattr(self.adapter, "interrupt", None)
        if interrupt is not None:
            try:
                await interrupt(thread_id=thread_id, turn_id=active_turn_id)
            except Exception:
                # The local turn is still cancelled below. A missing app-server
                # process must not make the manual editor unusable.
                pass
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            if row and row.status in ACTIVE_PLANNING_STATUSES:
                pending = pending_input_request(session, session_id)
                if pending and pending.response_mode == "resume_turn":
                    pending.status = "cancelled"
                    record_agent_event(session, row, "user_input.cancelled", data=public_input_request(pending), turn_id=active_turn_id)
                elif pending:
                    pending.fallback_reason = pending.fallback_reason or "original_turn_stopped"
                row.status = AgentSessionStatus.AWAITING_USER.value
                row.stop_reason = "user.cancelled"
                row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
                row.updated_at = utcnow()
                error = _planning_error("user.cancelled", code="interrupted")
                row.result_json = {**dict(row.result_json or {}), "last_error": error}
                record_agent_event(session, row, "turn.failed", data=error, turn_id=active_turn_id)
                _persist_agent_audit(session, row)
                session.commit()
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            task.cancel()
        return True

    async def steer(self, session_id: str, content: str, client_message_id: str | None = None) -> tuple[str, int]:
        """Append user guidance to the currently active SDK turn."""

        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            prior = find_generation_message(session, row, client_message_id) if row else None
            if prior:
                return prior
        value = content.strip()
        if not value:
            raise ServiceError(422, "message content cannot be empty")
        if len(value) > 20_000:
            raise ServiceError(422, "message content is too long")
        turn_id = self._turn_ids.get(session_id)
        callback = getattr(self.adapter, "steer", None)
        if turn_id is None or callback is None:
            raise ServiceError(409, "this conversation has no active Codex turn")
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            if row is None:
                raise ServiceError(404, "generation conversation not found")
            if row.status != AgentSessionStatus.RUNNING.value:
                raise ServiceError(409, "this conversation is not running")
            thread_id = row.thread_id
        try:
            accepted = await callback(
                content=value,
                thread_id=thread_id,
                turn_id=turn_id,
            )
        except Exception as exc:
            raise ServiceError(409, f"Codex could not accept the additional instruction: {exc}") from exc
        if not accepted:
            raise ServiceError(409, "the active Codex turn is no longer steerable")
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            if row is None:
                raise ServiceError(404, "generation conversation not found")
            event = record_agent_event(
                session,
                row,
                USER_EVENT_TYPE,
                data={"content": _safe_text(value, 20_000), "steered": True, "client_message_id": client_message_id},
                turn_id=turn_id,
            )
            row.updated_at = utcnow()
            _persist_agent_audit(session, row)
            session.commit()
            return turn_id, event.sequence

    async def answer_input(
        self, session_id: str, request_id: str, client_response_id: str,
        answers: dict[str, dict[str, list[str]]],
    ) -> tuple[str, int]:
        """Resolve a persisted question exactly once, resuming or starting a turn."""

        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            item = session.get(AgentInputRequest, request_id)
            if row is None or item is None or item.session_id != session_id:
                raise ServiceError(404, "input request not found")
            if item.status == "resolved" and item.client_response_id == client_response_id:
                sequence = int(session.scalar(select(AgentEvent.sequence).where(
                    AgentEvent.session_id == session_id,
                    AgentEvent.event_type == "user_input.resolved",
                    AgentEvent.turn_id == item.turn_id,
                ).order_by(AgentEvent.sequence.desc()).limit(1)) or 0)
                return item.response_turn_id or item.turn_id, sequence
            pending = pending_input_request(session, session_id)
            if item.status != "pending" or pending is None or pending.id != request_id:
                raise ServiceError(409, "input request is no longer pending")
            questions = [GenerationInputQuestion.model_validate(value) for value in item.questions_json]
            if set(answers) != {q.id for q in questions}:
                raise ServiceError(422, "answers must match all requested question ids")
            normalized: dict[str, dict[str, list[str]]] = {}
            for question in questions:
                value = answers.get(question.id)
                choices = value.get("answers") if isinstance(value, dict) else None
                if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], str):
                    raise ServiceError(422, f"question {question.id} requires one answer")
                answer = choices[0].strip()
                if not answer or len(answer) > 4000:
                    raise ServiceError(422, f"question {question.id} answer must be 1–4000 characters")
                if question.options and answer not in {opt.label for opt in question.options} and not question.isOther:
                    raise ServiceError(422, f"question {question.id} answer is not an available option")
                normalized[question.id] = {"answers": [answer]}
            safe = _redact(normalized)
            safe_answers = safe if isinstance(safe, dict) else normalized
            if item.response_mode == "resume_turn":
                callback = getattr(self.adapter, "answer_input", None)
                if callback is None or self._turn_ids.get(session_id) != item.turn_id:
                    item.response_mode = "new_turn"
                    item.fallback_reason = "turn_disconnected"
                    row.status = AgentSessionStatus.AWAITING_USER.value
                    record_agent_event(session, row, "user_input.fallback", data=public_input_request(item), turn_id=item.turn_id)
                    session.commit()
                else:
                    # Persist before writing to the Codex transport. A failed write
                    # leaves an auditable answer and is recovered as a new turn.
                    item.answers_json = safe
                    item.client_response_id = client_response_id
                    item.answered_at = utcnow()
                    item.status = "resolved"
                    row.status = AgentSessionStatus.RUNNING.value
                    event = record_agent_event(session, row, "user_input.resolved", data=public_input_request(item), turn_id=item.turn_id)
                    session.commit()
                    try:
                        accepted = await callback(turn_id=item.turn_id, transport_request_id=str(item.transport_request_id), answers=safe_answers)
                    except Exception:
                        accepted = False
                    if accepted:
                        return item.turn_id, event.sequence
                    with self.session_factory() as recovery:
                        failed_item = recovery.get(AgentInputRequest, request_id)
                        failed_row = recovery.get(AgentSession, session_id)
                        if failed_item and failed_row:
                            failed_item.status = "pending"
                            failed_item.response_mode = "new_turn"
                            failed_item.fallback_reason = "turn_disconnected"
                            failed_row.status = AgentSessionStatus.AWAITING_USER.value
                            record_agent_event(recovery, failed_row, "user_input.fallback", data=public_input_request(failed_item), turn_id=failed_item.turn_id)
                            recovery.commit()
                    raise ServiceError(409, "original turn disconnected; submit the answer again to continue in a new turn")
            if item.response_mode == "new_turn" and row.status in ACTIVE_PLANNING_STATUSES:
                # A fallback card can be surfaced before the old provider turn
                # has emitted its terminal event. Stop that turn first, keep the
                # pending card, then retry this same idempotent answer path.
                session.commit()
                await self.cancel(session_id)
                return await self.answer_input(session_id, request_id, client_response_id, answers)
            if row.status in ACTIVE_PLANNING_STATUSES:
                raise ServiceError(409, "original turn is still active")
            content = "用户对规划澄清问题的回答：\n" + "\n".join(
                f"{q.question}\n回答：{safe_answers[q.id]['answers'][0]}" for q in questions
            )
            new_turn_id, _ = append_generation_message(
                session, row, content,
                context_asset_ids=(row.context_json or {}).get("seed_asset_ids", []),
                client_message_id=client_response_id,
            )
            item.status = "resolved"
            item.answers_json = safe
            item.client_response_id = client_response_id
            item.response_turn_id = new_turn_id
            item.answered_at = utcnow()
            synthetic_user = session.scalar(select(AgentEvent).where(
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == USER_EVENT_TYPE,
                AgentEvent.turn_id == new_turn_id,
            ).order_by(AgentEvent.sequence.desc()).limit(1))
            if synthetic_user is not None:
                synthetic_user.data_json = {
                    **dict(synthetic_user.data_json or {}),
                    "input_request_id": item.id,
                    "input_request_answer": True,
                }
            event = record_agent_event(session, row, "user_input.resolved", data=public_input_request(item), turn_id=item.turn_id)
            session.commit()
        self.submit(session_id, new_turn_id)
        return new_turn_id, event.sequence

    async def _run(self, session_id: str, turn_id: str, cancel_event: asyncio.Event) -> None:
        try:
            with self.session_factory() as session:
                row = session.get(AgentSession, session_id)
                if row is None:
                    return
                project = session.get(Project, row.project_id)
                if project is None:
                    return
                events = list(
                    session.scalars(
                        select(AgentEvent)
                        .where(AgentEvent.session_id == session_id)
                        .order_by(AgentEvent.sequence)
                    ).all()
                )
                messages = _messages_from_events(events)
                transcript = [{"type": e.event_type, "turn_id": e.turn_id, "data": e.data_json} for e in events if e.event_type in {"user.message", "assistant.message", "user_input.resolved", "conversation.confirmed"}]
                recovery_turn = session.get(PlanningTurn, turn_id)
                if recovery_turn and recovery_turn.status == "recovering":
                    messages = [{"role": "user", "content": "继续上次中断的规划。保留已回答问题和人工修改，不重复询问已确认要求。", "turn_id": turn_id}]
                context = dict(row.context_json or {})
                draft = dict(row.draft_json or {})
                work_dir = ProjectStore(project.root_path).workspace / "agent" / "sessions" / session_id
                work_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_json(work_dir / "conversation-history.json", transcript)
                atomic_write_json(work_dir / "planning-draft-schema.json", GenerationPlanningDraft.model_json_schema())
                context["recovering_turn"] = bool(recovery_turn and recovery_turn.status == "recovering")
                context["recovery_history"] = transcript[-80:]
                context["recovery_history_file"] = "conversation-history.json"
                context["confirmed_answers"] = [e["data"] for e in transcript if e["type"] == "user_input.resolved"]
                context["previous_batches"] = [
                    {"plan_id": batch.plan_id, "draft": batch.draft_json, "jobs": [
                        {"task_id": job.task_id, "status": job.status, "asset_id": (job.request_json or {}).get("asset_id"), "result_revision_id": job.result_revision_id}
                        for job in session.scalars(select(GenerationJob).where(GenerationJob.plan_id == batch.plan_id)).all()
                    ]}
                    for batch in session.scalars(select(ConversationBatch).where(ConversationBatch.session_id == row.id)).all()
                ]
                initial_draft_hash = row.draft_hash
                if cancel_event.is_set():
                    return
            if not hasattr(self.adapter, "plan_turn"):
                raise CodexUnavailable("configured adapter does not implement planning")
            # Older test/local adapters do not accept ``thread_id``; pass it
            # when supported without changing that compatibility contract.
            plan_kwargs: dict[str, Any] = {
                "messages": messages,
                "draft": draft,
                "budget": max(1, int(getattr(self.settings, "planning_agent_budget", 8))),
                "turn_id": turn_id,
                "work_dir": work_dir,
            }
            try:
                parameters = inspect.signature(self.adapter.plan_turn).parameters
            except (TypeError, ValueError):
                parameters = {}
            if "thread_id" in parameters:
                plan_kwargs["thread_id"] = None
                with self.session_factory() as read_session:
                    current = read_session.get(AgentSession, session_id)
                    if current is not None:
                        plan_kwargs["thread_id"] = current.thread_id
            if "model" in parameters:
                with self.session_factory() as read_session:
                    current = read_session.get(AgentSession, session_id)
                    plan_kwargs["model"] = current.agent_model if current is not None else None
            result = self.adapter.plan_turn(context, **plan_kwargs)
            saw_terminal = False
            saw_failure = False
            iterator = _iter_adapter_result(result).__aiter__()
            async def watched_events():
                while True:
                    next_event = asyncio.create_task(anext(iterator))
                    try:
                        while True:
                            done, _ = await asyncio.wait({next_event}, timeout=max(60, self.settings.codex_timeout_seconds * 4))
                            if done:
                                yield next_event.result()
                                break
                            with self.session_factory() as check:
                                current = check.get(AgentSession, session_id)
                                if current and pending_input_request(check, session_id):
                                    continue
                            raise CodexAdapterError("transport stalled: 规划长时间无响应，现场已保留")
                    except StopAsyncIteration:
                        return
                    finally:
                        if not next_event.done():
                            next_event.cancel()
                            await asyncio.gather(next_event, return_exceptions=True)
            async for raw in watched_events():
                if cancel_event.is_set():
                    return
                with self.session_factory() as session:
                    row = session.get(AgentSession, session_id)
                    if row is None or row.stop_reason == "user.cancelled":
                        return
                    normalized = _normalize_adapter_event(raw, turn_id=turn_id)
                    if normalized is None:
                        continue
                    event_type, payload = normalized
                    raw_thread_id = raw.get("thread_id") if isinstance(raw, dict) else None
                    event_thread_id = raw_thread_id or payload.get("thread_id")
                    if event_thread_id:
                        row.thread_id = str(event_thread_id)
                    if event_type == "user_input.requested":
                        raw_questions = payload.get("questions")
                        if not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3:
                            raise ServiceError(422, "interactive request requires 1–3 questions")
                        questions = [GenerationInputQuestion.model_validate(question) for question in raw_questions]
                        if len({question.id for question in questions}) != len(questions) or any(
                            question.isSecret or (question.options is not None and not 2 <= len(question.options) <= 3)
                            for question in questions
                        ):
                            raise ServiceError(422, "interactive request has unsafe or invalid questions")
                        if pending_input_request(session, session_id):
                            raise ServiceError(409, "conversation already has a pending input request")
                        mode = payload.get("response_mode") if payload.get("response_mode") == "resume_turn" else "new_turn"
                        request_item = AgentInputRequest(
                            session_id=row.id, turn_id=turn_id,
                            remote_thread_id=payload.get("remote_thread_id"),
                            remote_turn_id=payload.get("remote_turn_id"),
                            item_id=str(payload.get("item_id") or new_id()),
                            transport_request_id=payload.get("transport_request_id"),
                            response_mode=mode, status="pending",
                            questions_json=[question.model_dump(mode="json", exclude={"isSecret"}) for question in questions],
                            auto_resolution_ms=payload.get("auto_resolution_ms"),
                        )
                        session.add(request_item)
                        session.flush()
                        payload = public_input_request(request_item)
                        row.status = AgentSessionStatus.AWAITING_INPUT.value if mode == "resume_turn" else AgentSessionStatus.AWAITING_USER.value
                    if event_type == "user_input.fallback":
                        pending = pending_input_request(session, session_id)
                        if pending:
                            pending.response_mode = "new_turn"
                            pending.fallback_reason = str(payload.get("reason") or "turn_disconnected")[:100]
                            payload = public_input_request(pending)
                        row.status = AgentSessionStatus.AWAITING_USER.value
                        row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
                        record_agent_event(session, row, event_type, data=payload, turn_id=turn_id)
                        record_agent_event(session, row, "turn.failed", data=_planning_error("input transport lost", code="process_restarted"), turn_id=turn_id)
                        _persist_agent_audit(session, row)
                        session.commit()
                        return
                    if event_type == "draft.updated":
                        candidate = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload.get("value")
                        if isinstance(candidate, dict):
                            try:
                                project_row = session.get(Project, row.project_id)
                                if project_row is None:
                                    raise ServiceError(404, "project is missing")
                                # Staging locations are owned by the runner, not
                                # model-authored project delivery paths.
                                candidate = dict(candidate)
                                candidate["tasks"] = [{**task, "candidate_path": None} if isinstance(task, dict) else task for task in candidate.get("tasks", [])]
                                candidate["settings"] = dict(draft.get("settings") or {})
                                validated = validate_planning_draft(
                                    session,
                                    project_row,
                                    candidate,
                                    context_hash=row.context_hash,
                                )
                            except (ServiceError, ValidationError) as exc:
                                row.result_json = {**dict(row.result_json or {}), "rejected_proposal": candidate}
                                row.status = AgentSessionStatus.AWAITING_USER.value
                                row.stop_reason = "draft.invalid"
                                row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
                                row.updated_at = utcnow()
                                error = _planning_error(
                                    f"draft.invalid: {exc}",
                                    code="draft_invalid",
                                )
                                error.update(_draft_validation_diagnostic(exc))
                                row.result_json = {
                                    **dict(row.result_json or {}),
                                    "last_error": error,
                                }
                                record_agent_event(
                                    session,
                                    row,
                                    "turn.failed",
                                    data=error,
                                    turn_id=turn_id,
                                )
                                _persist_agent_audit(session, row)
                                session.commit()
                                return
                            if row.draft_hash != initial_draft_hash:
                                row.result_json = {**dict(row.result_json or {}), "pending_proposal": validated}
                                record_agent_event(session, row, "draft.conflict", data={"message": "你编辑了方案，Agent 新提案等待合并。", "draft": validated}, turn_id=turn_id)
                                session.commit()
                                continue
                            row.draft_json = validated
                            row.draft_hash = draft_hash(validated)
                            row.draft_version = int(row.draft_version or 0) + 1
                            payload = {
                                "draft": validated,
                                "draft_hash": row.draft_hash,
                                "draft_version": row.draft_version,
                            }
                            if not validated.get("tasks") and validated.get("questions") and not pending_input_request(session, session_id):
                                _fallback_input_request(session, row, reason="adapter_questions")
                    if event_type in {"turn.completed", "turn.failed"}:
                        saw_terminal = True
                    if event_type == "turn.failed":
                        saw_failure = True
                        error = _planning_error(payload.get("reason") or "agent turn failed")
                        payload = {**payload, **error}
                        row.result_json = {
                            **dict(row.result_json or {}),
                            "last_error": payload,
                        }
                    elif event_type == "usage.updated":
                        row.result_json = {
                            **dict(row.result_json or {}),
                            "usage": payload.get("usage", {}),
                        }
                    record_agent_event(session, row, event_type, data=payload, turn_id=turn_id)
                    if event_type == "turn.failed":
                        row.status = AgentSessionStatus.AWAITING_USER.value
                        row.stop_reason = _safe_text(payload.get("reason") or "agent turn failed", 500)
                    elif event_type == "agent.unavailable":
                        row.status = AgentSessionStatus.UNAVAILABLE.value
                        row.stop_reason = _safe_text(payload.get("reason") or "agent unavailable", 500)
                    # Raw deltas are already durable in SQLite. Do not rewrite the
                    # complete audit transcript on every token (quadratic IO).
                    if event_type not in {"assistant.delta", "reasoning.summary", "tool.progress"}:
                        _persist_agent_audit(session, row)
                    session.commit()
            with self.session_factory() as session:
                row = session.get(AgentSession, session_id)
                if row is None or cancel_event.is_set() or row.stop_reason == "user.cancelled":
                    return
                if saw_failure or row.status == AgentSessionStatus.UNAVAILABLE.value:
                    row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
                    row.updated_at = utcnow()
                    _persist_agent_audit(session, row)
                    session.commit()
                    return
                row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
                row.status = AgentSessionStatus.AWAITING_USER.value
                row.stop_reason = None
                row.diagnostic_reason = None
                result_json = dict(row.result_json or {})
                result_json.pop("last_error", None)
                row.result_json = result_json
                row.updated_at = utcnow()
                if not saw_terminal:
                    record_agent_event(session, row, "turn.completed", data={"draft_hash": row.draft_hash}, turn_id=turn_id)
                _persist_agent_audit(session, row)
                session.commit()
        except CodexUnavailable as exc:
            self._finish_unavailable(session_id, turn_id, str(exc))
        except (CodexAdapterError, ValidationError, ServiceError) as exc:
            self._finish_failed(session_id, turn_id, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # adapter failures must leave a manual path
            self._finish_failed(session_id, turn_id, f"{type(exc).__name__}: planning turn failed")
        finally:
            from .planning_runtime import finish_attempt
            with self.session_factory() as session:
                finish_attempt(session, session_id, turn_id)
                session.commit()
            self._tasks.pop(session_id, None)
            self._cancel_events.pop(session_id, None)
            self._turn_ids.pop(session_id, None)

    def _finish_unavailable(self, session_id: str, turn_id: str, reason: str) -> None:
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            if row is None:
                return
            row.status = AgentSessionStatus.UNAVAILABLE.value
            pending = pending_input_request(session, session_id)
            if pending and pending.response_mode == "resume_turn":
                pending.response_mode = "new_turn"
                pending.fallback_reason = "turn_disconnected"
                record_agent_event(session, row, "user_input.fallback", data=public_input_request(pending), turn_id=turn_id)
            row.stop_reason = _safe_text(reason, 500)
            row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
            row.updated_at = utcnow()
            error = _planning_error(reason)
            row.result_json = {**dict(row.result_json or {}), "last_error": error}
            record_agent_event(session, row, "agent.unavailable", data=error, turn_id=turn_id)
            record_agent_event(session, row, "turn.failed", data=error, turn_id=turn_id)
            _persist_agent_audit(session, row)
            session.commit()

    def _finish_failed(self, session_id: str, turn_id: str, reason: str) -> None:
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            if row is None:
                return
            row.status = AgentSessionStatus.AWAITING_USER.value
            pending = pending_input_request(session, session_id)
            if pending and pending.response_mode == "resume_turn":
                pending.response_mode = "new_turn"
                pending.fallback_reason = "turn_disconnected"
                record_agent_event(session, row, "user_input.fallback", data=public_input_request(pending), turn_id=turn_id)
            row.stop_reason = _safe_text(reason, 500)
            row.budget_used = min(int(row.budget_limit or 0), int(row.budget_used or 0) + 1)
            row.updated_at = utcnow()
            error = _planning_error(reason)
            row.result_json = {**dict(row.result_json or {}), "last_error": error}
            record_agent_event(session, row, "turn.failed", data=error, turn_id=turn_id)
            _persist_agent_audit(session, row)
            session.commit()


def create_generation_conversation(
    session: Session,
    payload: GenerationConversationCreate,
    *,
    adapter: PlanningCodexAdapter,
    planning_budget: int,
) -> AgentSession:
    project = require(session, Project, payload.project_id, "project")
    context, context_hash = build_planning_context(session, project, seed_asset_ids=payload.seed_asset_ids)
    defaults = context.get("provider_routes", {})
    draft = _default_draft(context_hash, payload.title, defaults)
    store = ProjectStore(project.root_path)
    session_id = new_id()
    work_dir = store.workspace / "agent" / "sessions" / session_id
    work_dir.mkdir(parents=True, exist_ok=True)
    context_path = _write_context_snapshot(store, session_id, 1, context)
    context_relative = relative_to_root(store.root, context_path)
    row = AgentSession(
        id=session_id,
        project_id=project.id,
        purpose=PLANNING_PURPOSE,
        title=(payload.title or "新资产生成任务").strip() or "新资产生成任务",
        adapter=str(getattr(adapter, "name", adapter.__class__.__name__)),
        adapter_version=str(getattr(adapter, "version", "unknown")),
        schema_version=PLANNING_SCHEMA_VERSION,
        status=AgentSessionStatus.CREATED.value,
        context_hash=context_hash,
        context_path=context_relative,
        context_json=context,
        draft_json=draft,
        draft_hash=draft_hash(draft),
        draft_version=1,
        sandbox_json={
            "read_only": True,
            "work_dir": f"workspace/agent/sessions/{session_id}",
            "seed_asset_ids": sorted(set(payload.seed_asset_ids)),
            "context_version": 2,
        },
        allowed_actions_json=[],
        writable_allowlist_json=[],
        budget_limit=int(planning_budget),
        budget_used=0,
        turn_count=0,
        agent_model=payload.agent_model.strip() if payload.agent_model and payload.agent_model.strip() else None,
    )
    session.add(row)
    session.flush()
    record_agent_event(session, row, "draft.updated", data={"draft": draft, "draft_hash": row.draft_hash, "draft_version": row.draft_version, "source": "system"})
    _persist_agent_audit(session, row)
    session.commit()
    return row


def list_generation_conversations(
    session: Session,
    project_id: str | None = None,
    *,
    include_archived: bool = False,
) -> list[AgentSession]:
    statement = select(AgentSession).where(AgentSession.purpose == PLANNING_PURPOSE)
    if project_id:
        statement = statement.where(AgentSession.project_id == project_id)
    if not include_archived:
        statement = statement.where(AgentSession.archived_at.is_(None))
    statement = statement.options(
        defer(AgentSession.context_json),
        defer(AgentSession.draft_json),
    )
    return list(session.scalars(statement.order_by(AgentSession.updated_at.desc())).all())


def update_generation_conversation_settings(
    session: Session,
    row: AgentSession,
    *,
    agent_model: str | None,
) -> AgentSession:
    if row.purpose != PLANNING_PURPOSE:
        raise ServiceError(409, "agent session is not a generation planning conversation")
    if row.status in ACTIVE_PLANNING_STATUSES:
        raise ServiceError(409, "wait for the current planning turn to finish before changing the Agent model")
    row.agent_model = agent_model.strip() if agent_model and agent_model.strip() else None
    row.updated_at = utcnow()
    record_agent_event(
        session,
        row,
        "conversation.settings.updated",
        data={"agent_model": row.agent_model},
    )
    _persist_agent_audit(session, row)
    session.commit()
    return row


def replace_generation_conversation_context(
    session: Session,
    row: AgentSession,
    *,
    seed_asset_ids: Iterable[str],
) -> AgentSession:
    if row.purpose != PLANNING_PURPOSE:
        raise ServiceError(409, "agent session is not a generation planning conversation")
    if row.status in ACTIVE_PLANNING_STATUSES:
        raise ServiceError(409, "wait for the current planning turn to finish before changing references")
    project = require(session, Project, row.project_id, "project")
    values = list(dict.fromkeys(str(item).strip() for item in seed_asset_ids if str(item).strip()))
    assets = {asset.id for asset in session.scalars(select(Asset).where(Asset.project_id == project.id)).all()}
    missing = sorted(set(values) - assets)
    if missing:
        raise ServiceError(422, f"context assets are not in this project: {', '.join(missing)}")
    _refresh_session_context(session, row, project, seed_asset_ids=values)
    row.updated_at = utcnow()
    _persist_agent_audit(session, row)
    session.commit()
    return row


def archive_generation_conversation(session: Session, row: AgentSession) -> AgentSession:
    if row.status in ACTIVE_PLANNING_STATUSES:
        raise ServiceError(409, "stop the active planning turn before archiving the conversation")
    row.archived_at = utcnow()
    row.updated_at = utcnow()
    _persist_agent_audit(session, row)
    session.commit()
    return row


def unarchive_generation_conversation(session: Session, row: AgentSession) -> AgentSession:
    row.archived_at = None
    row.updated_at = utcnow()
    _persist_agent_audit(session, row)
    session.commit()
    return row


def remove_generation_conversation_artifacts(session_id: str, project: Project) -> None:
    """Remove only this planning chat's audit/context files."""

    store = ProjectStore(project.root_path)
    session_dir = safe_join(store.root, f"workspace/agent/sessions/{session_id}")
    audit_file = safe_join(store.root, f"history/agent/sessions/{session_id}.json")
    event_dir = safe_join(store.root, f"history/agent/events/{session_id}")
    if session_dir.is_dir():
        shutil.rmtree(session_dir)
    if audit_file.is_file():
        audit_file.unlink()
    if event_dir.is_dir():
        shutil.rmtree(event_dir)


def get_generation_events(session: Session, session_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[AgentEvent]:
    return list(
        session.scalars(
            select(AgentEvent)
            .where(AgentEvent.session_id == session_id, AgentEvent.sequence > max(0, after_sequence))
            .order_by(AgentEvent.sequence)
            .limit(max(1, min(limit, 2_000)))
        ).all()
    )


def _refresh_session_context(
    session: Session,
    row: AgentSession,
    project: Project,
    *,
    seed_asset_ids: Iterable[str],
) -> bool:
    """Refresh a planning session's redacted context when project facts move."""

    seed_values = [str(value) for value in seed_asset_ids if value]
    context, context_hash = build_planning_context(
        session,
        project,
        seed_asset_ids=seed_values,
    )
    store = ProjectStore(project.root_path)
    context_changed = context_hash != row.context_hash or not row.context_json
    if not context_changed and _context_snapshot_is_compact(store, row.context_path):
        return False
    version = max(2, int(row.draft_version or 1) + 1)
    context_path = _context_snapshot_path(store, row.id, version)
    while context_path.exists() or _context_index_path(store, row.id, version).exists():
        version += 1
        context_path = _context_snapshot_path(store, row.id, version)
    context_path = _write_context_snapshot(store, row.id, version, context)
    row.context_json = context
    row.context_hash = context_hash
    row.context_path = relative_to_root(store.root, context_path)
    row.sandbox_json = {
        **dict(row.sandbox_json or {}),
        "seed_asset_ids": sorted(set(seed_values)),
        "context_version": 2,
    }
    if context_changed and isinstance(row.draft_json, dict):
        refreshed_draft = dict(row.draft_json)
        refreshed_draft["context_hash"] = context_hash
        row.draft_json = refreshed_draft
        row.draft_hash = draft_hash(refreshed_draft)
        row.draft_version = int(row.draft_version or 0) + 1
        record_agent_event(
            session,
            row,
            "draft.updated",
            data={
                "draft": refreshed_draft,
                "draft_hash": row.draft_hash,
                "draft_version": row.draft_version,
                "source": "context.refresh",
            },
        )
    return True


def ensure_generation_context_v2(session: Session, row: AgentSession) -> AgentSession:
    """Lazily migrate a legacy Context v1 session when it is opened."""

    if int((row.context_json or {}).get("context_version", 1)) >= 2:
        return row
    project = require(session, Project, row.project_id, "project")
    _refresh_session_context(
        session,
        row,
        project,
        seed_asset_ids=(row.context_json or {}).get("seed_asset_ids", []),
    )
    row.schema_version = PLANNING_SCHEMA_VERSION
    row.updated_at = utcnow()
    _persist_agent_audit(session, row)
    session.commit()
    return row


def find_generation_message(
    session: Session,
    row: AgentSession,
    client_message_id: str | None,
) -> tuple[str, int] | None:
    """Return an already-recorded user turn for idempotent message retries."""

    if not client_message_id:
        return None
    events = list(
        session.scalars(
            select(AgentEvent)
            .where(AgentEvent.session_id == row.id, AgentEvent.event_type == USER_EVENT_TYPE)
            .order_by(AgentEvent.sequence.desc())
        ).all()
    )
    for event in events:
        if (event.data_json or {}).get("client_message_id") == client_message_id and event.turn_id:
            return event.turn_id, event.sequence
    return None


def append_generation_message(
    session: Session,
    row: AgentSession,
    content: str,
    *,
    context_asset_ids: Iterable[str] = (),
    client_message_id: str | None = None,
) -> tuple[str, int]:
    if row.purpose != PLANNING_PURPOSE:
        raise ServiceError(409, "agent session is not a generation planning conversation")
    if row.status in ACTIVE_PLANNING_STATUSES:
        raise ServiceError(409, "generation planning session already has an active turn")
    if row.plan_id and row.status == AgentSessionStatus.COMPLETED.value:
        row.draft_json = {**dict(row.draft_json or {}), "tasks": [], "summary": "基于上一批结果继续规划", "questions": []}
        row.draft_hash = draft_hash(row.draft_json)
        row.draft_version += 1
        row.budget_used = 0
    if int(row.budget_used or 0) >= int(row.budget_limit or 0):
        raise ServiceError(429, "planning agent budget is exhausted; continue with manual editing or raise the limit")
    content = content.strip()
    if not content:
        raise ServiceError(422, "message content cannot be empty")
    project = require(session, Project, row.project_id, "project")
    requested = [str(item) for item in context_asset_ids if item]
    current = {asset.id for asset in session.scalars(select(Asset).where(Asset.project_id == project.id)).all()}
    missing = sorted(set(requested) - current)
    if missing:
        raise ServiceError(422, f"context assets are not in this project: {', '.join(missing)}")
    seed = set((row.context_json or {}).get("seed_asset_ids", [])) | set(requested)
    _refresh_session_context(session, row, project, seed_asset_ids=seed)
    turn_id = new_id()
    session.add(PlanningTurn(id=turn_id, session_id=row.id, input_json={"content": content}, context_hash=row.context_hash, draft_hash=row.draft_hash))
    if not row.turn_count and (not row.title or row.title == "新资产生成任务"):
        row.title = content[:60]
    record_agent_event(
        session,
        row,
        USER_EVENT_TYPE,
        data={"content": _safe_text(content, 20_000), "context_asset_ids": requested, "client_message_id": client_message_id},
        turn_id=turn_id,
    )
    from .planning_runtime import ensure_attempt
    session.flush()
    ensure_attempt(session, row.id, turn_id)
    row.turn_count = int(row.turn_count or 0) + 1
    row.status = AgentSessionStatus.RUNNING.value
    row.stop_reason = None
    result_json = dict(row.result_json or {})
    result_json.pop("last_error", None)
    row.result_json = result_json
    row.updated_at = utcnow()
    _persist_agent_audit(session, row)
    session.commit()
    return turn_id, int(session.scalar(select(AgentEvent.sequence).order_by(AgentEvent.sequence.desc())) or 0)


def update_generation_draft(
    session: Session,
    row: AgentSession,
    payload: GenerationConversationDraftUpdate,
) -> AgentSession:
    if row.purpose != PLANNING_PURPOSE:
        raise ServiceError(409, "agent session is not a generation planning conversation")
    if payload.base_hash != (row.draft_hash or draft_hash(row.draft_json or {})):
        raise ServiceError(409, "draft changed in another tab; reload the current plan")
    project = require(session, Project, row.project_id, "project")
    # Editing a draft does not regenerate its planning context. Provider catalog
    # refreshes and unrelated asset scans must not invalidate ordinary controls.
    # Validate live references/paths below; base_hash still protects concurrent edits.
    normalized = validate_planning_draft(session, project, payload.draft, context_hash=row.context_hash)
    row.draft_json = normalized
    row.draft_hash = draft_hash(normalized)
    row.draft_version = int(row.draft_version or 0) + 1
    row.updated_at = utcnow()
    record_agent_event(session, row, "draft.updated", data={"draft": normalized, "draft_hash": row.draft_hash, "draft_version": row.draft_version, "source": "user"})
    _persist_agent_audit(session, row)
    session.commit()
    return row


def _snapshot_catalog_files(store: ProjectStore) -> dict[Path, bytes]:
    """Capture catalog JSON files before a confirmation transaction.

    Fresh projects contain only ``relations.json`` (which is maintained by the
    relation index), so the snapshot must retain the catalog root separately;
    otherwise a failed first asset registration would leave a new descriptor on
    disk with no path to discover and remove.
    """

    return {
        path: path.read_bytes()
        for path in store.catalog.rglob("*.json")
        if path.name != "relations.json"
    }


def _restore_catalog_files(store: ProjectStore, snapshot: dict[Path, bytes]) -> None:
    current = {
        path
        for path in store.catalog.rglob("*.json")
        if path.name != "relations.json"
    }
    # The snapshot is intentionally only used for paths that may be touched by
    # new asset registration.  Restore known files and remove newly-created
    # collection files.
    for path, content in snapshot.items():
        atomic_write_bytes(path, content)
    known = set(snapshot)
    for path in current - known:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _materialize_new_asset(
    session: Session,
    project: Project,
    proposal: GenerationAssetProposal,
    *,
    task: GenerationTaskProposal,
    used_keys: set[str],
) -> tuple[Asset, str]:
    key = proposal.key or f"{task.kind.value}.{_slug(proposal.title or task.id)}"
    if key in used_keys:
        # The key was part of the user-visible, hashed draft.  Silently
        # changing it here would make the confirmed asset differ from what the
        # user approved; let the transaction fail and ask for a refreshed plan.
        raise ServiceError(409, f"asset key already exists: {key}")
    if not STABLE_KEY_RE.fullmatch(key):
        raise ServiceError(422, f"new asset key is invalid: {key}")
    if proposal.kind is None or not proposal.subtype or not proposal.title:
        raise ServiceError(422, f"new asset proposal for task {task.id} is incomplete")
    if session.scalar(select(Asset).where(Asset.project_id == project.id, Asset.key == key)) is not None:
        raise ServiceError(409, f"asset key already exists: {key}")
    store = ProjectStore(project.root_path)
    if proposal.schema_ref:
        try:
            schema = store.read_schema(proposal.schema_ref)
            if schema is None:
                raise StorageError(f"schema not found: {proposal.schema_ref}")
            jsonschema.validate(proposal.metadata, schema)
        except (StorageError, jsonschema.ValidationError, jsonschema.SchemaError) as exc:
            raise ServiceError(422, f"new asset metadata does not satisfy schema: {exc}") from exc
    asset = Asset(
        id=new_id(),
        project_id=project.id,
        key=key,
        kind=proposal.kind.value,
        subtype=proposal.subtype,
        title=proposal.title,
        schema_ref=proposal.schema_ref,
        tags=list(proposal.tags or []),
        metadata_json=dict(proposal.metadata or {}),
    )
    session.add(asset)
    session.flush()
    store.write_asset(asset_descriptor(asset))
    used_keys.add(key)
    return asset, key


def _reference_path_for_task(session: Session, project: Project, reference: GenerationReferenceProposal) -> str | None:
    _asset, revision, rendition = _reference_rows(session, project.id, reference)
    if rendition is None:
        return None
    path = rendition.normalized_path or rendition.source_path
    return _relative_or_none(ProjectStore(project.root_path).root, path)


def _plan_payload_from_draft(
    session: Session,
    project: Project,
    draft: GenerationPlanningDraft,
) -> tuple[GenerationPlanCreate, list[Asset]]:
    used_keys = {asset.key for asset in session.scalars(select(Asset).where(Asset.project_id == project.id)).all()}
    created: list[Asset] = []
    tasks: list[GenerationTask] = []
    for proposal in draft.tasks:
        if proposal.asset.mode == "new":
            asset, _key = _materialize_new_asset(session, project, proposal.asset, task=proposal, used_keys=used_keys)
            created.append(asset)
        else:
            if not proposal.asset.asset_id:
                raise ServiceError(422, f"task {proposal.id} has no asset")
            asset = session.get(Asset, proposal.asset.asset_id)
            if asset is None or asset.project_id != project.id:
                raise ServiceError(422, f"task {proposal.id} references an asset outside this project")

        references_data: list[dict[str, Any]] = []
        primary_path: str | None = None
        for reference in proposal.references:
            path = _reference_path_for_task(session, project, reference)
            fixed = reference.model_dump(mode="json")
            fixed["path"] = path
            references_data.append(fixed)
            if reference.role == "primary" and path:
                primary_path = path
        task_data: dict[str, Any] = {
            "id": proposal.id,
            "kind": proposal.kind.value,
            "asset_id": asset.id,
            "prompt": proposal.prompt,
            "provider_profile_id": proposal.provider_profile_id,
            "model": proposal.model,
            "schema": proposal.output_schema,
            "depends_on": list(proposal.depends_on),
            "width": proposal.width,
            "height": proposal.height,
            "max_bytes": proposal.max_bytes,
            "transparent": proposal.transparent,
            "reference_path": primary_path,
            "reference_task_id": proposal.reference_task_id,
            "target_path": proposal.target_path,
            "metadata": {
                **dict(proposal.metadata or {}),
                "planning_references": references_data,
                "candidate_path": proposal.candidate_path or f"workspace/candidates/pending/{proposal.id}",
            },
        }
        # Pydantic removes nulls in the final JSON while preserving explicit
        # empty lists expected by the Runner.
        tasks.append(GenerationTask.model_validate(task_data))
    settings = dict(draft.settings or {})
    plan_payload = GenerationPlanCreate(
        project_id=project.id,
        provider_profile_id=settings.get("provider_profile_id"),
        name=draft.title,
        tasks=tasks,
        extra_call_budget=settings.get("extra_call_budget"),
        max_paid_remediation_rounds=int(settings.get("max_paid_remediation_rounds", 2)),
        max_transport_retries=int(settings.get("max_transport_retries", 2)),
        max_concurrency=int(settings.get("max_concurrency", 3)),
    )
    return plan_payload, created


def _asset_read_list(assets: Iterable[Asset]) -> list[Any]:
    from .domain import AssetRead

    return [AssetRead.model_validate(asset) for asset in assets]


def confirm_generation_conversation(
    session: Session,
    row: AgentSession,
    payload: GenerationConversationConfirm,
    *,
    available_provider_ids: set[str],
) -> dict[str, Any]:
    if row.purpose != PLANNING_PURPOSE:
        raise ServiceError(409, "agent session is not a generation planning conversation")
    previous = session.scalar(select(ConversationBatch).where(ConversationBatch.session_id == row.id, ConversationBatch.draft_hash == payload.draft_hash))
    if previous or (row.plan_id and row.status == AgentSessionStatus.COMPLETED.value and payload.draft_hash == row.draft_hash):
        plan = session.get(GenerationPlan, previous.plan_id if previous else row.plan_id)
        if plan is None:
            raise ServiceError(409, "conversation points to a missing plan")
        jobs = list(session.scalars(select(GenerationJob).where(GenerationJob.plan_id == plan.id)).all())
        batch_events = session.scalars(select(AgentEvent).where(AgentEvent.session_id == row.id, AgentEvent.event_type == "conversation.confirmed")).all()
        created_ids = next((list((event.data_json or {}).get("created_asset_ids", [])) for event in batch_events if (event.data_json or {}).get("plan_id") == plan.id), [])
        created = [asset for asset in (session.get(Asset, item) for item in created_ids) if asset is not None]
        return {"conversation": row, "plan": plan, "jobs": jobs, "created_assets": _asset_read_list(created)}
    if row.status in ACTIVE_PLANNING_STATUSES:
        raise ServiceError(409, "wait for the current planning turn to finish")
    if payload.context_hash and payload.context_hash != row.context_hash:
        raise ServiceError(409, "generation planning context is stale; refresh before confirmation")
    if payload.draft_hash != (row.draft_hash or draft_hash(row.draft_json or {})):
        raise ServiceError(409, "draft hash is stale; reload the current plan")
    project = require(session, Project, row.project_id, "project")
    context, context_hash = build_planning_context(
        session,
        project,
        seed_asset_ids=(row.context_json or {}).get("seed_asset_ids", []),
    )
    if context_hash != row.context_hash:
        raise ServiceError(409, "project context changed while planning; refresh before confirmation")
    normalized = validate_planning_draft(
        session,
        project,
        row.draft_json or {},
        context_hash=context_hash,
        for_confirm=True,
    )
    draft = GenerationPlanningDraft.model_validate(normalized)
    warnings = {item.code: item for item in draft.warnings}
    unaccepted = sorted(code for code in warnings if code not in set(payload.accepted_warning_codes))
    if unaccepted:
        raise ServiceError(422, f"confirmation requires accepting warnings: {', '.join(unaccepted)}")
    if not draft.tasks:
        raise ServiceError(422, "at least one generation task is required")

    store = ProjectStore(project.root_path)
    catalog_snapshot = _snapshot_catalog_files(store)
    try:
        # Catalog writes acquire the ProjectStore lock individually.  Keeping a
        # second ``flock`` open here would deadlock on macOS; the database flush
        # and final commit still happen as one transaction, with the catalog
        # snapshot restored on any failure.
        plan_payload, created = _plan_payload_from_draft(session, project, draft)
        plan = create_plan(session, plan_payload, commit=False)
        session.flush()
        jobs = confirm_plan(
            session,
            plan,
            available_provider_ids=available_provider_ids,
            commit=False,
        )
        session.add(ConversationBatch(session_id=row.id, plan_id=plan.id, draft_hash=payload.draft_hash, draft_version=row.draft_version, draft_json=normalized))
        row.plan_id = plan.id
        row.draft_json = normalized
        row.draft_hash = draft_hash(normalized)
        row.status = AgentSessionStatus.COMPLETED.value
        row.stop_reason = None
        row.completed_at = utcnow()
        row.updated_at = utcnow()
        row.result_json = {
            **dict(row.result_json or {}),
            "created_asset_ids": [asset.id for asset in created],
            "plan_id": plan.id,
            "confirmed_at": utcnow().isoformat(),
        }
        record_agent_event(
            session,
            row,
            "conversation.confirmed",
            data={"plan_id": plan.id, "created_asset_ids": [asset.id for asset in created]},
        )
        session.flush()
        session.commit()
    except Exception:
        session.rollback()
        _restore_catalog_files(store, catalog_snapshot)
        raise
    # Audit files are written after the transaction so a DB rollback cannot
    # leave a durable conversation claiming a plan was created.
    try:
        _persist_agent_audit(session, row)
    except (OSError, StorageError):
        # The database remains authoritative; the next project scan can rebuild
        # the audit projection.  Do not undo a confirmed plan for an audit-file
        # failure.
        pass
    return {"conversation": row, "plan": plan, "jobs": jobs, "created_assets": _asset_read_list(created)}


async def stream_generation_events(
    database: Any,
    session_id: str,
    request: Any,
    *,
    after_sequence: int = 0,
) -> AsyncIterable[str]:
    """Yield durable AgentEvent rows as SSE, resuming from a sequence cursor."""

    cursor = max(0, int(after_sequence or 0))
    last_keepalive = asyncio.get_running_loop().time()
    while not await request.is_disconnected():
        with database.sessions() as session:
            row = session.get(AgentSession, session_id)
            if row is None:
                return
            events = get_generation_events(session, session_id, after_sequence=cursor)
            for event in events:
                cursor = max(cursor, event.sequence)
                payload = __import__("json").dumps(_event_payload(event), ensure_ascii=False, separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
        now = asyncio.get_running_loop().time()
        if now - last_keepalive >= 15:
            yield ": keepalive\n\n"
            last_keepalive = now
        await asyncio.sleep(0.2)
