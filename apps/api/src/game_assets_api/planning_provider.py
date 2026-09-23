"""Provider-backed planning with private runtime state and memory-only credentials."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .codex_adapter import AppServerCodexAdapter, CodexUnavailable
from .models import ProviderProfile
from .provider_catalog import default_route, model_is_compatible


def credential_socket(state_dir: Path) -> Path:
    # macOS AF_UNIX paths are limited to 104 bytes. No secret is in the path.
    digest = hashlib.sha256(str(state_dir.resolve()).encode()).hexdigest()[:20]
    return Path('/tmp') / f'gams-vault-{os.getuid()}-{digest}.sock'


async def serve_credentials(state_dir: Path, vault: Any):
    path = credential_socket(state_dir)
    if path.exists():
        path.unlink()

    async def handle(reader, writer):
        try:
            provider_id = (await asyncio.wait_for(reader.readline(), 2)).decode().strip()
            if len(provider_id) <= 100:
                writer.write(json.dumps({'key': vault.get(provider_id)}).encode() + b'\n')
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=path, start_serving=False)
    path.chmod(0o600)
    await server.start_serving()
    return server


async def read_credential(state_dir: Path, provider_id: str) -> str | None:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(credential_socket(state_dir)), 2)
        try:
            writer.write(provider_id.encode() + b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 2))
            return result.get('key')
        finally:
            writer.close()
            await writer.wait_closed()
    except (OSError, TimeoutError, ValueError):
        return None


class ProviderPlanningAdapter:
    """Never fall back to the user's personal Codex account or model."""
    name = 'provider-codex-planner'
    version = '1'
    interactive_user_input = True

    def __init__(self, base: AppServerCodexAdapter, sessions: Any, settings: Any, vault: Any = None):
        self.base, self.sessions, self.settings, self.vault = base, sessions, settings, vault
        self.active: dict[str, AppServerCodexAdapter] = {}

    async def plan_turn(self, context, *, messages, draft, budget, turn_id, model=None, thread_id=None, work_dir=None):
        route = (draft.get('settings') or {}).get('route_defaults', {}).get('text') or {}
        with self.sessions() as session:
            default_id, default_model = default_route(session, 'text')
            provider_id = route.get('provider_profile_id') or default_id
            profile = session.get(ProviderProfile, provider_id) if provider_id else None
            if profile is None or not profile.is_active:
                raise CodexUnavailable('planning_provider_missing: 请先在供应商渠道配置文字供应商与模型。')
            chosen_model = route.get('model') or (default_model if provider_id == default_id else None) or profile.text_model
            if not chosen_model or not model_is_compatible(profile, chosen_model, 'text'):
                raise CodexUnavailable('planning_provider_missing: 所选文字模型不可用，请检查供应商模型设置。')
            if profile.kind != 'openai_compatible':
                raise CodexUnavailable('planning_provider_unsupported: 规划需要支持 Responses API 的 OpenAI 兼容供应商。')
            base_url, credential_mode = profile.base_url, profile.credential_mode
            provider_name = profile.name
        key = self.vault.get(provider_id) if self.vault is not None else await read_credential(self.settings.state_dir, provider_id)
        if credential_mode == 'required' and not key:
            raise CodexUnavailable('planning_provider_locked: 请在供应商渠道解锁所选供应商；不会改用个人 Codex 登录。')
        home = self.settings.state_dir / 'planning-codex' / hashlib.sha256(provider_id.encode()).hexdigest()[:20]
        overrides = {
            'model_provider': 'gams',
            'model': chosen_model,
            'model_providers.gams.name': provider_name,
            'model_providers.gams.base_url': base_url,
            'model_providers.gams.wire_api': 'responses',
            'model_providers.gams.requires_openai_auth': False,
            'model_providers.gams.env_key': 'GAMS_PLANNING_API_KEY',
            'shell_environment_policy.inherit': 'none',
        }
        effort = route.get('reasoning_effort')
        if effort is not None:
            if effort not in {'none', 'minimal', 'low', 'medium', 'high', 'xhigh'}:
                raise CodexUnavailable('planning_provider_invalid: 不支持的思考等级。')
            overrides['model_reasoning_effort'] = effort
        command = (*self.base.command, *(arg for k, v in overrides.items() for arg in ('-c', f'{k}={json.dumps(v)}')))
        adapter = AppServerCodexAdapter(command, timeout_seconds=self.base.timeout_seconds, isolated_home=home, provider_env={'GAMS_PLANNING_API_KEY': key or 'local-no-auth'})
        self.active[turn_id] = adapter
        try:
            yield {'type': 'planning.route', 'data': {'provider_profile_id': provider_id, 'provider_name': provider_name, 'model': chosen_model, 'isolated': True, 'reasoning_effort': effort}}
            async for event in adapter.plan_turn(context, messages=messages, draft=draft, budget=budget, turn_id=turn_id, model=chosen_model, thread_id=thread_id, work_dir=work_dir):
                yield event
        finally:
            self.active.pop(turn_id, None)
            adapter.provider_env.clear()

    async def steer(self, *, turn_id=None, **kwargs):
        adapter = self.active.get(turn_id)
        return await adapter.steer(turn_id=turn_id, **kwargs) if adapter else False

    async def interrupt(self, *, turn_id=None, **kwargs):
        adapter = self.active.get(turn_id)
        return await adapter.interrupt(turn_id=turn_id, **kwargs) if adapter else False

    async def answer_input(self, *, turn_id, **kwargs):
        adapter = self.active.get(turn_id)
        return await adapter.answer_input(turn_id=turn_id, **kwargs) if adapter else False

    async def close(self):
        for turn_id in list(self.active):
            await self.interrupt(turn_id=turn_id)
