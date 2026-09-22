from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from game_assets_api import generation_planning as planning
from game_assets_api.codex_adapter import AppServerCodexAdapter, OfficialCodexAdapter, build_codex_adapter
from game_assets_api.models import AgentInputRequest, AgentSession
from game_assets_api.services import ServiceError

from .conftest import create_asset, create_fake_provider, create_project


def _image_task(
    *,
    task_id: str = "make-rain",
    key: str = "media.chapter-01-rain",
    provider_id: str,
    model: str = "fake-image",
    target_path: str = "approved/assets/backgrounds/chapter-01-rain.png",
) -> dict[str, Any]:
    return {
        "id": task_id,
        "asset": {
            "mode": "new",
            "key": key,
            "kind": "media",
            "subtype": "background",
            "title": "序章夜雨背景",
            "tags": ["chapter-01"],
            "metadata": {"purpose": "opening"},
        },
        "kind": "image",
        "prompt": "16:9 夜雨中的序章街道，遵循风格圣经。",
        "provider_profile_id": provider_id,
        "model": model,
        "width": 1600,
        "height": 900,
        "transparent": False,
        "references": [],
        "depends_on": [],
        "target_path": target_path,
        "candidate_path": "workspace/candidates/pending/make-rain",
        "locked_fields": [],
        "metadata": {"decision_basis": "场景要求 16:9"},
    }


def _draft(conversation: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "title": "序章夜雨背景生成",
        "summary": "为序章建立一张可审查的夜雨背景。",
        "tasks": [task],
        "questions": [],
        "assumptions": ["沿用项目的图片默认路由"],
        "warnings": [],
        "settings": {
            "extra_call_budget": 2,
            "max_paid_remediation_rounds": 2,
            "max_transport_retries": 2,
            "max_concurrency": 2,
        },
        "context_hash": conversation["context_hash"],
    }


def _conversation(client: TestClient, project_id: str) -> dict[str, Any]:
    response = client.post(
        "/api/generation-conversations",
        json={"project_id": project_id, "seed_asset_ids": []},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for_status(client: TestClient, session_id: str, statuses: set[str]) -> dict[str, Any]:
    for _ in range(100):
        value = client.get(f"/api/generation-conversations/{session_id}").json()
        if value.get("status") in statuses:
            return value
        asyncio.run(asyncio.sleep(0.01))
    raise AssertionError(f"conversation did not reach {statuses}")


def test_conversation_list_is_lightweight_and_prompt_uses_only_latest_instruction(
    client: TestClient,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    conversation = _conversation(client, project["id"])

    with client.app.state.database.sessions() as snapshot_session:
        snapshot_row = snapshot_session.get(AgentSession, conversation["id"])
        assert snapshot_row is not None
        manifest_path = Path(project["root_path"]) / str(snapshot_row.context_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["context_manifest_version"] == 1
    assert manifest["index_files"]["full"].startswith("context-index")
    assert (manifest_path.parent / manifest["index_files"]["full"]).is_file()

    # Model the production-sized payload that previously made this endpoint
    # return multiple megabytes. The summary endpoint must never serialize it.
    large_revision_index = [
        {"asset_id": f"asset-{index}", "revision": 1, "digest": "x" * 2_048}
        for index in range(1_146)
    ]
    with client.app.state.database.sessions() as session:
        row = session.get(AgentSession, conversation["id"])
        assert row is not None
        row.context_json = {"revision_index": large_revision_index}
        row.draft_json = {"tasks": [{"prompt": "y" * 200_000}]}
        session.commit()

    response = client.get(f"/api/generation-conversations?project_id={project['id']}")
    assert response.status_code == 200
    assert len(response.content) < 10_000
    summary = response.json()[0]
    assert "context" not in summary
    assert "draft" not in summary

    events = [
        SimpleNamespace(event_type="user.message", data_json={"content": "旧指令"}, turn_id="turn-1"),
        SimpleNamespace(event_type="assistant.message", data_json={"content": "旧回复"}, turn_id="turn-1"),
        SimpleNamespace(event_type="user.message", data_json={"content": "最新指令"}, turn_id="turn-2"),
    ]
    messages = planning._messages_from_events(events)
    assert messages == [{"role": "user", "content": "最新指令", "turn_id": "turn-2"}]
    prompt = OfficialCodexAdapter._prompt(
        {"revision_index": large_revision_index, "_context_hash": "context-hash"},
        messages,
        {"title": "紧凑草案", "tasks": []},
        None,
    )
    assert "最新指令" in prompt
    assert "旧指令" not in prompt
    assert "asset-1145" not in prompt
    assert len(prompt.encode("utf-8")) < 100_000


def test_official_codex_uses_a_strict_json_envelope_for_open_draft_data() -> None:
    schema = OfficialCodexAdapter._output_schema()
    assert schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "draft_json": {"type": "string"},
        },
        "required": ["message", "draft_json"],
        "additionalProperties": False,
    }
    message, draft = OfficialCodexAdapter._structured_response(
        '{"message":"方案已更新","draft_json":"{\\"tasks\\":[],\\"settings\\":{\\"route_defaults\\":{}}}"}'
    )
    assert message == "方案已更新"
    assert draft == {"tasks": [], "settings": {"route_defaults": {}}}


def _agent_message_notification(text: str, phase: str | None) -> SimpleNamespace:
    root = SimpleNamespace(
        type="agentMessage",
        text=text,
        phase=SimpleNamespace(value=phase) if phase else None,
    )
    return SimpleNamespace(
        method="item/completed",
        payload=SimpleNamespace(item=SimpleNamespace(root=root)),
    )


def test_official_codex_commentary_never_proposes_a_draft_update() -> None:
    adapter = OfficialCodexAdapter()
    events = adapter._events_from_notification(
        _agent_message_notification(
            '{"message":"正在读取项目清单","draft_json":"{}"}',
            "commentary",
        ),
        local_turn_id="turn-1",
        thread_id="thread-1",
    )

    assert [item["type"] for item in events] == ["assistant.message"]
    assert events[0]["data"]["phase"] == "commentary"


def test_default_adapter_uses_pinned_interactive_app_server() -> None:
    adapter = build_codex_adapter(None)
    assert isinstance(adapter, AppServerCodexAdapter)
    assert adapter.command[-2:] == ("--enable", "default_mode_request_user_input")
    assert "codex" in adapter.command[0]


@pytest.mark.asyncio
async def test_app_server_question_pauses_and_resumes_same_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AppServerCodexAdapter(("codex", "app-server"))
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    replies: list[tuple[str, dict[str, Any]]] = []
    process = SimpleNamespace()

    async def spawn(_self: Any, _work_dir: Any = None) -> Any:
        return process

    async def initialize(_self: Any, _process: Any) -> None:
        return None

    async def open_thread(_self: Any, _process: Any, **_kwargs: Any) -> str:
        return "remote-thread"

    async def rpc(_self: Any, _process: Any, method: str, _params: Any) -> dict[str, Any]:
        assert method == "turn/start"
        return {"turn": {"id": "remote-turn"}}

    async def read(_self: Any, _process: Any, *, timeout: float | None = None) -> dict[str, Any]:
        return await asyncio.wait_for(incoming.get(), timeout=timeout)

    async def respond(_self: Any, _process: Any, request_id: int | str, value: dict[str, Any]) -> None:
        replies.append((str(request_id), value))

    async def stop(_self: Any, _process: Any) -> None:
        return None

    monkeypatch.setattr(AppServerCodexAdapter, "_spawn", spawn)
    monkeypatch.setattr(AppServerCodexAdapter, "_initialize", initialize)
    monkeypatch.setattr(AppServerCodexAdapter, "_open_thread", open_thread)
    monkeypatch.setattr(AppServerCodexAdapter, "_request", rpc)
    monkeypatch.setattr(AppServerCodexAdapter, "_read_line", read)
    monkeypatch.setattr(AppServerCodexAdapter, "_respond", respond)
    monkeypatch.setattr(AppServerCodexAdapter, "_stop_process", stop)
    await incoming.put({"id": 42, "method": "item/tool/requestUserInput", "params": {
        "threadId": "remote-thread", "turnId": "remote-turn", "itemId": "question-item",
        "questions": [{"id": "role", "header": "身份", "question": "角色身份？", "options": [
            {"label": "武将", "description": "军职"}, {"label": "文官", "description": "朝臣"},
        ]}],
    }})
    stream = adapter.plan_turn(
        {"context_hash": "hash"}, messages=[{"content": "新角色"}], draft={"settings": {}},
        budget=1, turn_id="local-turn",
    )
    requested = await anext(stream)
    assert requested["type"] == "user_input.requested"
    assert requested["data"]["remote_turn_id"] == "remote-turn"
    assert replies == []
    assert await adapter.answer_input(
        turn_id="local-turn", transport_request_id="42", answers={"role": {"answers": ["武将"]}},
    )
    assert replies == [("42", {"answers": {"role": {"answers": ["武将"]}}})]
    await incoming.put({"method": "item/completed", "params": {"item": {
        "type": "agentMessage", "phase": "final_answer",
        "text": json.dumps({"message": "已规划", "draft_json": json.dumps({"tasks": [], "settings": {}})}),
    }}})
    await incoming.put({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
    events = [event async for event in stream]
    assert [event["type"] for event in events] == ["assistant.message", "draft.updated", "turn.completed"]


@pytest.mark.asyncio
async def test_official_codex_emits_only_the_last_authoritative_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = OfficialCodexAdapter()
    notifications = [
        _agent_message_notification(
            '{"message":"旧兼容回复","draft_json":"{\\"title\\":\\"旧草案\\"}"}',
            None,
        ),
        _agent_message_notification(
            '{"message":"最终回复","draft_json":"{\\"title\\":\\"最终草案\\"}"}',
            "final_answer",
        ),
        SimpleNamespace(
            method="turn/completed",
            payload=SimpleNamespace(
                turn=SimpleNamespace(
                    status=SimpleNamespace(value="completed"),
                    error=None,
                    duration_ms=10,
                )
            ),
        ),
    ]

    class Handle:
        async def stream(self):
            for notification in notifications:
                yield notification

    class Thread:
        id = "thread-1"

        async def turn(self, *_args: Any, **_kwargs: Any) -> Handle:
            return Handle()

    async def fake_client(_adapter: OfficialCodexAdapter) -> object:
        return object()

    async def fake_thread(_adapter: OfficialCodexAdapter, *_args: Any, **_kwargs: Any) -> Thread:
        return Thread()

    monkeypatch.setattr(OfficialCodexAdapter, "_ensure_client", fake_client)
    monkeypatch.setattr(OfficialCodexAdapter, "_thread", fake_thread)
    events = [
        event
        async for event in adapter.plan_turn(
            {"context_hash": "context-hash"},
            messages=[{"content": "更新草案"}],
            draft={"title": "原草案"},
            budget=1,
            turn_id="turn-1",
        )
    ]

    draft_events = [event for event in events if event["type"] == "draft.updated"]
    assert len(draft_events) == 1
    assert draft_events[0]["data"]["draft"]["title"] == "最终草案"
    assert draft_events[0]["data"]["phase"] == "final_answer"
    assert events[-1]["type"] == "turn.completed"


def test_invalid_output_schema_is_not_misreported_as_context_invalid() -> None:
    error = planning._planning_error(
        "Invalid schema for response_format in context=(): additionalProperties is required (invalid_json_schema)"
    )
    assert error["error_code"] == "output_schema_invalid"
    assert error["retryable"] is False


def test_string_warnings_are_normalized_to_stable_objects(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "warning-game")
    conversation = _conversation(client, project["id"])
    draft = deepcopy(conversation["draft"])
    draft["warnings"] = ["  角色身份和交付规格尚未明确。  "]

    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )

    assert response.status_code == 200, response.text
    warning = response.json()["draft"]["warnings"][0]
    assert warning["code"].startswith("agent_warning_")
    assert warning["message"] == "角色身份和交付规格尚未明确。"


def test_invalid_warning_object_reports_its_field_path(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "invalid-warning-game")
    conversation = _conversation(client, project["id"])
    draft = deepcopy(conversation["draft"])
    draft["warnings"] = [{"code": "missing_message"}]

    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )

    assert response.status_code == 422
    assert "warnings[0].message" in response.text


@pytest.mark.asyncio
async def test_official_codex_warmup_never_blocks_api_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    waiting = asyncio.Event()

    async def stalled_client(_adapter: OfficialCodexAdapter) -> Any:
        await waiting.wait()

    monkeypatch.setattr(OfficialCodexAdapter, "_ensure_client", stalled_client)
    adapter = OfficialCodexAdapter()
    await asyncio.wait_for(adapter.start(), timeout=0.1)
    assert adapter._startup_task is not None
    assert not adapter._startup_task.done()
    await adapter.close()
    assert adapter._startup_task is None


def test_planning_confirm_registers_new_asset_atomically_and_is_idempotent(
    client: TestClient,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)
    conversation = _conversation(client, project["id"])
    task = _image_task(provider_id=provider["id"])
    draft = _draft(conversation, task)

    edited = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )
    assert edited.status_code == 200, edited.text
    edited_payload = edited.json()
    assert edited_payload["draft"]["tasks"][0]["asset"]["mode"] == "new"

    # Planning is a read-only phase: neither the ordinary Catalog nor a plan
    # exists until the explicit confirmation call.
    assert client.get(f"/api/assets?project_id={project['id']}").json() == []
    assert client.get(f"/api/generation-plans?project_id={project['id']}").json() == []

    confirmed = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={
            "draft_hash": edited_payload["draft_hash"],
            "context_hash": edited_payload["context_hash"],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    result = confirmed.json()
    assert result["conversation"]["status"] == "completed"
    assert len(result["created_assets"]) == 1
    assert result["created_assets"][0]["key"] == task["asset"]["key"]
    assert result["plan"]["status"] in {"confirmed", "credentials_locked"}
    assert len(result["jobs"]) == 1

    assets = client.get(f"/api/assets?project_id={project['id']}").json()
    assert [item["key"] for item in assets] == [task["asset"]["key"]]
    plans = client.get(f"/api/generation-plans?project_id={project['id']}").json()
    assert len(plans) == 1

    # A network retry of the confirm request must return the same immutable
    # result rather than creating another asset or plan.
    repeated = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={
            "draft_hash": edited_payload["draft_hash"],
            "context_hash": edited_payload["context_hash"],
        },
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["plan"]["id"] == result["plan"]["id"]
    assert [item["id"] for item in repeated.json()["created_assets"]] == [
        item["id"] for item in result["created_assets"]
    ]
    assert len(client.get(f"/api/generation-plans?project_id={project['id']}").json()) == 1

    deleted_chat = client.delete(f"/api/generation-conversations/{conversation['id']}")
    assert deleted_chat.status_code == 204, deleted_chat.text
    assert len(client.get(f"/api/generation-plans?project_id={project['id']}").json()) == 1
    assert len(client.get(f"/api/assets?project_id={project['id']}").json()) == 1


def test_planning_draft_rejects_cross_project_cycles_paths_and_models(
    client: TestClient,
) -> None:
    first = create_project(client, client.app.state.settings.projects_root / "first-game")
    second = create_project(client, client.app.state.settings.projects_root / "second-game")
    provider = create_fake_provider(client)
    foreign_asset = create_asset(client, second["id"], key="entity.foreign")
    conversation = _conversation(client, first["id"])

    first_task = _image_task(provider_id=provider["id"])
    first_task["asset"] = {
        "mode": "existing",
        "asset_id": foreign_asset["id"],
        "key": foreign_asset["key"],
        "kind": foreign_asset["kind"],
        "subtype": foreign_asset["subtype"],
        "title": foreign_asset["title"],
        "tags": [],
        "metadata": {},
    }
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": _draft(conversation, first_task)},
    )
    assert response.status_code == 422
    assert "existing project asset" in response.text

    cycle_a = _image_task(task_id="task-a", key="media.a", provider_id=provider["id"])
    cycle_b = _image_task(task_id="task-b", key="media.b", provider_id=provider["id"])
    cycle_a["depends_on"] = ["task-b"]
    cycle_b["depends_on"] = ["task-a"]
    cycle_draft = _draft(conversation, cycle_a)
    cycle_draft["tasks"] = [cycle_a, cycle_b]
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": cycle_draft},
    )
    assert response.status_code == 422
    assert "acyclic" in response.text

    unsafe = _image_task(provider_id=provider["id"], target_path="../outside.png")
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": _draft(conversation, unsafe)},
    )
    assert response.status_code == 422
    assert "unsafe path" in response.text or "project-relative" in response.text

    invalid_model = _image_task(provider_id=provider["id"], model="not-registered")
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": _draft(conversation, invalid_model)},
    )
    assert response.status_code == 422
    assert "not-registered" in response.text


def test_planning_messages_are_streamed_and_duplicate_client_ids_are_idempotent(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)

    class StreamingAdapter:
        name = "test-stream"
        version = "1"

        async def plan_turn(self, context: dict[str, Any], **kwargs: Any):
            draft = deepcopy(kwargs["draft"])
            draft["tasks"] = [_image_task(provider_id=provider["id"])]
            draft["context_hash"] = context.get("project", {}).get("id") and kwargs["draft"].get("context_hash")
            yield {"type": "assistant.delta", "content": "我先读取项目规范。"}
            yield {"type": "tool.started", "tool": "read_project_specs"}
            yield {"type": "tool.result", "tool": "read_project_specs", "result": {"count": 0}}
            yield {"type": "assistant.message", "content": "已根据项目规则提出一项夜雨背景任务。"}
            yield {"type": "draft.updated", "draft": draft}
            yield {"type": "turn.completed"}

    # Replace only the planning adapter; the isolated runner and event store
    # remain the production implementation.
    client.app.state.generation_planning_runner.adapter = StreamingAdapter()
    conversation = _conversation(client, project["id"])
    message = client.post(
        f"/api/generation-conversations/{conversation['id']}/messages",
        json={"content": "生成序章夜雨背景", "client_message_id": "message-1"},
    )
    assert message.status_code == 200, message.text
    turn_id = message.json()["turn_id"]
    completed = _wait_for_status(client, conversation["id"], {"awaiting_user"})
    assert completed["draft"]["tasks"][0]["id"] == "make-rain"
    events = client.get(f"/api/generation-conversations/{conversation['id']}/events").json()
    types = [item["event_type"] for item in events]
    assert "assistant.delta" in types
    assert "tool.started" in types
    assert "tool.result" in types
    assert "draft.updated" in types
    assert "turn.completed" in types
    assert all("api_key" not in str(item) and "secret" not in str(item).lower() for item in events)

    duplicate = client.post(
        f"/api/generation-conversations/{conversation['id']}/messages",
        json={"content": "生成序章夜雨背景", "client_message_id": "message-1"},
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["turn_id"] == turn_id
    assert completed["turn_count"] == 1


def test_native_input_request_resumes_one_turn_and_is_idempotent(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "input-game")

    class QuestionAdapter:
        name = "question-test"
        version = "1"
        interactive_user_input = True

        def __init__(self) -> None:
            self.resumed = asyncio.Event()
            self.answer: dict[str, Any] | None = None

        async def plan_turn(self, _context: dict[str, Any], **kwargs: Any):
            yield {"type": "user_input.requested", "data": {
                "item_id": "item-1", "transport_request_id": "rpc-1",
                "remote_thread_id": "thread-1", "remote_turn_id": "turn-remote",
                "response_mode": "resume_turn", "questions": [{
                    "id": "role", "header": "身份", "question": "角色身份是什么？",
                    "options": [{"label": "武将", "description": "军职"}, {"label": "文官", "description": "朝臣"}],
                }],
            }}
            await self.resumed.wait()
            draft = deepcopy(kwargs["draft"])
            draft["summary"] = f"角色身份：{self.answer['role']['answers'][0]}"
            yield {"type": "draft.updated", "draft": draft}
            yield {"type": "turn.completed"}

        async def answer_input(self, *, turn_id: str, transport_request_id: str, answers: dict[str, Any]) -> bool:
            assert transport_request_id == "rpc-1"
            self.answer = answers
            self.resumed.set()
            return True

    adapter = QuestionAdapter()
    client.app.state.generation_planning_runner.adapter = adapter
    conversation = _conversation(client, project["id"])
    sent = client.post(f"/api/generation-conversations/{conversation['id']}/messages", json={"content": "新角色"})
    assert sent.status_code == 200
    waiting = _wait_for_status(client, conversation["id"], {"awaiting_input"})
    pending = waiting["pending_input"]
    assert pending["questions"][0]["id"] == "role"
    assert waiting["draft_hash"] == conversation["draft_hash"]
    assert waiting["turn_count"] == 1
    invalid = client.post(f"/api/generation-conversations/{conversation['id']}/input-requests/{pending['id']}/answer", json={
        "client_response_id": "response-1", "answers": {"role": {"answers": ["皇帝"]}},
    })
    assert invalid.status_code == 422
    answered = client.post(f"/api/generation-conversations/{conversation['id']}/input-requests/{pending['id']}/answer", json={
        "client_response_id": "response-1", "answers": {"role": {"answers": ["武将"]}},
    })
    assert answered.status_code == 200, answered.text
    assert answered.json()["turn_id"] == sent.json()["turn_id"]
    again = client.post(f"/api/generation-conversations/{conversation['id']}/input-requests/{pending['id']}/answer", json={
        "client_response_id": "response-1", "answers": {"role": {"answers": ["武将"]}},
    })
    assert again.status_code == 200
    completed = _wait_for_status(client, conversation["id"], {"awaiting_user"})
    assert completed["draft"]["summary"] == "角色身份：武将"
    assert completed["turn_count"] == 1
    assert completed["budget_used"] == 1
    assert client.get(f"/api/generation-plans?project_id={project['id']}").json() == []


def test_restart_downgrades_input_and_preserves_answer_card(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "restart-input-game")
    conversation = _conversation(client, project["id"])
    with client.app.state.database.sessions() as session:
        row = session.get(AgentSession, conversation["id"])
        row.status = "awaiting_input"
        item = AgentInputRequest(
            session_id=row.id, turn_id="old-turn", item_id="item-1", response_mode="resume_turn",
            status="pending", transport_request_id="rpc-1", questions_json=[{
                "id": "role", "header": "身份", "question": "角色身份是什么？", "options": None,
            }],
        )
        session.add(item)
        session.commit()
        request_id = item.id
    asyncio.run(client.app.state.generation_planning_runner.start())
    recovered = client.get(f"/api/generation-conversations/{conversation['id']}").json()
    assert recovered["status"] == "awaiting_user"
    assert recovered["pending_input"]["id"] == request_id
    assert recovered["pending_input"]["response_mode"] == "new_turn"
    asyncio.run(client.app.state.generation_planning_runner.start())
    with client.app.state.database.sessions() as session:
        assert len(session.query(AgentInputRequest).filter_by(session_id=conversation["id"]).all()) == 1


def test_invalid_agent_draft_preserves_the_last_valid_draft_and_reports_field_path(
    client: TestClient,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "invalid-agent-draft")

    class InvalidDraftAdapter:
        name = "invalid-draft"
        version = "1"

        async def plan_turn(self, _context: dict[str, Any], **kwargs: Any):
            invalid = deepcopy(kwargs["draft"])
            invalid["warnings"] = [{"code": "missing_message"}]
            yield {"type": "draft.updated", "draft": invalid}
            yield {"type": "turn.completed"}

    client.app.state.generation_planning_runner.adapter = InvalidDraftAdapter()
    conversation = _conversation(client, project["id"])
    response = client.post(
        f"/api/generation-conversations/{conversation['id']}/messages",
        json={"content": "更新草案"},
    )
    assert response.status_code == 200, response.text

    failed = _wait_for_status(client, conversation["id"], {"awaiting_user"})
    assert failed["draft"] == conversation["draft"]
    assert failed["draft_hash"] == conversation["draft_hash"]
    assert failed["draft_version"] == conversation["draft_version"]
    assert failed["last_error"]["field_path"] == "warnings[0].message"
    assert "code" in failed["last_error"]["validation_message"]
    events = client.get(f"/api/generation-conversations/{conversation['id']}/events").json()
    failed_event = next(item for item in reversed(events) if item["event_type"] == "turn.failed")
    assert failed_event["data"]["field_path"] == "warnings[0].message"


def test_normalized_warning_still_requires_explicit_acceptance(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "warning-confirm")
    provider = create_fake_provider(client)
    conversation = _conversation(client, project["id"])
    draft = _draft(conversation, _image_task(provider_id=provider["id"]))
    draft["warnings"] = ["请确认本次测试警告。"]
    edited = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )
    assert edited.status_code == 200, edited.text
    warning_code = edited.json()["draft"]["warnings"][0]["code"]

    rejected = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={"draft_hash": edited.json()["draft_hash"]},
    )
    assert rejected.status_code == 422
    assert warning_code in rejected.text

    accepted = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={
            "draft_hash": edited.json()["draft_hash"],
            "accepted_warning_codes": [warning_code],
        },
    )
    assert accepted.status_code == 200, accepted.text


def test_confirmation_rollback_removes_catalog_asset_when_plan_creation_fails(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)
    conversation = _conversation(client, project["id"])
    task = _image_task(provider_id=provider["id"], key="media.rollback-check")
    draft = _draft(conversation, task)
    edited = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )
    assert edited.status_code == 200, edited.text

    def fail_confirm(*_args: Any, **_kwargs: Any) -> None:
        raise ServiceError(503, "simulated controller failure")

    monkeypatch.setattr(planning, "confirm_plan", fail_confirm)
    response = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={"draft_hash": edited.json()["draft_hash"]},
    )
    assert response.status_code == 503
    assert client.get(f"/api/assets?project_id={project['id']}").json() == []
    assert client.get(f"/api/generation-plans?project_id={project['id']}").json() == []


def test_text_tasks_keep_missing_schema_as_a_question_until_confirmation(
    client: TestClient,
) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)
    text_asset = create_asset(
        client,
        project["id"],
        key="content.chapter-01-dialogue",
        kind="content",
        subtype="dialogue",
        title="序章对白",
    )
    conversation = _conversation(client, project["id"])
    task = _image_task(provider_id=provider["id"], task_id="write-dialogue", key="content.new-dialogue")
    task["kind"] = "text"
    task["model"] = "fake-text"
    task["asset"] = {
        "mode": "existing",
        "asset_id": text_asset["id"],
        "key": text_asset["key"],
        "kind": text_asset["kind"],
        "subtype": text_asset["subtype"],
        "title": text_asset["title"],
        "tags": [],
        "metadata": {},
    }
    for key in ("width", "height", "transparent", "candidate_path"):
        task.pop(key, None)
    draft = _draft(conversation, task)
    draft["settings"]["max_concurrency"] = 2
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )
    assert response.status_code == 200, response.text
    updated = response.json()
    assert "schema:write-dialogue" in updated["draft"]["questions"]
    confirmation = client.post(
        f"/api/generation-conversations/{conversation['id']}/confirm",
        json={"draft_hash": updated["draft_hash"]},
    )
    assert confirmation.status_code == 422
    assert "JSON Schema" in confirmation.text


def test_context_and_settings_staleness_are_rejected(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)
    conversation = _conversation(client, project["id"])
    task = _image_task(provider_id=provider["id"])
    draft = _draft(conversation, task)

    stale_hash = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": "0" * 64, "draft": draft},
    )
    assert stale_hash.status_code == 409

    invalid_settings = deepcopy(draft)
    invalid_settings["settings"]["max_concurrency"] = 0
    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": invalid_settings},
    )
    assert response.status_code == 422
    assert "max_concurrency" in response.text

    # A new Catalog fact changes the immutable project snapshot.  The old
    # conversation cannot be confirmed against that stale context hash.
    create_asset(client, project["id"], key="entity.context-drift")
    edited = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={"base_hash": conversation["draft_hash"], "draft": draft},
    )
    assert edited.status_code == 409
    assert "context changed" in edited.text


def test_adapter_events_never_persist_nested_hidden_reasoning() -> None:
    normalized = planning._normalize_adapter_event(
        {
            "type": "tool.result",
            "data": {
                "tool": "read_project_specs",
                "reason": "选择当前已批准的风格圣经。",
                "result": {
                    "analysis": "private analysis",
                    "items": [
                        {
                            "decision_basis": "与序章资产一致",
                            "reasoning_content": "private reasoning",
                        }
                    ],
                },
            },
        },
        turn_id="turn-1",
    )

    assert normalized is not None
    event_type, payload = normalized
    assert event_type == "tool.result"
    assert payload["reason"] == "选择当前已批准的风格圣经。"
    assert payload["result"]["items"][0]["decision_basis"] == "与序章资产一致"
    assert "analysis" not in payload["result"]
    assert "reasoning_content" not in payload["result"]["items"][0]


def test_revision_context_rewrites_nested_paths_to_project_relative(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    revision = SimpleNamespace(
        id="revision-1",
        asset_id="asset-1",
        sequence=1,
        format="media",
        content_json={
            "rendition": {
                "source_path": str(tmp_path / "outside.bin"),
                "normalized_path": "workspace/candidates/result.webp",
            }
        },
        content_hash="a" * 64,
        input_hash="b" * 64,
        style_revision=None,
        prompt_recipe=None,
        provider_snapshot={"cache_path": str(tmp_path / "provider-cache")},
        file_path=str(tmp_path / "outside.json"),
        review_status="pending",
        created_at=None,
    )

    snapshot = planning._revision_snapshot(revision, root)

    assert snapshot["content"]["rendition"]["source_path"] is None
    assert snapshot["content"]["rendition"]["normalized_path"] == "workspace/candidates/result.webp"
    assert snapshot["provider_snapshot"]["cache_path"] is None
    assert snapshot["file_path"] is None


def test_manual_draft_edits_are_redacted_before_persistence(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "sample-game")
    provider = create_fake_provider(client)
    conversation = _conversation(client, project["id"])
    task = _image_task(provider_id=provider["id"])
    task["prompt"] = "保持风格一致；误贴的凭据 sk-examplecredential 不得保存。"
    task["metadata"]["api_key"] = "another-private-value"

    response = client.patch(
        f"/api/generation-conversations/{conversation['id']}/draft",
        json={
            "base_hash": conversation["draft_hash"],
            "draft": _draft(conversation, task),
        },
    )

    assert response.status_code == 200, response.text
    persisted = str(response.json()["draft"])
    assert "sk-examplecredential" not in persisted
    assert "another-private-value" not in persisted
    assert "[redacted]" in persisted


def test_generation_conversation_lifecycle_and_reference_context(client: TestClient) -> None:
    project = create_project(client, client.app.state.settings.projects_root / "lifecycle-game")
    reference = create_asset(
        client,
        project["id"],
        key="media.style-anchor",
        kind="media",
        subtype="visual_anchor",
        title="视觉锚点",
    )
    conversation = _conversation(client, project["id"])

    capabilities = client.get("/api/generation-agent/capabilities")
    assert capabilities.status_code == 200
    assert "available" in capabilities.json()
    assert "diagnostic" in capabilities.json()

    settings = client.patch(
        f"/api/generation-conversations/{conversation['id']}/settings",
        json={"agent_model": "codex-test-model"},
    )
    assert settings.status_code == 200, settings.text
    assert settings.json()["agent_model"] == "codex-test-model"

    context = client.patch(
        f"/api/generation-conversations/{conversation['id']}/context",
        json={"seed_asset_ids": [reference["id"]]},
    )
    assert context.status_code == 200, context.text
    assert context.json()["context"]["seed_asset_ids"] == [reference["id"]]

    archived = client.post(f"/api/generation-conversations/{conversation['id']}/archive")
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"]
    assert client.get(f"/api/generation-conversations?project_id={project['id']}").json() == []
    archived_list = client.get(
        f"/api/generation-conversations?project_id={project['id']}&include_archived=true"
    )
    assert [item["id"] for item in archived_list.json()] == [conversation["id"]]

    restored = client.post(f"/api/generation-conversations/{conversation['id']}/unarchive")
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None

    deleted = client.delete(f"/api/generation-conversations/{conversation['id']}")
    assert deleted.status_code == 204, deleted.text
    assert client.get(f"/api/generation-conversations/{conversation['id']}").status_code == 404
    assert client.get(f"/api/assets?project_id={project['id']}").json()[0]["id"] == reference["id"]
    rescanned = client.post(f"/api/projects/{project['id']}/scan")
    assert rescanned.status_code == 200, rescanned.text
    assert rescanned.json()["errors"] == []
