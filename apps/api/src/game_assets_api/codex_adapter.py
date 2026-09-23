"""Isolated, read-only Codex planning boundary."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Protocol


class CodexAdapterError(RuntimeError):
    """Base error raised by the isolated adapter."""


class CodexUnavailable(CodexAdapterError):
    pass


class CodexProtocolError(CodexAdapterError):
    pass


class CodexAdapter(Protocol):
    name: str
    version: str

    async def diagnose(
        self,
        context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]: ...

    async def capabilities(self) -> dict[str, Any]: ...


class PlanningCodexAdapter(Protocol):
    """The deliberately small, read-only planning surface.

    Planning is kept separate from ``diagnose`` so an adapter update cannot
    accidentally change the M4 diagnosis contract.  Implementations may return
    a normal object, an NDJSON string, or an async iterable of event objects.
    The planning runner normalizes those forms into the public event protocol.
    """

    async def plan_turn(
        self,
        context: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        budget: int,
        turn_id: str,
        model: str | None = None,
        thread_id: str | None = None,
        work_dir: Path | None = None,
    ) -> Any: ...

    async def steer(
        self,
        *,
        content: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool: ...

    async def interrupt(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool: ...


@dataclass(slots=True)
class UnavailableCodexAdapter:
    name: str = "unavailable"
    version: str = "none"

    async def diagnose(
        self,
        _context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        raise CodexUnavailable("Codex SDK/App Server is not configured")

    async def plan_turn(
        self,
        _context: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        budget: int,
        turn_id: str,
        model: str | None = None,
        thread_id: str | None = None,
        work_dir: Path | None = None,
    ) -> Any:
        raise CodexUnavailable("Codex SDK/App Server is not configured")

    async def capabilities(self) -> dict[str, Any]:
        return {
            "available": False,
            "adapter": self.name,
            "version": self.version,
            "models": [],
            "diagnostic": {
                "code": "codex_unavailable",
                "message": "本机没有可用的 Codex App Server。",
                "hint": "安装并登录 Codex，或设置 GAME_ASSETS_CODEX_BIN；也可以继续使用右侧人工编排。",
            },
        }

    async def steer(
        self,
        *,
        content: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        return False

    async def interrupt(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        return False


@dataclass(slots=True)
class JsonCommandCodexAdapter:
    """Run a pinned local stdio adapter with a JSON-in/JSON-out contract.

    The subprocess receives only the redacted context and a small protocol
    envelope.  It runs from a disposable workspace and never receives API keys,
    the database URL, or a write token for the project.
    """

    command: tuple[str, ...]
    timeout_seconds: float = 45.0
    name: str = "codex-stdio"
    version: str = "json-stdio-v1"

    async def diagnose(
        self,
        context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        if not self.command:
            raise CodexUnavailable("Codex command is empty")
        cwd = work_dir if work_dir and work_dir.is_dir() else None
        # Do not forward the parent process' secrets to an Agent command.  The
        # command may use its own OS-level login managed by the Codex runtime.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
            and not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET"))
        }
        env["GAMS_AGENT_READ_ONLY"] = "1"
        payload = json.dumps(
            {"protocol_version": 1, "budget": budget, "context": context},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise CodexUnavailable(f"unable to start Codex adapter: {exc}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(payload), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CodexUnavailable("Codex adapter timed out") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", "replace").strip()[:500]
            raise CodexUnavailable(
                f"Codex adapter exited with status {process.returncode}"
                + (f": {detail}" if detail else "")
            )
        try:
            value = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexProtocolError("Codex adapter did not return JSON") from exc
        if not isinstance(value, dict):
            raise CodexProtocolError("Codex adapter response must be an object")
        return value

    async def plan_turn(
        self,
        context: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        budget: int,
        turn_id: str,
        model: str | None = None,
        thread_id: str | None = None,
        work_dir: Path | None = None,
    ) -> Any:
        """Run a planning turn using protocol v2.

        The command is intentionally given the same redacted context package as
        diagnosis plus the visible conversation and current editable draft.  It
        never receives a project write token or credentials.  Both a single JSON
        response and newline-delimited JSON are accepted to make the boundary
        usable by the Codex App Server and by small local test adapters.
        """

        if not self.command:
            raise CodexUnavailable("Codex command is empty")
        cwd = work_dir if work_dir and work_dir.is_dir() else None
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "LANG", "LC_ALL", "TMPDIR"}
            and not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET"))
        }
        env["GAMS_AGENT_READ_ONLY"] = "1"
        payload = json.dumps(
            {
                "protocol_version": 2,
                "purpose": "generation_planning",
                "budget": budget,
                "turn_id": turn_id,
                "thread_id": thread_id,
                "context": context,
                "messages": messages,
                "draft": draft,
                "capabilities": {
                    "read_only": True,
                    "events": [
                        "assistant.delta",
                        "assistant.message",
                        "tool.started",
                        "tool.result",
                        "draft.updated",
                        "turn.completed",
                        "turn.failed",
                        "agent.unavailable",
                    ],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except (OSError, ValueError) as exc:
            raise CodexUnavailable(f"unable to start Codex adapter: {exc}") from exc
        try:
            if process.stdin is None:
                raise CodexUnavailable("Codex planning adapter stdin is unavailable")
            process.stdin.write(payload)
            await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            process.kill()
            await process.wait()
            raise CodexUnavailable("Codex planning adapter closed stdin") from exc

        async def stream() -> AsyncIterator[dict[str, Any]]:
            stderr_task = asyncio.create_task(process.stderr.read() if process.stderr else asyncio.sleep(0, result=b""))
            lines: list[str] = []
            streamed = False
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    while process.stdout is not None:
                        raw_line = await process.stdout.readline()
                        if not raw_line:
                            break
                        try:
                            line = raw_line.decode("utf-8")
                        except UnicodeDecodeError as exc:
                            raise CodexProtocolError("Codex planning adapter returned non-UTF-8 data") from exc
                        lines.append(line)
                        stripped_line = line.strip()
                        if not stripped_line:
                            continue
                        try:
                            value = json.loads(stripped_line)
                        except json.JSONDecodeError:
                            # Pretty-printed single JSON is parsed after EOF;
                            # once a valid NDJSON frame has been emitted,
                            # malformed subsequent lines are a protocol error.
                            if streamed:
                                raise CodexProtocolError("Codex planning adapter returned invalid NDJSON")
                            continue
                        if isinstance(value, dict):
                            streamed = True
                            yield value
                        elif isinstance(value, list):
                            streamed = True
                            for item in value:
                                if not isinstance(item, dict):
                                    raise CodexProtocolError("Codex planning event list must contain objects")
                                yield item
                        else:
                            if streamed:
                                raise CodexProtocolError("Codex planning events must be objects")
                    await process.wait()
                stderr = await stderr_task
            except TimeoutError as exc:
                process.kill()
                await process.wait()
                await stderr_task
                raise CodexUnavailable("Codex planning adapter timed out") from exc
            except asyncio.CancelledError:
                process.kill()
                await process.wait()
                await stderr_task
                raise
            except CodexAdapterError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                await stderr_task
                raise
            if process.returncode != 0:
                detail = stderr.decode("utf-8", "replace").strip()[:500]
                raise CodexUnavailable(
                    f"Codex planning adapter exited with status {process.returncode}"
                    + (f": {detail}" if detail else "")
                )
            if streamed:
                return
            text = "".join(lines).strip()
            if not text:
                raise CodexProtocolError("Codex planning adapter returned an empty response")
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CodexProtocolError("Codex planning adapter returned invalid NDJSON") from exc
            if isinstance(value, dict):
                yield value
            elif isinstance(value, list) and all(isinstance(item, dict) for item in value):
                for item in value:
                    yield item
            else:
                raise CodexProtocolError("Codex planning adapter response must contain objects")

        return stream()

    async def capabilities(self) -> dict[str, Any]:
        return {
            "available": True,
            "adapter": self.name,
            "version": self.version,
            "models": [],
            "diagnostic": {
                "code": "custom_adapter",
                "message": "已使用自定义规划适配器。",
                "hint": "模型列表由自定义适配器管理。",
            },
        }


@dataclass(slots=True)
class _ActiveAppServerTurn:
    process: asyncio.subprocess.Process
    thread_id: str | None = None
    remote_turn_id: str | None = None
    pending_request_id: int | str | None = None
    pending_item_id: str | None = None
    pending_auto_resolution_ms: int | None = None


@dataclass(slots=True)
class AppServerCodexAdapter:
    """A small, read-only JSONL bridge to the local ``codex app-server``.

    The app server is deliberately started per operation.  The durable Codex
    thread id lives in ``AgentSession``; a later turn resumes that thread while
    keeping the API process free from a long-lived provider connection.
    """

    command: tuple[str, ...]
    timeout_seconds: float = 45.0
    name: str = "codex-app-server"
    version: str = "app-server-jsonl-v1"
    isolated_home: Path | None = None
    provider_env: dict[str, str] = field(default_factory=dict, repr=False)
    _active: dict[str, _ActiveAppServerTurn] = field(init=False, default_factory=dict)
    _request_id: int = field(init=False, default=0)
    interactive_user_input: bool = True
    _stderr_tasks: dict[int, asyncio.Task] = field(init=False, default_factory=dict)
    _stderr_tail: dict[int, bytes] = field(init=False, default_factory=dict)

    def _environment(self) -> dict[str, str]:
        # Codex uses its local login/configuration, but the planning Agent must
        # never inherit application/provider secrets from the API process.
        env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "CODEX_HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR"}
            and not any(token in key.upper() for token in ("KEY", "TOKEN", "SECRET"))
        }
        if self.isolated_home is not None:
            self.isolated_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            env["CODEX_HOME"] = str(self.isolated_home)
        env.update(self.provider_env)
        env["GAMS_AGENT_READ_ONLY"] = "1"
        return env

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _spawn(self, work_dir: Path | None = None) -> asyncio.subprocess.Process:
        cwd = work_dir if work_dir and work_dir.is_dir() else None
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=str(cwd) if cwd else None,
                env=self._environment(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=4 * 1024 * 1024,
            )
            async def drain_stderr():
                while chunk := await process.stderr.read(4096):
                    self._stderr_tail[process.pid] = (self._stderr_tail.get(process.pid, b"") + chunk)[-2048:]
            self._stderr_tasks[process.pid] = asyncio.create_task(drain_stderr())
            return process
        except (OSError, ValueError) as exc:
            raise CodexUnavailable(
                f"无法启动 Codex App Server：{exc}。请检查 Codex 安装或 GAME_ASSETS_CODEX_BIN。"
            ) from exc

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        try:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.5)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        finally:
            task = self._stderr_tasks.pop(process.pid, None)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self._stderr_tail.pop(process.pid, None)

    async def _send(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        request_id: int | str | None = None,
    ) -> int | str | None:
        if process.stdin is None:
            raise CodexUnavailable("Codex App Server stdin is unavailable")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        if request_id is not None:
            payload["id"] = request_id
        process.stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()
        return request_id

    async def _respond(
        self,
        process: asyncio.subprocess.Process,
        request_id: int | str,
        result: dict[str, Any],
    ) -> None:
        if process.stdin is None:
            raise CodexUnavailable("Codex App Server stdin is unavailable")
        payload = {"id": request_id, "result": result}
        process.stdin.write((json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
        await process.stdin.drain()

    @staticmethod
    def _rpc_error(value: dict[str, Any]) -> str | None:
        error = value.get("error")
        if not isinstance(error, dict):
            return None
        message = str(error.get("message") or error.get("code") or "Codex App Server returned an error")
        return message[:500]

    async def _read_line(self, process: asyncio.subprocess.Process, *, timeout: float | None = 45.0) -> dict[str, Any]:
        if process.stdout is None:
            raise CodexProtocolError("Codex App Server stdout is unavailable")
        raw = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
        if not raw:
            detail = self._stderr_tail.get(process.pid, b"").decode("utf-8", "replace").strip()
            for secret in self.provider_env.values():
                if secret:
                    detail = detail.replace(secret, "[redacted]")
            raise CodexUnavailable(
                "Codex App Server exited before responding"
                + (f": {detail[:500]}" if detail else "")
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CodexProtocolError("Codex App Server returned invalid JSONL") from exc
        if not isinstance(value, dict):
            raise CodexProtocolError("Codex App Server JSONL frames must be objects")
        return value

    async def _request(
        self,
        process: asyncio.subprocess.Process,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id()
        await self._send(process, method, params, request_id=request_id)
        while True:
            value = await self._read_line(process, timeout=self.timeout_seconds)
            if value.get("id") != request_id:
                # Notifications can arrive while a request is being handled.
                # They are consumed here; turn notifications are handled by the
                # streaming loop below after the turn/start response.
                continue
            error = self._rpc_error(value)
            if error:
                raise CodexUnavailable(f"Codex App Server {method} failed: {error}")
            result = value.get("result")
            return result if isinstance(result, dict) else {}

    async def _initialize(self, process: asyncio.subprocess.Process) -> None:
        await self._request(
            process,
            "initialize",
            {
                "clientInfo": {
                    "name": "game-asset-management-system",
                    "title": "Game Asset Management System",
                    "version": "0.1.0",
                }
            },
        )
        await self._send(process, "initialized")

    @staticmethod
    def _thread_id(result: dict[str, Any]) -> str | None:
        thread = result.get("thread")
        if isinstance(thread, dict) and thread.get("id"):
            return str(thread["id"])
        for key in ("threadId", "thread_id", "id"):
            if result.get(key):
                return str(result[key])
        return None

    @staticmethod
    def _turn_id(result: dict[str, Any]) -> str | None:
        turn = result.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        for key in ("turnId", "turn_id", "id"):
            if result.get(key):
                return str(result[key])
        return None

    async def _open_thread(
        self,
        process: asyncio.subprocess.Process,
        *,
        thread_id: str | None,
        model: str | None,
        work_dir: Path | None,
    ) -> str:
        cwd = str(work_dir) if work_dir and work_dir.is_dir() else None
        sandbox = "read-only"
        if thread_id:
            params: dict[str, Any] = {"threadId": thread_id, "sandbox": sandbox, "developerInstructions": OfficialCodexAdapter._developer_instructions()}
            if cwd:
                params["cwd"] = cwd
            if model:
                params["model"] = model
            try:
                result = await self._request(process, "thread/resume", params)
                return self._thread_id(result) or thread_id
            except CodexUnavailable as exc:
                if not any(token in str(exc).lower() for token in ("not found", "unknown thread", "paginated_threads", "not supported", "no rollout")):
                    raise
        params = {
            "approvalPolicy": "never",
            "sandbox": sandbox,
            "developerInstructions": OfficialCodexAdapter._developer_instructions(),
        }
        if cwd:
            params["cwd"] = cwd
        if model:
            params["model"] = model
        result = await self._request(
            process,
            "thread/start",
            params,
        )
        value = self._thread_id(result)
        if not value:
            raise CodexProtocolError("Codex App Server thread/start did not return a thread id")
        return value

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    parts.append(AppServerCodexAdapter._text(item.get("text", item.get("content", ""))))
            return "".join(parts)
        if isinstance(value, dict):
            return AppServerCodexAdapter._text(value.get("text", value.get("content", value.get("message", ""))))
        return ""

    @staticmethod
    def _extract_draft(text: str, structured: Any = None) -> dict[str, Any] | None:
        candidates: list[Any] = [structured]
        stripped = text.strip()
        if stripped:
            candidates.append(stripped)
            if "```" in stripped:
                candidates.extend(part.strip() for part in stripped.split("```") if part.strip() and not part.strip().startswith("json"))
        for candidate in candidates:
            if isinstance(candidate, dict) and isinstance(candidate.get("draft"), dict):
                return candidate["draft"]
            if not isinstance(candidate, str):
                continue
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("draft"), dict):
                return parsed["draft"]
        return None

    def _event_from_notification(
        self,
        value: dict[str, Any],
        *,
        local_turn_id: str,
        thread_id: str,
        final_text: list[str],
    ) -> dict[str, Any] | None:
        method = str(value.get("method", ""))
        params = value.get("params") if isinstance(value.get("params"), dict) else {}
        item = params.get("item") if isinstance(params.get("item"), dict) else {}
        remote_turn_id = params.get("turnId") or params.get("turn_id")
        common = {"thread_id": thread_id, "turn_id": local_turn_id}
        if method in {"item/agentMessage/delta", "item/agent_message/delta"}:
            delta = self._text(params.get("delta", params.get("text", params.get("content", ""))))
            if not delta:
                return None
            final_text.append(delta)
            return {"type": "assistant.delta", "data": {"content": delta, "item_id": params.get("itemId") or params.get("item_id"), **common}}
        if method in {"item/started", "item/started/commandExecution"}:
            if item.get("type") in {"agentMessage", "text"}:
                return None
            tool = item.get("type") or item.get("command") or params.get("type") or "codex.tool"
            return {"type": "tool.started", "data": {"tool": str(tool), "item": item, **common}}
        if method in {"item/completed", "item/completed/agentMessage"}:
            item_type = str(item.get("type", params.get("type", "")))
            if item_type in {"agentMessage", "assistantMessage", "message", "text"} or method.endswith("agentMessage"):
                content = self._text(item.get("text", item.get("content", params.get("text", params.get("content", "")))))
                if content:
                    phase = item.get("phase")
                    visible, draft = OfficialCodexAdapter._structured_response(content)
                    event = {"type": "assistant.message", "data": {"content": visible, "phase": phase, "item_id": item.get("id"), **common}}
                    if draft is not None:
                        event["data"]["draft"] = draft
                    return event
                return None
            tool = item.get("type") or item.get("command") or params.get("type") or "codex.tool"
            return {"type": "tool.result", "data": {"tool": str(tool), "item": item, **common}}
        if method in {"turn/failed", "turn/error"}:
            error = params.get("error") if isinstance(params.get("error"), dict) else params
            return {"type": "turn.failed", "data": {"reason": self._text(error) or "Codex turn failed", **common}}
        if method == "turn/completed":
            turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
            status = str(turn.get("status", "completed"))
            if status in {"failed", "error"}:
                return {"type": "turn.failed", "data": {"reason": self._text(turn.get("error")) or "Codex turn failed", **common}}
            return {"type": "turn.completed", "data": {"status": status, **common}}
        if method in {"error", "turn/aborted"}:
            return {"type": "turn.failed", "data": {"reason": self._text(params) or "Codex turn failed", **common}}
        return None

    async def plan_turn(
        self,
        context: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        budget: int,
        turn_id: str,
        model: str | None = None,
        thread_id: str | None = None,
        work_dir: Path | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        process = await self._spawn(work_dir)
        active = _ActiveAppServerTurn(process=process)
        self._active[turn_id] = active
        try:
            await self._initialize(process)
            active.thread_id = await self._open_thread(
                process,
                thread_id=thread_id,
                model=model,
                work_dir=work_dir,
            )
            if active.thread_id != thread_id:
                context = {**context, "rebuild_conversation": True}
                if thread_id:
                    yield {"type": "thread.rebuilt", "data": {"message": "已保留上下文继续规划。", "thread_id": active.thread_id}}
            prompt = OfficialCodexAdapter._prompt(context, messages, draft, work_dir)
            prompt += ("\n\n若缺少可在本次对话立即回答的关键信息，优先调用 request_user_input，"
                       "一次最多三个问题；在得到回答前不要提交最终草案。开放设定可不提供选项。"
                       "不能即时解决的项目 Schema 等长期阻塞项才放进 draft.questions。")
            turn_params: dict[str, Any] = {
                "threadId": active.thread_id,
                "input": [{"type": "text", "text": prompt}],
                "approvalPolicy": "never",
                "sandboxPolicy": {"type": "readOnly"},
                "outputSchema": OfficialCodexAdapter._output_schema(),
            }
            if model:
                turn_params["model"] = model
            if work_dir and work_dir.is_dir():
                turn_params["cwd"] = str(work_dir)
            start_result = await self._request(
                process,
                "turn/start",
                turn_params,
            )
            active.remote_turn_id = self._turn_id(start_result)
            final_text: list[str] = []
            pending_draft: dict[str, Any] | None = None
            pending_priority = 0
            completed = False
            while True:
                try:
                    timeout = None if active.pending_request_id is not None and active.pending_auto_resolution_ms is None else self.timeout_seconds
                    if active.pending_request_id is not None and active.pending_auto_resolution_ms is not None:
                        timeout = max(0.001, active.pending_auto_resolution_ms / 1000)
                    value = await self._read_line(process, timeout=timeout)
                except TimeoutError:
                    if active.pending_request_id is None:
                        raise CodexUnavailable("Codex App Server turn timed out")
                    yield {"type": "user_input.fallback", "data": {"item_id": active.pending_item_id, "reason": "input_timeout", "turn_id": turn_id}}
                    return
                if value.get("id") is not None:
                    if value.get("method"):
                        request_id = value["id"] if isinstance(value["id"], (int, str)) else None
                        if request_id is not None:
                            if value["method"] == "item/tool/requestUserInput":
                                params = value.get("params") if isinstance(value.get("params"), dict) else {}
                                raw_questions = params.get("questions")
                                if (not isinstance(raw_questions, list) or not 1 <= len(raw_questions) <= 3
                                        or any(not isinstance(q, dict) or q.get("isSecret") for q in raw_questions)):
                                    await self._respond(process, request_id, {"answers": {}})
                                    yield {"type": "user_input.requested", "data": {"questions": [{"id": "clarification", "header": "补充信息", "question": "请描述生成目标和交付要求。", "options": None}], "response_mode": "new_turn", "item_id": str(params.get("itemId") or request_id), "turn_id": turn_id}}
                                    yield {"type": "turn.failed", "data": {"reason": "interactive input request is unsupported or unsafe"}}
                                    return
                                active.pending_request_id = request_id
                                active.pending_item_id = str(params.get("itemId") or request_id)
                                active.pending_auto_resolution_ms = params.get("autoResolutionMs") if isinstance(params.get("autoResolutionMs"), int) else None
                                public_questions = [{key: item for key, item in question.items() if key != "isSecret"} for question in raw_questions]
                                yield {"type": "user_input.requested", "data": {"questions": public_questions, "response_mode": "resume_turn", "item_id": active.pending_item_id, "transport_request_id": str(request_id), "remote_thread_id": active.thread_id, "remote_turn_id": active.remote_turn_id, "auto_resolution_ms": active.pending_auto_resolution_ms, "turn_id": turn_id}}
                                continue
                            await self._respond(
                                process,
                                request_id,
                                {"decision": "decline", "reason": "generation planning is read-only"},
                            )
                    continue
                if value.get("method") == "serverRequest/resolved":
                    params = value.get("params") if isinstance(value.get("params"), dict) else {}
                    if str(params.get("requestId")) == str(active.pending_request_id):
                        active.pending_request_id = None
                        active.pending_item_id = None
                        active.pending_auto_resolution_ms = None
                    continue
                event = self._event_from_notification(
                    value,
                    local_turn_id=turn_id,
                    thread_id=active.thread_id,
                    final_text=final_text,
                )
                if event is None:
                    continue
                if event["type"] == "assistant.message":
                    phase = event["data"].get("phase")
                    draft_value = event["data"].pop("draft", None)
                    if isinstance(draft_value, dict) and phase != "commentary":
                        priority = 2 if phase == "final_answer" else 1
                        if priority >= pending_priority:
                            pending_draft = draft_value
                            pending_priority = priority
                if event["type"] == "turn.completed" and pending_draft is not None:
                    yield {"type": "draft.updated", "data": {"draft": pending_draft, "source": "codex.app-server", "thread_id": active.thread_id, "turn_id": turn_id}}
                yield event
                if event["type"] in {"turn.completed", "turn.failed"}:
                    completed = True
                    break
            if not completed:
                raise CodexProtocolError("Codex App Server ended without a terminal turn event")
        except asyncio.CancelledError:
            raise
        finally:
            self._active.pop(turn_id, None)
            await self._stop_process(process)

    async def answer_input(self, *, turn_id: str, transport_request_id: str, answers: dict[str, Any]) -> bool:
        active = self._active.get(turn_id)
        if active is None or str(active.pending_request_id) != transport_request_id:
            return False
        await self._respond(active.process, active.pending_request_id, {"answers": answers})
        active.pending_request_id = None
        active.pending_item_id = None
        active.pending_auto_resolution_ms = None
        return True

    async def steer(self, *, content: str, thread_id: str | None = None, turn_id: str | None = None) -> bool:
        active = self._active.get(turn_id or "")
        if active is None or active.pending_request_id is not None or not active.remote_turn_id:
            return False
        await self._send(active.process, "turn/steer", {"threadId": active.thread_id or thread_id, "input": [{"type": "text", "text": content}], "expectedTurnId": active.remote_turn_id}, request_id=self._next_id())
        return True

    async def interrupt(self, *, thread_id: str | None = None, turn_id: str | None = None) -> bool:
        active = self._active.get(turn_id or "") if turn_id else None
        if active is None:
            return False
        if active.remote_turn_id:
            try:
                await self._send(
                    active.process,
                    "turn/interrupt",
                    {"threadId": active.thread_id or thread_id, "turnId": active.remote_turn_id},
                    request_id=self._next_id(),
                )
            except (CodexAdapterError, OSError):
                return False
        return True

    async def capabilities(self) -> dict[str, Any]:
        process = await self._spawn()
        try:
            await self._initialize(process)
            result = await self._request(process, "model/list", {"limit": 100})
            values = result.get("data", result.get("models", result.get("items", [])))
            models: list[dict[str, Any]] = []
            if isinstance(values, list):
                for item in values:
                    if not isinstance(item, dict):
                        continue
                    model_id = item.get("id") or item.get("model") or item.get("slug")
                    if model_id:
                        models.append({
                            "id": str(model_id),
                            "name": str(item.get("name") or item.get("displayName") or model_id),
                            "is_default": bool(item.get("isDefault", item.get("is_default", False))),
                            "input_modalities": item.get("inputModalities", item.get("input_modalities", ["text"])),
                        })
            return {
                "available": True,
                "adapter": self.name,
                "version": self.version,
                "models": models,
                "diagnostic": {"code": "ready", "message": "Codex App Server 已连接。", "hint": "规划 Agent 使用只读沙箱。"},
            }
        finally:
            await self._stop_process(process)

    async def diagnose(
        self,
        _context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        raise CodexAdapterError("Codex App Server is configured for generation planning")

    async def archive_thread(self, thread_id: str | None) -> bool:
        return await self._thread_lifecycle("thread/archive", thread_id)

    async def unarchive_thread(self, thread_id: str | None) -> bool:
        return await self._thread_lifecycle("thread/unarchive", thread_id)

    async def delete_thread(self, thread_id: str | None) -> bool:
        return await self._thread_lifecycle("thread/delete", thread_id)

    async def _thread_lifecycle(self, method: str, thread_id: str | None) -> bool:
        if not thread_id:
            return False
        process = await self._spawn()
        try:
            await self._initialize(process)
            await self._request(process, method, {"threadId": thread_id})
            return True
        except CodexUnavailable as exc:
            # Deleting local audit state must not be blocked by an already
            # missing rollout or a stopped local app server.
            if "not found" in str(exc).lower() or "unknown" in str(exc).lower():
                return True
            raise
        finally:
            await self._stop_process(process)


@dataclass(slots=True)
class OfficialCodexAdapter:
    """Shared official Codex Python SDK client for read-only planning turns."""

    codex_bin: str | None = None
    timeout_seconds: float = 45.0
    name: str = "openai-codex"
    version: str = "0.144.4"
    isolated_home: Path | None = None
    provider_env: dict[str, str] = field(default_factory=dict, repr=False)
    config_overrides: tuple[str, ...] = ()
    _client: Any = field(init=False, default=None)
    _client_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _active: dict[str, Any] = field(init=False, default_factory=dict)
    _startup_error: str | None = field(init=False, default=None)
    _startup_task: asyncio.Task[Any] | None = field(init=False, default=None)

    def _environment(self) -> dict[str, str]:
        """Keep local Codex auth while excluding application/provider secrets."""

        allowed = {
            "PATH",
            "HOME",
            "CODEX_HOME",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TMPDIR",
            "TERM",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        }
        env = {key: value for key, value in os.environ.items() if key in allowed}
        if self.isolated_home is not None:
            self.isolated_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            env["CODEX_HOME"] = str(self.isolated_home)
        env.update(self.provider_env)
        env["GAMS_AGENT_READ_ONLY"] = "1"
        return env

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from openai_codex import AsyncCodex, CodexConfig

                candidate = AsyncCodex(
                    CodexConfig(
                        codex_bin=self.codex_bin,
                        env=self._environment(),
                        config_overrides=self.config_overrides,
                        client_name="game_asset_management_system",
                        client_title="Game Asset Generation Center",
                        client_version="0.1.0",
                    )
                )
                await candidate.__aenter__()
            except Exception as exc:
                self._startup_error = self._friendly_error(exc)
                raise CodexUnavailable(self._startup_error) from exc
            self._client = candidate
            self._startup_error = None
            return candidate

    async def start(self) -> None:
        """Warm the SDK connection without blocking FastAPI's lifespan.

        App Server discovery may wait on a stale local runtime.  Generation is
        optional to the rest of the workbench, so health, assets, and project
        routes must become available immediately while the shared client warms
        in the background.
        """

        if self._startup_task is not None and not self._startup_task.done():
            return

        async def warm_client() -> None:
            try:
                await self._ensure_client()
            except (CodexUnavailable, asyncio.CancelledError):
                return

        self._startup_task = asyncio.create_task(warm_client(), name="codex-sdk-warmup")
        await asyncio.sleep(0)

    async def close(self) -> None:
        startup_task, self._startup_task = self._startup_task, None
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task
        async with self._client_lock:
            client, self._client = self._client, None
        if client is not None:
            await client.close()
        self._active.clear()

    @staticmethod
    def _friendly_error(exc: BaseException) -> str:
        message = str(exc).strip() or type(exc).__name__
        lowered = message.lower()
        if any(token in lowered for token in ("login", "auth", "unauthorized", "401")):
            return "Codex 登录已失效，请先在本机 Codex 中重新登录。"
        if any(token in lowered for token in ("overload", "busy", "rate limit", "429")):
            return "Codex 当前过载，请稍后重试。"
        if any(token in lowered for token in ("not found", "no such file", "executable")):
            return "Codex 固定运行时不可用，请重新同步 openai-codex 或检查 GAME_ASSETS_CODEX_BIN。"
        return _truncate_text(message, 500)

    @staticmethod
    def _model_json(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            dumped = value.model_dump(mode="json", by_alias=True)
            return dumped if isinstance(dumped, dict) else {"value": dumped}
        if isinstance(value, dict):
            return dict(value)
        return {"value": str(value)}

    @staticmethod
    def _public_item(value: Any) -> dict[str, Any]:
        item = OfficialCodexAdapter._model_json(value)
        for key in ("aggregatedOutput", "aggregated_output", "output", "result"):
            if key in item and isinstance(item[key], str):
                item[key] = _truncate_text(item[key], 8_000)
        return item

    @staticmethod
    def _structured_response(text: str) -> tuple[str, dict[str, Any] | None]:
        stripped = text.strip()
        candidates = [stripped]
        if "```" in stripped:
            candidates.extend(
                block.strip().removeprefix("json").strip()
                for block in stripped.split("```")
                if block.strip()
            )
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                message = str(value.get("message") or "").strip()
                draft = value.get("draft") if isinstance(value.get("draft"), dict) else None
                draft_json = value.get("draft_json")
                if draft is None and isinstance(draft_json, str):
                    try:
                        parsed_draft = json.loads(draft_json)
                    except json.JSONDecodeError:
                        parsed_draft = None
                    if isinstance(parsed_draft, dict):
                        draft = parsed_draft
                return message or "方案草案已更新。", draft
        return stripped, None

    @staticmethod
    def _output_schema() -> dict[str, Any]:
        # GenerationPlanningDraft intentionally contains open JSON objects for
        # metadata, output schemas and route settings. OpenAI strict structured
        # output rejects such nested objects unless they disallow extra keys.
        # Use a strict envelope and transport the complete draft as JSON text;
        # the planning service still parses and validates it before accepting
        # any update.
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "draft_json": {"type": "string"},
            },
            "required": ["message", "draft_json"],
            "additionalProperties": False,
        }

    @staticmethod
    def _context_filename(work_dir: Path | None) -> str:
        if work_dir is None or not work_dir.is_dir():
            return "context.json"
        candidates = [item for item in work_dir.glob("context*.json") if not item.name.startswith("context-index")]
        if not candidates:
            return "context.json"
        return max(candidates, key=lambda item: item.stat().st_mtime_ns).name

    @staticmethod
    def _prompt(
        context: dict[str, Any],
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        work_dir: Path | None,
    ) -> str:
        latest = messages[-1] if messages else {}
        instruction = _truncate_text(latest.get("content", "请检查当前草案并提出下一步。"), 20_000)
        context_name = OfficialCodexAdapter._context_filename(work_dir)
        draft_json = json.dumps(draft, ensure_ascii=False, separators=(",", ":"))
        draft_section = draft_json
        if len(draft_json.encode("utf-8")) > 70_000 and work_dir is not None:
            draft_path = work_dir / "current-draft.json"
            draft_path.write_text(draft_json, encoding="utf-8")
            draft_section = "请读取 ./current-draft.json（完整当前草案）。"
        prompt = (
            "请处理用户的最新规划指令。项目事实位于只读工作目录中的 "
            f"./{context_name}；它是紧凑清单。先读取清单，只在确实需要某个资产时，按清单指向的索引文件使用 rg 或定向 JSON 查询。"
            "不要 cat、打印或完整读取大型索引文件。不要猜测不存在的资产或修订。\n"
            "你只负责规划：不得修改文件、创建计划/资产/任务、调用生成供应商或请求写权限。\n"
            "返回符合输出 schema 的 JSON，其中 message 是给用户的简洁中文说明，draft_json 是更新后完整草案的 JSON 字符串。"
            "draft_json 必须能解析成一个 JSON 对象，并保留当前草案中仍然有效的设置和元数据。"
            "先读取 ./planning-draft-schema.json，严格遵守任务、asset 与 references 的字段结构，不自行发明 title、output_spec 等任务字段。"
            "只输出必要方案，不复制索引或规范全文；说明和假设保持简短。"
            "引用必须固定 revision_id。文档 sha256 使用该修订 content_hash，不能使用 input_hash；图片使用 rendition.sha256。"
            "candidate_path 留空，由系统分配候选暂存目录；target_path 才是批准后的交付路径。"
            "warnings 必须是对象数组，例如 [{\"code\":\"character_spec_incomplete\","
            "\"message\":\"角色身份和交付规格尚未明确。\"}]，不得使用字符串数组。\n\n"
            f"LATEST_USER_INSTRUCTION:\n{instruction}\n\n"
            f"CURRENT_DRAFT:\n{draft_section}\n\n"
            f"CONTEXT_HASH:\n{context.get('context_hash') or context.get('_context_hash') or '见上下文文件'}"
        )
        if context.get("previous_batches"):
            batches = json.dumps(context["previous_batches"], ensure_ascii=False)
            if work_dir is not None:
                (work_dir / "previous-batches.json").write_text(batches, encoding="utf-8")
                batches = "读取 ./previous-batches.json，使用已生成的资产与修订作为下一版的明确参考；不能修改已确认批次。"
            prompt += "\n\nPREVIOUS_BATCHES:\n" + batches
        if context.get("rebuild_conversation") or context.get("recovering_turn"):
            recovery = json.dumps({"confirmed_answers": context.get("confirmed_answers", []), "recent_dialogue": context.get("recovery_history", [])}, ensure_ascii=False)
            if work_dir is not None:
                (work_dir / "recovery-summary.json").write_text(recovery, encoding="utf-8")
                recovery = "先读取 ./recovery-summary.json，保留全部已确认要求与回答。完整历史位于 ./conversation-history.json，可按需读取。"
            prompt += "\n\nRECOVERY_CONTEXT（继续已保存回合，以下记录是必须保留的用户上下文）:\n" + recovery
        if len(prompt.encode("utf-8")) >= 100_000:
            raise CodexProtocolError("Codex planning prompt exceeds the 100KB safety limit")
        return prompt

    @staticmethod
    def _developer_instructions() -> str:
        return (
            "你是游戏资产生成中心的只读规划 Agent。只读取当前工作目录提供的上下文文件；"
            "不修改任何文件，不运行会写入状态的命令，不访问数据库或版本控制，不调用资产生成供应商。"
            "所有项目事实都必须可由上下文文件验证。仅输出用户可见的简短推理摘要，不输出隐藏思维链。"
        )

    async def _thread(
        self,
        client: Any,
        *,
        thread_id: str | None,
        model: str | None,
        work_dir: Path | None,
    ) -> Any:
        from openai_codex import ApprovalMode, Sandbox

        values = {
            "approval_mode": ApprovalMode.deny_all,
            "cwd": str(work_dir) if work_dir else None,
            "developer_instructions": self._developer_instructions(),
            "model": model,
            "sandbox": Sandbox.read_only,
        }
        if thread_id:
            try:
                return await self._retry_overload(
                    lambda: client.thread_resume(thread_id, **values)
                )
            except Exception as exc:
                if not any(token in str(exc).lower() for token in ("not found", "unknown thread", "paginated_threads", "not supported")):
                    raise
        return await self._retry_overload(lambda: client.thread_start(**values))

    @staticmethod
    async def _retry_overload(operation: Any, *, attempts: int = 3) -> Any:
        from openai_codex import is_retryable_error

        delay = 0.25
        for attempt in range(1, attempts + 1):
            try:
                return await operation()
            except Exception as exc:
                if attempt >= attempts or not is_retryable_error(exc):
                    raise
                await asyncio.sleep(delay)
                delay = min(2.0, delay * 2)
        raise AssertionError("unreachable")

    def _events_from_notification(
        self,
        notification: Any,
        *,
        local_turn_id: str,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        method = str(getattr(notification, "method", ""))
        payload = getattr(notification, "payload", None)
        common = {"thread_id": thread_id, "turn_id": local_turn_id}
        if method == "turn/started":
            turn = getattr(payload, "turn", None)
            return [{
                "type": "turn.started",
                "data": {
                    **common,
                    "remote_turn_id": getattr(turn, "id", None),
                    "started_at": getattr(turn, "started_at", None),
                },
            }]
        if method == "item/agentMessage/delta":
            delta = str(getattr(payload, "delta", ""))
            return [{"type": "assistant.delta", "data": {**common, "item_id": getattr(payload, "item_id", None), "content": delta}}] if delta else []
        if method == "item/reasoning/summaryTextDelta":
            delta = str(getattr(payload, "delta", ""))
            return [{"type": "reasoning.summary", "data": {**common, "item_id": getattr(payload, "item_id", None), "content": delta}}] if delta else []
        if method in {"item/commandExecution/outputDelta", "item/mcpToolCall/progress"}:
            data = self._model_json(payload)
            data.update(common)
            if isinstance(data.get("delta"), str):
                data["delta"] = _truncate_text(data["delta"], 8_000)
            return [{"type": "tool.progress", "data": data}]
        if method == "item/started":
            item = getattr(payload, "item", None)
            root = getattr(item, "root", item)
            item_type = str(getattr(root, "type", "codex.tool"))
            if item_type in {"agentMessage", "reasoning", "userMessage"}:
                return []
            return [{
                "type": "tool.started",
                "data": {**common, "tool": item_type, "item": self._public_item(item)},
            }]
        if method == "item/completed":
            item = getattr(payload, "item", None)
            root = getattr(item, "root", item)
            item_type = str(getattr(root, "type", "codex.tool"))
            if item_type == "agentMessage":
                raw_phase = getattr(root, "phase", None)
                phase = getattr(raw_phase, "value", raw_phase)
                phase = str(phase) if phase is not None else None
                content, draft = self._structured_response(str(getattr(root, "text", "")))
                events: list[dict[str, Any]] = []
                if content:
                    events.append({
                        "type": "assistant.message",
                        "data": {**common, "item_id": getattr(root, "id", None), "content": content, "phase": phase},
                    })
                if draft is not None and phase != "commentary":
                    events.append({
                        "type": "_draft.candidate",
                        "data": {
                            **common,
                            "draft": draft,
                            "phase": phase,
                            "source": "openai-codex-sdk",
                        },
                    })
                return events
            if item_type == "reasoning":
                summaries = getattr(root, "summary", None) or []
                content = "\n".join(str(value) for value in summaries if value)
                return [{"type": "reasoning.summary", "data": {**common, "content": content}}] if content else []
            return [{
                "type": "tool.result",
                "data": {**common, "tool": item_type, "item": self._public_item(item)},
            }]
        if method == "thread/tokenUsage/updated":
            usage = getattr(payload, "token_usage", None)
            return [{
                "type": "usage.updated",
                "data": {**common, "usage": self._model_json(usage)},
            }]
        if method == "turn/completed":
            turn = getattr(payload, "turn", None)
            status = getattr(getattr(turn, "status", None), "value", getattr(turn, "status", "completed"))
            error = getattr(turn, "error", None)
            if str(status) == "failed":
                return [{
                    "type": "turn.failed",
                    "data": {
                        **common,
                        "reason": _truncate_text(getattr(error, "message", None) or "Codex turn failed", 500),
                        "duration_ms": getattr(turn, "duration_ms", None),
                    },
                }]
            return [{
                "type": "turn.completed",
                "data": {
                    **common,
                    "status": str(status),
                    "duration_ms": getattr(turn, "duration_ms", None),
                },
            }]
        if method == "error":
            value = self._model_json(payload)
            return [{
                "type": "turn.failed",
                "data": {**common, "reason": _truncate_text(value.get("message") or value, 500)},
            }]
        return []

    async def plan_turn(
        self,
        context: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        draft: dict[str, Any],
        budget: int,
        turn_id: str,
        model: str | None = None,
        thread_id: str | None = None,
        work_dir: Path | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        del budget
        client = await self._ensure_client()
        try:
            thread = await self._thread(
                client,
                thread_id=thread_id,
                model=model,
                work_dir=work_dir,
            )
            rebuilt = bool(thread_id and thread.id != thread_id)
            context = {**context, "rebuild_conversation": rebuilt or not thread_id}
            if rebuilt or not thread_id:
                messages = [{"role": "user", "content": "这是已保存的会话记录。保留已确认要求，继续最新目标。\n" + json.dumps(context.get("recovery_history", []), ensure_ascii=False) + "\n已回答问题：" + json.dumps(context.get("confirmed_answers", []), ensure_ascii=False)}] + messages
            if rebuilt:
                yield {"type": "thread.rebuilt", "thread_id": thread.id, "data": {"message": "已保留上下文继续规划。", "previous_thread_id": thread_id}}
            from openai_codex import ApprovalMode, Sandbox
            from openai_codex.generated.v2_all import ReasoningSummary

            handle = await self._retry_overload(
                lambda: thread.turn(
                    self._prompt(context, messages, draft, work_dir),
                    approval_mode=ApprovalMode.deny_all,
                    cwd=str(work_dir) if work_dir else None,
                    model=model,
                    output_schema=self._output_schema(),
                    sandbox=Sandbox.read_only,
                    summary=ReasoningSummary(root="concise"),
                )
            )
        except CodexAdapterError:
            raise
        except Exception as exc:
            raise CodexUnavailable(self._friendly_error(exc)) from exc

        self._active[turn_id] = handle
        terminal = False
        pending_draft: dict[str, Any] | None = None
        pending_priority = 0
        try:
            async for notification in handle.stream():
                for event in self._events_from_notification(
                    notification,
                    local_turn_id=turn_id,
                    thread_id=thread.id,
                ):
                    if event.get("type") == "_draft.candidate":
                        phase = (event.get("data") or {}).get("phase")
                        priority = 2 if phase == "final_answer" else 1
                        if priority >= pending_priority:
                            pending_draft = event
                            pending_priority = priority
                        continue
                    is_terminal = event.get("type") in {"turn.completed", "turn.failed"}
                    if is_terminal and terminal:
                        continue
                    if event.get("type") == "turn.completed" and pending_draft is not None:
                        draft_event = dict(pending_draft)
                        draft_event["type"] = "draft.updated"
                        draft_event.setdefault("thread_id", thread.id)
                        yield draft_event
                        pending_draft = None
                    terminal = terminal or is_terminal
                    event.setdefault("thread_id", thread.id)
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise CodexAdapterError(self._friendly_error(exc)) from exc
        finally:
            self._active.pop(turn_id, None)
        if not terminal:
            raise CodexProtocolError("Codex SDK stream ended without a terminal turn event")

    async def steer(
        self,
        *,
        content: str,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        del thread_id
        handle = self._active.get(turn_id or "")
        if handle is None:
            return False
        await handle.steer(content)
        return True

    async def interrupt(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        del thread_id
        handle = self._active.get(turn_id or "")
        if handle is None:
            return False
        await handle.interrupt()
        return True

    async def capabilities(self) -> dict[str, Any]:
        try:
            client = await self._ensure_client()
            account, response = await asyncio.gather(client.account(), client.models())
        except Exception as exc:
            message = self._friendly_error(exc)
            return {
                "available": False,
                "adapter": self.name,
                "version": self.version,
                "models": [],
                "diagnostic": {
                    "code": "runtime_unavailable",
                    "message": message,
                    "hint": "检查本机 Codex 登录或固定 SDK 运行时后重试；人工编排仍可使用。",
                },
            }
        logged_in = not bool(getattr(account, "requires_openai_auth", False)) or getattr(account, "account", None) is not None
        models = [
            {
                "id": item.id,
                "name": item.display_name,
                "is_default": bool(item.is_default),
                "input_modalities": [getattr(value, "value", str(value)) for value in (item.input_modalities or [])],
            }
            for item in response.data
        ]
        return {
            "can_connect": True, "can_start": logged_in, "can_resume": None,
            "available": logged_in,
            "adapter": self.name,
            "version": self.version,
            "models": models,
            "diagnostic": {
                "code": "ready" if logged_in else "auth_required",
                "message": "Codex SDK 已连接，只读规划沙箱已启用。" if logged_in else "Codex 尚未登录。",
                "hint": "复用本机 Codex 登录；规划 Agent 无项目写权限。",
            },
        }

    async def diagnose(
        self,
        _context: dict[str, Any],
        *,
        budget: int,
        work_dir: Path | None = None,
    ) -> dict[str, Any]:
        del budget, work_dir
        raise CodexAdapterError("official Codex SDK adapter is configured for generation planning")

    async def archive_thread(self, thread_id: str | None) -> bool:
        if not thread_id:
            return False
        client = await self._ensure_client()
        await client.thread_archive(thread_id)
        return True

    async def unarchive_thread(self, thread_id: str | None) -> bool:
        if not thread_id:
            return False
        client = await self._ensure_client()
        await client.thread_unarchive(thread_id)
        return True

    async def delete_thread(self, thread_id: str | None) -> bool:
        # SDK 0.144.4 intentionally exposes archive/unarchive but no delete.
        # Archive remote history before deleting only the application's local
        # projection instead of falling back to private JSON-RPC methods.
        return await self.archive_thread(thread_id) if thread_id else False


def _truncate_text(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else f"{text[:limit]}\n[truncated]"


def build_codex_adapter(
    command: str | None,
    *,
    codex_bin: str | None = None,
    timeout_seconds: float = 45.0,
) -> CodexAdapter:
    # The explicit legacy command remains the highest priority so tests and
    # deployments with a small local adapter keep working unchanged.
    if command and command.strip():
        try:
            argv = tuple(shlex.split(command))
        except ValueError:
            return UnavailableCodexAdapter()
        if not argv or Path(argv[0]).name.lower() in {"git", "git.exe"}:
            return UnavailableCodexAdapter()
        return JsonCommandCodexAdapter(argv, timeout_seconds=timeout_seconds)

    configured = codex_bin.strip() if codex_bin and codex_bin.strip() else None
    if configured and Path(configured).name.lower() in {"git", "git.exe"}:
        return UnavailableCodexAdapter()
    try:
        from codex_cli_bin import bundled_codex_path

        binary = configured or str(bundled_codex_path())
    except (ImportError, FileNotFoundError):
        if configured is None:
            return UnavailableCodexAdapter()
        binary = configured
    return AppServerCodexAdapter(
        command=(binary, "app-server", "--enable", "default_mode_request_user_input"),
        timeout_seconds=timeout_seconds,
        version="0.144.4",
    )
