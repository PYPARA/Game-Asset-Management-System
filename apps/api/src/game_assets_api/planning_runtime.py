"""Persistent planning queue. API lifetimes never own production planning tasks."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update

from .agent import record_agent_event
from .models import AgentSession, AgentEvent, PlanningTurn, PlanningAttempt, PlanningCommand, ConversationBatch, GenerationPlan, new_id, utcnow
from .services import ServiceError


def backfill_planning(session: Any) -> None:
    for row in session.scalars(select(AgentSession).where(AgentSession.purpose == "generation_planning")).all():
        for event in session.scalars(select(AgentEvent).where(AgentEvent.session_id == row.id, AgentEvent.event_type == "user.message").order_by(AgentEvent.sequence)).all():
            if (not row.title or row.title == "新资产生成任务") and (event.data_json or {}).get("content"):
                row.title = str(event.data_json["content"])[:60]
            if event.turn_id and not session.get(PlanningTurn, event.turn_id):
                session.add(PlanningTurn(id=event.turn_id, session_id=row.id, status="completed", input_json=event.data_json, context_hash=row.context_hash, draft_hash=row.draft_hash, created_at=event.created_at))
                session.flush()
        if row.plan_id and not session.scalar(select(ConversationBatch).where(ConversationBatch.plan_id == row.plan_id)):
            session.add(ConversationBatch(session_id=row.id, plan_id=row.plan_id, draft_hash=row.draft_hash or row.plan_id, draft_version=row.draft_version, draft_json=row.draft_json, created_at=row.completed_at or row.updated_at))
    session.flush()


def ensure_attempt(session: Any, session_id: str, turn_id: str) -> PlanningAttempt:
    turn = session.get(PlanningTurn, turn_id)
    if not turn:
        row = session.get(AgentSession, session_id)
        turn = PlanningTurn(id=turn_id, session_id=session_id, context_hash=row.context_hash, draft_hash=row.draft_hash)
        session.add(turn)
        session.flush()
    active = session.scalar(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn_id, PlanningAttempt.status.in_(["queued", "running"])))
    if active:
        return active
    prior = session.scalar(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn_id))
    if prior:
        return prior
    attempt = PlanningAttempt(turn_id=turn_id, request_key="initial:" + turn_id)
    session.add(attempt)
    session.flush()
    return attempt


def recover_turn(session: Any, row: AgentSession, turn_id: str, key: str) -> PlanningAttempt:
    session.flush()
    previous = session.scalar(select(PlanningAttempt).where(PlanningAttempt.request_key == row.id + ":" + key))
    if previous:
        if previous.turn_id != turn_id:
            raise ServiceError(409, "恢复请求标识已用于其他回合")
        return previous
    turn = session.get(PlanningTurn, turn_id)
    if not turn or turn.session_id != row.id:
        raise ServiceError(404, "找不到需要恢复的回合")
    latest = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == row.id).order_by(PlanningTurn.created_at.desc()))
    if latest and latest.id != turn_id:
        raise ServiceError(409, "已有后续对话，请继续最新回合")
    active = session.scalar(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn_id, PlanningAttempt.status.in_(["queued", "running"])))
    if active:
        return active
    if turn.status == "completed" and row.status == "completed" and not row.stop_reason:
        raise ServiceError(409, "该回合已完成，请发送新的指令继续")
    turn.status = "recovering"
    row.status = "running"
    row.stop_reason = None
    row.result_json = {k: v for k, v in dict(row.result_json or {}).items() if k != "last_error"}
    attempt = PlanningAttempt(turn_id=turn_id, request_key=row.id + ":" + key)
    session.add(attempt)
    record_agent_event(session, row, "turn.recovering", data={"message": "正在恢复刚才的规划；对话、已回答问题和方案已保留。"}, turn_id=turn_id)
    session.flush()
    return attempt


def finish_attempt(session: Any, session_id: str, turn_id: str) -> None:
    row = session.get(AgentSession, session_id)
    turn = session.get(PlanningTurn, turn_id)
    if not row or not turn:
        return
    status = "cancelled" if row.stop_reason == "user.cancelled" else "failed" if row.stop_reason else "completed"
    # Cancellation during worker shutdown must leave a recoverable lease.
    if row.status in {"running", "awaiting_input"} and not row.stop_reason:
        return
    turn.status = status
    for attempt in session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn_id, PlanningAttempt.status.in_(["queued", "running"]))).all():
        attempt.status = status
        attempt.error_json = dict((row.result_json or {}).get("last_error") or {})
        attempt.lease_expires_at = None


def runtime_detail(session: Any, row: AgentSession) -> dict[str, Any]:
    if session is None:
        return {}
    turn = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == row.id).order_by(PlanningTurn.created_at.desc()))
    event = session.scalar(select(AgentEvent).where(AgentEvent.session_id == row.id).order_by(AgentEvent.sequence.desc()))
    batches = []
    for batch in session.scalars(select(ConversationBatch).where(ConversationBatch.session_id == row.id).order_by(ConversationBatch.created_at)).all():
        plan = session.get(GenerationPlan, batch.plan_id)
        batches.append({"id": batch.id, "plan_id": batch.plan_id, "draft_hash": batch.draft_hash, "draft_version": batch.draft_version, "status": plan.status if plan else "missing", "created_at": batch.created_at.isoformat(), "draft": batch.draft_json})
    return {"current_turn_id": turn.id if turn else None, "recovery_status": turn.status if turn else None, "last_sequence": event.sequence if event else 0, "batches": batches, "pending_proposal": (row.result_json or {}).get("pending_proposal")}


class PlanningQueueClient:
    def __init__(self, session_factory: Any, adapter: Any, settings: Any):
        self.session_factory, self.adapter, self.settings = session_factory, adapter, settings

    async def start(self) -> None:
        with self.session_factory() as session:
            backfill_planning(session)
            session.commit()

    async def stop(self) -> None:
        pass  # The worker owns leases and transports, not this API instance.

    def submit(self, session_id: str, turn_id: str) -> None:
        with self.session_factory() as session:
            ensure_attempt(session, session_id, turn_id)
            session.commit()

    async def command(self, session_id: str, kind: str, payload: dict, key: str | None = None) -> Any:
        command_id = session_id + ":" + (key or new_id())
        with self.session_factory() as session:
            command = session.get(PlanningCommand, command_id)
            if not command:
                session.add(PlanningCommand(id=command_id, session_id=session_id, kind=kind, payload_json=payload))
                session.commit()
            elif command.kind != kind or command.payload_json != payload:
                raise ServiceError(409, "操作标识已用于不同请求")
        for _ in range(150):
            with self.session_factory() as session:
                command = session.get(PlanningCommand, command_id)
                if command.status == "completed":
                    return command.result_json["value"]
                if command.status == "failed":
                    raise ServiceError(command.result_json.get("code", 409), command.result_json.get("message", "操作失败"))
            await asyncio.sleep(.1)
        raise ServiceError(503, "规划 worker 尚未响应，操作已保存；重试会查询同一操作。")

    async def cancel(self, session_id: str) -> bool:
        with self.session_factory() as session:
            row = session.get(AgentSession, session_id)
            turn = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == session_id).order_by(PlanningTurn.created_at.desc()))
            if turn:
                queued = session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn.id, PlanningAttempt.status == "queued")).all()
                if queued:
                    for attempt in queued:
                        attempt.status = "cancelled"
                    turn.status = "cancelled"
                    row.status, row.stop_reason = "awaiting_user", "user.cancelled"
                    record_agent_event(session, row, "turn.failed", data={"message": "本轮已停止，对话与草案已保留。", "retryable": True, "error_code": "interrupted"}, turn_id=turn.id)
                    session.commit()
                    return True
        return await self.command(session_id, "cancel", {})

    async def steer(self, session_id: str, content: str, client_message_id: str | None = None) -> tuple[str, int]:
        return tuple(await self.command(session_id, "steer", {"content": content, "client_message_id": client_message_id}, client_message_id))

    async def answer_input(self, session_id: str, request_id: str, client_response_id: str, answers: dict) -> tuple[str, int]:
        return tuple(await self.command(session_id, "answer_input", {"request_id": request_id, "client_response_id": client_response_id, "answers": answers}, client_response_id))


async def run_worker(settings: Any) -> None:
    from .database import Database
    from .codex_adapter import build_codex_adapter
    from .generation_planning import GenerationPlanningRunner, pending_input_request, public_input_request
    import fcntl
    database = Database(settings)
    database.create_schema()
    # One local worker per state directory; claims use conditional DB updates.
    lock = (settings.state_dir / "planning-worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(75)  # Another local supervisor owns the worker.
    adapter = build_codex_adapter(settings.codex_command, codex_bin=settings.codex_bin, timeout_seconds=settings.codex_timeout_seconds)
    from .codex_adapter import AppServerCodexAdapter
    from .planning_provider import ProviderPlanningAdapter
    if isinstance(adapter, AppServerCodexAdapter):
        adapter = ProviderPlanningAdapter(adapter, database.sessions, settings)
    runner = GenerationPlanningRunner(database.sessions, adapter, settings)
    owner = new_id()
    with database.sessions() as session:
        backfill_planning(session)
        # Owning the OS lock proves the former local worker is gone.
        for attempt in session.scalars(select(PlanningAttempt).where(PlanningAttempt.status == "running")).all():
            attempt.lease_expires_at = utcnow()
        for command in session.scalars(select(PlanningCommand).where(PlanningCommand.status == "running")).all():
            command.status = "queued"  # Answers are idempotent at the runner boundary.
        for row in session.scalars(select(AgentSession).where(AgentSession.purpose == "generation_planning", AgentSession.status.in_(["running", "awaiting_input"]))).all():
            turn = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == row.id).order_by(PlanningTurn.created_at.desc()))
            if turn and not session.scalar(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn.id)):
                ensure_attempt(session, row.id, turn.id)
                turn.status = "recovering"
        session.commit()
    try:
        while True:
            with database.sessions() as session:
                for attempt in session.scalars(select(PlanningAttempt).where(PlanningAttempt.status == "running", PlanningAttempt.lease_expires_at < utcnow())).all():
                    turn = session.get(PlanningTurn, attempt.turn_id)
                    row = session.get(AgentSession, turn.session_id)
                    attempt.status = "failed"
                    attempt.error_json = {"error_code": "process_restarted"}
                    session.flush()
                    pending = pending_input_request(session, row.id)
                    if pending:
                        pending.response_mode = "new_turn"
                        pending.fallback_reason = "process_restarted"
                        row.status = "awaiting_user"
                        record_agent_event(session, row, "user_input.fallback", data=public_input_request(pending), turn_id=turn.id)
                    elif row.stop_reason != "user.cancelled":
                        attempts = session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn.id)).all()
                        if len(attempts) < 4:
                            recover_turn(session, row, turn.id, "auto-" + new_id())
                        else:
                            row.status = "awaiting_user"
                            row.stop_reason = "recovery.exhausted"
                            record_agent_event(session, row, "turn.failed", data={"message": "自动恢复未成功，现场已保存，请稍后继续规划。", "retryable": True}, turn_id=turn.id)
                session.commit()
                for turn in session.scalars(select(PlanningTurn).where(PlanningTurn.status == "failed")).all():
                    row = session.get(AgentSession, turn.session_id)
                    attempts = session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id == turn.id).order_by(PlanningAttempt.created_at)).all()
                    code = (row.result_json or {}).get("last_error", {}).get("error_code")
                    if row.stop_reason != "user.cancelled" and not pending_input_request(session, row.id) and attempts and len(attempts) < 4 and code in {"overloaded", "process_restarted", "connection_lost"}:
                        latest = session.scalar(select(PlanningTurn).where(PlanningTurn.session_id == row.id).order_by(PlanningTurn.created_at.desc()))
                        if latest.id == turn.id:
                            recover_turn(session, row, turn.id, "auto-" + new_id())
                session.commit()
                queued = session.scalars(select(PlanningAttempt).where(PlanningAttempt.status == "queued").order_by(PlanningAttempt.created_at)).all()
                for attempt in queued:
                    turn = session.get(PlanningTurn, attempt.turn_id)
                    if turn.session_id in runner._tasks:
                        continue
                    changed = session.execute(update(PlanningAttempt).where(PlanningAttempt.id == attempt.id, PlanningAttempt.status == "queued").values(status="running", lease_owner=owner, heartbeat_at=utcnow(), lease_expires_at=utcnow() + timedelta(seconds=30)))
                    session.commit()
                    if changed.rowcount:
                        runner.submit(turn.session_id, turn.id)
                for attempt in session.scalars(select(PlanningAttempt).where(PlanningAttempt.status == "running", PlanningAttempt.lease_owner == owner)).all():
                    attempt.heartbeat_at = utcnow()
                    attempt.lease_expires_at = utcnow() + timedelta(seconds=30)
                commands = session.scalars(select(PlanningCommand).where(PlanningCommand.status == "queued").order_by(PlanningCommand.created_at)).all()
                for command in commands:
                    command.status = "running"
                session.commit()
            for command in commands:
                try:
                    method = getattr(runner, command.kind)
                    value = await method(command.session_id, **command.payload_json)
                    result, state = {"value": value}, "completed"
                except Exception as exc:
                    result, state = {"message": str(exc), "code": getattr(exc, "status_code", 409)}, "failed"
                with database.sessions() as session:
                    item = session.get(PlanningCommand, command.id)
                    item.status, item.result_json = state, result
                    session.commit()
            await asyncio.sleep(.2)
    finally:
        await runner.stop()
        close = getattr(adapter, "close", None)
        if close:
            try:
                await asyncio.wait_for(close(), timeout=5)
            except (TimeoutError, Exception):
                pass
        lock.close()
