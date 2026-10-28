import asyncio
import json
import os
from types import SimpleNamespace

import pytest
from game_assets_api.codex_adapter import AppServerCodexAdapter, CodexUnavailable
from game_assets_api.models import ProviderProfile
from game_assets_api.planning_provider import ProviderPlanningAdapter, serve_credentials, read_credential, credential_socket
from game_assets_api.providers import CredentialVault


@pytest.mark.asyncio
async def test_selected_provider_model_isolated_from_personal_defaults(client, monkeypatch, tmp_path):
    with client.app.state.database.sessions() as session:
        session.add(ProviderProfile(id='selected', name='krill', kind='openai_compatible', base_url='https://provider.example/v1', text_model='gpt-5.6-sol', image_model='image', models_json=[{'id':'gpt-5.6-sol','modalities':['text']}]))
        session.commit()
    vault = CredentialVault()
    vault.unlock('selected', 'fixture-secret')
    observed = {}
    async def fake_turn(self, context, **kwargs):
        observed.update(command=self.command, environment=self._environment(), model=kwargs['model'], thread_id=kwargs['thread_id'])
        yield {'type':'turn.completed','data':{}}
    monkeypatch.setattr(AppServerCodexAdapter, 'plan_turn', fake_turn)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'personal'))
    settings = SimpleNamespace(state_dir=tmp_path)
    adapter = ProviderPlanningAdapter(AppServerCodexAdapter(('codex','app-server')), client.app.state.database.sessions, settings, vault)
    draft = {'settings':{'route_defaults':{'text':{'provider_profile_id':'selected','model':'gpt-5.6-sol','reasoning_effort':'high'}}}}
    events = [event async for event in adapter.plan_turn({'project': {'id': 'project-a'}}, messages=[],draft=draft,budget=1,turn_id='t',model='gpt-6-astra',thread_id='legacy-thread')]
    assert observed['model'] == 'gpt-5.6-sol'
    assert 'model_reasoning_effort="high"' in observed['command']
    assert events[0]['data']['reasoning_effort'] == 'high'
    assert observed['environment']['CODEX_HOME'] != str(tmp_path / 'personal')
    assert observed['thread_id'] is None
    first_home = observed['environment']['CODEX_HOME']
    _ = [event async for event in adapter.plan_turn({'project': {'id': 'project-b'}}, messages=[],draft=draft,budget=1,turn_id='t2')]
    assert observed['environment']['CODEX_HOME'] != first_home
    assert observed['environment']['GAMS_PLANNING_API_KEY'] == 'fixture-secret'
    assert 'fixture-secret' not in str(observed['command']) + json.dumps(events)
    assert 'shell_environment_policy.inherit="none"' in observed['command']
    vault.clear()
    with pytest.raises(CodexUnavailable, match='planning_provider_locked'):
        _ = [event async for event in adapter.plan_turn({}, messages=[],draft=draft,budget=1,turn_id='t')]


@pytest.mark.asyncio
async def test_memory_only_worker_credential_bridge(tmp_path):
    vault = CredentialVault(); vault.unlock('provider', 'fixture-key')
    server = await serve_credentials(tmp_path, vault)
    try:
        assert credential_socket(tmp_path).stat().st_mode & 0o777 == 0o600
        assert await read_credential(tmp_path, 'provider') == 'fixture-key'
        vault.clear()
        assert await read_credential(tmp_path, 'provider') is None
    finally:
        server.close(); await server.wait_closed(); credential_socket(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_planning_bridge_never_resumes_a_persistent_thread(tmp_path, monkeypatch):
    adapter = AppServerCodexAdapter(('codex','app-server'))
    calls = []
    async def request(self, process, method, params):
        calls.append((method, params))
        return {'thread': {'id':'new', 'ephemeral': True}}
    monkeypatch.setattr(AppServerCodexAdapter, '_request', request)
    assert await adapter._open_thread(None, thread_id='old', model='gpt-5.6-sol', work_dir=tmp_path) == 'new'
    assert [method for method, _ in calls] == ['thread/start']
    assert calls[0][1]['ephemeral'] is True
    assert calls[0][1]['cwd'] == str(tmp_path)


@pytest.mark.asyncio
async def test_unconfirmed_ephemeral_thread_never_starts_a_turn(tmp_path, monkeypatch):
    adapter = AppServerCodexAdapter(('codex','app-server'))
    calls = []
    async def request(self, process, method, params):
        calls.append(method)
        return {'thread': {'id': 'unexpected-persistent', 'ephemeral': False}}
    monkeypatch.setattr(AppServerCodexAdapter, '_request', request)
    with pytest.raises(CodexUnavailable, match='ephemeral_threads_unsupported'):
        await adapter._open_thread(None, thread_id=None, model=None, work_dir=tmp_path)
    assert calls == ['thread/start']


@pytest.mark.asyncio
async def test_ephemeral_unsupported_does_not_fall_back_to_persistent(tmp_path, monkeypatch):
    adapter = AppServerCodexAdapter(('codex','app-server'))
    calls = []
    async def request(self, process, method, params):
        calls.append((method, params))
        raise CodexUnavailable('unknown field ephemeral')
    monkeypatch.setattr(AppServerCodexAdapter, '_request', request)
    with pytest.raises(CodexUnavailable, match='ephemeral_threads_unsupported'):
        await adapter._open_thread(None, thread_id='old', model=None, work_dir=tmp_path)
    assert [method for method, _ in calls] == ['thread/start']
    assert calls[0][1]['ephemeral'] is True


def test_bridge_preserves_assistant_item_identity():
    adapter = AppServerCodexAdapter(('codex','app-server'))
    event = adapter._event_from_notification({'method':'item/agentMessage/delta','params':{'itemId':'final','delta':'x'}},local_turn_id='t',thread_id='thread',final_text=[])
    assert event['data']['item_id'] == 'final'


@pytest.mark.parametrize('notification', [
    {'method': 'turn/failed', 'params': {'error': 'error sending request for url (https://provider.test/v1/responses)'}},
    {'method': 'turn/completed', 'params': {'turn': {'status': 'failed', 'error': {'message': 'error sending request'}}}},
    {'method': 'turn/completed', 'params': {'turn': {'status': 'failed', 'reason': 'error sending request'}}},
])
def test_bridge_preserves_turn_failure_reason(notification):
    adapter = AppServerCodexAdapter(('codex','app-server'))
    event = adapter._event_from_notification(notification, local_turn_id='t', thread_id='thread', final_text=[])
    assert event['type'] == 'turn.failed'
    assert 'error sending request' in event['data']['reason']

@pytest.mark.asyncio
async def test_bridge_drains_stderr_and_accepts_large_completed_frame(tmp_path):
    import sys
    script = tmp_path / 'fixture_runtime.py'
    script.write_text("import sys,json\nsys.stderr.write('diagnostic '*20000)\nsys.stderr.flush()\nprint(json.dumps({'method':'item/completed','params':{'item':{'type':'agentMessage','text':'x'*100000}}}),flush=True)\n")
    adapter = AppServerCodexAdapter((sys.executable, str(script)), isolated_home=tmp_path / 'home')
    process = await adapter._spawn(tmp_path)
    try:
        frame = await adapter._read_line(process, timeout=5)
        assert len(frame['params']['item']['text']) == 100000
        assert len(adapter._stderr_tail.get(process.pid, b'')) <= 2048
    finally:
        await adapter._stop_process(process)


def test_provider_timeouts_are_not_installation_errors():
    from game_assets_api.generation_planning import _planning_error
    assert _planning_error('Codex App Server turn timed out')['error_code'] == 'planning_timeout'
    assert _planning_error('Codex App Server turn/start failed: model unavailable')['error_code'] == 'turn_failed'
    assert _planning_error('error sending request for url (https://provider.test/v1/responses)')['error_code'] == 'connection_lost'

@pytest.mark.asyncio
async def test_text_asset_request_carries_reasoning_effort(monkeypatch):
    from game_assets_api.providers import OpenAICompatibleProvider
    from game_assets_api.runner import JobRunner
    import httpx
    profile=SimpleNamespace(base_url='https://fixture.example/v1',allow_private_network=False,credential_mode='none')
    provider=OpenAICompatibleProvider(profile,None)
    bodies=[]
    async def request(method,path,**kwargs):
        bodies.append(kwargs['json'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr(provider,'_request',request)
    runner=object.__new__(JobRunner)
    await runner._invoke(provider,job_id='j',request={'kind':'text','prompt':'p','model':'m','metadata':{'reasoning_effort':'high'}},schema={'type':'object'},idempotency_key='fixture')
    assert bodies[0]['reasoning_effort']=='high'
