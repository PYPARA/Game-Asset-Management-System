from types import SimpleNamespace
from copy import deepcopy
import pytest
from sqlalchemy import select
from game_assets_api.models import AgentSession, AgentEvent, PlanningTurn, PlanningAttempt
from game_assets_api.planning_runtime import recover_turn, ensure_attempt, PlanningQueueClient
from game_assets_api.agent import record_agent_event
from game_assets_api.codex_adapter import OfficialCodexAdapter
from .conftest import create_project
from .test_generation_planning import _conversation


def test_recovery_keeps_answered_context_and_original_message(client):
    project = create_project(client, client.app.state.settings.projects_root / 'recovery')
    conversation = _conversation(client, project['id'])
    factory = client.app.state.database.sessions
    with factory() as session:
        row = session.get(AgentSession, conversation['id'])
        session.add(PlanningTurn(id='turn-original',session_id=row.id,input_json={'content':'生成新角色'},status='failed'))
        session.flush()
        record_agent_event(session,row,'user.message',data={'content':'生成新角色'},turn_id='turn-original')
        record_agent_event(session,row,'user_input.resolved',data={'answers':{'role':{'answers':['文官']},'output':{'answers':['中性立绘 1 张']},'design':{'answers':['按身份自动设计']}}},turn_id='turn-original')
        attempt=ensure_attempt(session,row.id,'turn-original');attempt.status='failed'
        row.status='unavailable';row.stop_reason='process_restarted'
        draft=deepcopy(row.draft_json)
        recovery=recover_turn(session,row,'turn-original','stable-recovery-key');session.commit()
        again=recover_turn(session,row,'turn-original','stable-recovery-key')
        assert recovery.id==again.id
        assert row.draft_json==draft
        events=session.scalars(select(AgentEvent).where(AgentEvent.session_id==row.id)).all()
        assert len([e for e in events if e.event_type=='user.message'])==1
        assert '文官' in str([e.data_json for e in events if e.event_type=='user_input.resolved'])
        assert [e for e in events if e.event_type=='turn.recovering'][0].turn_id=='turn-original'
        assert len(session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id=='turn-original')).all())==2

@pytest.mark.asyncio
async def test_resume_protocol_incompatible_starts_new_thread_with_saved_context(tmp_path):
    class Client:
        async def thread_resume(self,*args,**kwargs):
            raise RuntimeError('thread/resume failed: paginated_threads is not supported yet')
        async def thread_start(self,**kwargs):
            return SimpleNamespace(id='replacement-thread')
    adapter=OfficialCodexAdapter()
    thread=await adapter._thread(Client(),thread_id='lost-thread',model=None,work_dir=tmp_path)
    assert thread.id=='replacement-thread'
    context={'rebuild_conversation':True,'recovery_history':[{'type':'user.message','data':{'content':'新角色'}}],'confirmed_answers':[{'role':'文官','output':'中性立绘 1 张','design':'按身份自动设计'}]}
    prompt=adapter._prompt(context,messages=[{'content':'继续规划'}],draft={'tasks':[]},work_dir=tmp_path)
    assert 'recovery-summary.json' in prompt
    assert '文官' in (tmp_path/'recovery-summary.json').read_text()

@pytest.mark.asyncio
async def test_api_queue_shutdown_does_not_cancel_worker_attempt(client):
    project=create_project(client,client.app.state.settings.projects_root/'queue')
    conversation=_conversation(client,project['id'])
    queue=PlanningQueueClient(client.app.state.database.sessions,None,client.app.state.settings)
    with queue.session_factory() as session:
        session.add(PlanningTurn(id='durable-turn',session_id=conversation['id']))
        session.commit()
    queue.submit(conversation['id'],'durable-turn')
    await queue.stop()
    replacement=PlanningQueueClient(queue.session_factory,None,queue.settings)
    await replacement.start()
    with queue.session_factory() as session:
        attempts=session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id=='durable-turn')).all()
        assert len(attempts)==1 and attempts[0].status=='queued'


def test_same_conversation_has_two_immutable_batches(client):
    # This test checks confirmation history, not asynchronous asset production.
    # Keep the first batch from changing project context during the second edit.
    client.portal.call(client.app.state.runner.stop)
    from .conftest import create_fake_provider
    from .test_generation_planning import _image_task, _draft
    from game_assets_api import generation_planning as planning
    from game_assets_api.models import ConversationBatch
    project=create_project(client,client.app.state.settings.projects_root/'iterations')
    provider=create_fake_provider(client)
    conversation=_conversation(client,project['id'])
    path=f"/api/generation-conversations/{conversation['id']}"
    draft=_draft(conversation,_image_task(provider_id=provider['id']))
    edited=client.patch(path+'/draft',json={'base_hash':conversation['draft_hash'],'draft':draft}).json()
    first=client.post(path+'/confirm',json={'draft_hash':edited['draft_hash'],'context_hash':edited['context_hash']})
    assert first.status_code==200,first.text
    with client.app.state.database.sessions() as session:
        row=session.get(AgentSession,conversation['id'])
        row.budget_used=row.budget_limit
        turn_id,_=planning.append_generation_message(session,row,'调整衣服颜色，再来一版',client_message_id='iteration')
        # Isolate planning from any live model; only explicitly confirm below.
        session.get(PlanningAttempt,session.scalar(select(PlanningAttempt.id).where(PlanningAttempt.turn_id==turn_id))).status='completed'
        row.status='awaiting_user';session.commit()
    current=client.get(path).json()
    assert len(current['batches'])==1 and current['draft']['tasks']==[]
    second_task=_image_task(provider_id=provider['id'],task_id='second',key='media.second',target_path='approved/assets/backgrounds/second.png')
    updated=client.patch(path+'/draft',json={'base_hash':current['draft_hash'],'draft':_draft(current,second_task)})
    if updated.status_code==409 and 'context changed' in updated.text:
        current=client.patch(path+'/context',json={'seed_asset_ids':[]}).json()
        updated=client.patch(path+'/draft',json={'base_hash':current['draft_hash'],'draft':_draft(current,second_task)})
    assert updated.status_code==200,updated.text
    value=updated.json()
    second=client.post(path+'/confirm',json={'draft_hash':value['draft_hash'],'context_hash':value['context_hash']})
    assert second.status_code==200,second.text
    assert first.json()['plan']['id']!=second.json()['plan']['id']
    detail=client.get(path).json()
    assert len(detail['batches'])==2
    assert detail['batches'][0]['draft']['tasks'][0]['asset']['key']=='media.chapter-01-rain'
    repeated=client.post(path+'/confirm',json={'draft_hash':edited['draft_hash']})
    assert repeated.status_code==200 and repeated.json()['plan']['id']==first.json()['plan']['id']

@pytest.mark.asyncio
async def test_worker_restart_reclaims_lease_and_rebuilds_answers(client, monkeypatch):
    import asyncio
    from game_assets_api import codex_adapter
    from game_assets_api.planning_runtime import run_worker
    project=create_project(client,client.app.state.settings.projects_root/'worker-restart')
    conversation=_conversation(client,project['id'])
    factory=client.app.state.database.sessions
    from game_assets_api import generation_planning as planning
    with factory() as session:
        row=session.get(AgentSession,conversation['id'])
        turn,_=planning.append_generation_message(session,row,'新角色',client_message_id='worker-input')
        record_agent_event(session,row,'user_input.resolved',data={'answers':{'role':['文官'],'output':['中性立绘 1 张'],'design':['按身份自动设计']}},turn_id=turn)
        session.commit()
    started=asyncio.Event(); resumed=asyncio.Event(); contexts=[]
    class Adapter:
        async def plan_turn(self,context,**kwargs):
            contexts.append(context)
            if len(contexts)==1:
                started.set()
                await asyncio.Event().wait()
            resumed.set()
            yield {'type':'assistant.message','data':{'content':'文官，单张中性立绘，自动设计'}}
            yield {'type':'turn.completed','data':{}}
    monkeypatch.setattr(codex_adapter,'build_codex_adapter',lambda *a,**k:Adapter())
    first=asyncio.create_task(run_worker(client.app.state.settings))
    await asyncio.wait_for(started.wait(),5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError): await first
    second=asyncio.create_task(run_worker(client.app.state.settings))
    try:
        await asyncio.wait_for(resumed.wait(),5)
        for _ in range(50):
            with factory() as session:
                status=session.get(PlanningTurn,turn).status
            if status=='completed':break
            await asyncio.sleep(.02)
        assert status=='completed'
        assert '文官' in str(contexts[-1]['confirmed_answers'])
        with factory() as session:
            assert len(session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id==turn)).all())==2
            assert len(session.scalars(select(AgentEvent).where(AgentEvent.session_id==conversation['id'],AgentEvent.event_type=='user.message')).all())==1
    finally:
        second.cancel()
        with pytest.raises(asyncio.CancelledError): await second

@pytest.mark.asyncio
async def test_worker_limits_automatic_recovery_and_never_confirms(client,monkeypatch):
    import asyncio
    from game_assets_api import codex_adapter, generation_planning as planning
    from game_assets_api.codex_adapter import CodexAdapterError
    from game_assets_api.planning_runtime import run_worker
    from game_assets_api.models import GenerationPlan
    project=create_project(client,client.app.state.settings.projects_root/'retry-limit')
    conversation=_conversation(client,project['id'])
    with client.app.state.database.sessions() as session:
        row=session.get(AgentSession,conversation['id'])
        turn,_=planning.append_generation_message(session,row,'测试中断恢复',client_message_id='bounded')
    class Broken:
        async def plan_turn(self,*args,**kwargs):
            raise CodexAdapterError('transport connection lost')
            yield
    monkeypatch.setattr(codex_adapter,'build_codex_adapter',lambda *a,**k:Broken())
    worker=asyncio.create_task(run_worker(client.app.state.settings))
    try:
        attempts=[]
        for _ in range(100):
            with client.app.state.database.sessions() as session:
                attempts=session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id==turn)).all()
                if len(attempts)==4 and all(a.status=='failed' for a in attempts):break
            await asyncio.sleep(.03)
        await asyncio.sleep(.3)
        with client.app.state.database.sessions() as session:
            assert len(session.scalars(select(PlanningAttempt).where(PlanningAttempt.turn_id==turn)).all())==4
            assert session.scalar(select(GenerationPlan)) is None
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):await worker
