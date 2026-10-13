"""Business retries and owner-bound reaction controls for S2-C07/C08/C09/C10."""
import copy
import json
from pathlib import Path
import pytest
import commands
import dispatch
import inbound
import ingest
import ingest_tick
F=json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'));C=F['schedule3']

@pytest.fixture
def inbox(tmp_path,monkeypatch):
    monkeypatch.setattr(ingest,'_STATE_DIR',str(tmp_path/'state'))
    ingest.prepare_state()
    Path(ingest._last_file(C['channel_id'])).write_text('3104',encoding='utf-8')
    monkeypatch.setattr(ingest,'_fetch',lambda *a,**k:[C['messages'][0]])
    return tmp_path

def _fail_stage(*args,**kwargs): raise OSError('synthetic staging failure')

def test_text_write_failure_does_not_advance_cursor(inbox,monkeypatch):
    monkeypatch.setattr(inbound,'stage',_fail_stage)
    with pytest.raises(OSError): ingest.poll_stream(F['stream'],C['channel_id'],'synthetic',C['owner_id'])
    assert Path(ingest._last_file(C['channel_id'])).read_text()=='3104'

def test_reaction_write_failure_does_not_mark_seen(inbox,monkeypatch):
    event={'key':'3105:synthetic:3103','message_id':'3105','emoji':'synthetic','content':F['title'],'timestamp':F['now']}
    monkeypatch.setattr(ingest,'reaction_events',lambda *a,**k:([event],{event['key']}))
    monkeypatch.setattr(inbound,'stage',_fail_stage)
    with pytest.raises(OSError): ingest.poll_reactions_stream(F['stream'],C['channel_id'],'synthetic',C['owner_id'])
    assert event['key'] not in ingest._load_seen(F['stream'])[0]

def test_pending_messages_survive_batches_and_retry_without_new_poll(inbox,monkeypatch):
    ingest.poll_stream(F['stream'],C['channel_id'],'synthetic',C['owner_id'])
    monkeypatch.setattr(ingest,'_fetch',lambda *a,**k:[C['messages'][1]])
    ingest.poll_stream(F['stream'],C['channel_id'],'synthetic',C['owner_id'])
    monkeypatch.setattr(ingest,'poll',lambda **k:{})
    monkeypatch.setattr(ingest,'load_registry',lambda:{})
    monkeypatch.setattr(ingest_tick,'_log',lambda *a:None)
    monkeypatch.setattr(commands,'route',lambda msgs,*a,**k:([],msgs,[]))
    seen=[];succeed=False
    def run(stream,reply,**kw):
        seen.append((reply,kw.get('msg_id')))
        return succeed
    monkeypatch.setattr(dispatch,'dispatch',run)
    ingest_tick.run(post=False)
    assert len(seen)==2
    first_ids=[x[1] for x in seen];assert all(first_ids) and len(set(first_ids))==2
    succeed=True;ingest_tick.run(post=False)
    assert [x[1] for x in seen[2:]]==first_ids
    ingest_tick.run(post=False)
    assert len(seen)==4

@pytest.mark.parametrize('entry',['events','stream','all'])
def test_missing_reaction_owner_rejected_before_enumeration(monkeypatch,entry):
    monkeypatch.setattr(ingest,'_fetch',lambda *a,**k:pytest.fail('ownerless fetch'))
    monkeypatch.setattr(ingest,'_reactors',lambda *a,**k:pytest.fail('ownerless reactors'))
    monkeypatch.setattr(ingest,'channels',lambda *a,**k:pytest.fail('ownerless channel enumeration'))
    with pytest.raises((ValueError,RuntimeError)):
        if entry=='events': ingest.reaction_events(C['channel_id'],'synthetic',None,[C['messages'][0]])
        elif entry=='stream': ingest.poll_reactions_stream(F['stream'],C['channel_id'],'synthetic',None)
        else: ingest.poll(reg={},token='synthetic')

def test_create_retry_uses_stable_distinct_action_identities(monkeypatch):
    calls=[]
    def ensure(title,**fields):
        calls.append(fields);return {'item':{'id':'synthetic-created','state':'pending'},'decision':'created'}
    monkeypatch.setattr(dispatch.store,'ensure_item',ensure)
    monkeypatch.setattr(dispatch.store,'add_item',lambda *a,**k:pytest.fail('identified create bypassed ensure'))
    for _ in range(2):
        dispatch.execute(F['stream'],{'kind':'pool'},copy.deepcopy(C['create_plan']),[],msg_id=F['message_id'])
    keys=[fields['idempotency_key'] for fields in calls]
    assert keys[:2]==keys[2:] and keys[0]!=keys[1]

@pytest.mark.parametrize('kind',['unshown','failed'])
def test_dispatch_confirmation_is_bound_to_outcomes(monkeypatch,kind):
    plan={'actions':[{'op':'done','id':'synthetic-allowed' if kind=='failed' else 'synthetic-unshown'}],
          'confirm':'Everything is complete.'}
    monkeypatch.setattr(dispatch,'get_state',lambda _: [{'id':'synthetic-allowed','title':F['title']}])
    monkeypatch.setattr(dispatch,'get_work',lambda:[])
    monkeypatch.setattr(dispatch,'call_chain',lambda *a,**k:json.dumps(plan))
    def fail(*a,**k): raise RuntimeError('synthetic failure')
    monkeypatch.setattr(dispatch.store,'done',fail)
    posts=[];monkeypatch.setattr(dispatch,'_post',lambda stream,text,*a,**k:posts.append(text))
    # A rejected (unshown) action is final; only a failed one asks for another attempt.
    assert dispatch.dispatch(F['stream'],F['request'],post=False) is (kind=='unshown')
    assert posts and 'Everything is complete.' not in posts[0]
    assert any(word in posts[0].lower() for word in ['failed','rejected','skipped'])

def test_successful_confirmation_control(monkeypatch):
    plan={'actions':[{'op':'done','id':'synthetic-allowed'}],'confirm':'Invented completion details'}
    monkeypatch.setattr(dispatch,'get_state',lambda _:[{'id':'synthetic-allowed','title':F['title']}])
    monkeypatch.setattr(dispatch,'get_work',lambda:[])
    monkeypatch.setattr(dispatch,'call_chain',lambda *a,**k:json.dumps(plan))
    monkeypatch.setattr(dispatch.store,'done',lambda *a,**k:{'id':'synthetic-allowed','state':'done'})
    posts=[];monkeypatch.setattr(dispatch,'_post',lambda stream,text,*a,**k:posts.append(text))
    assert dispatch.dispatch(F['stream'],F['request'],post=False) is True
    assert 'Invented completion details' not in posts[0]


def test_saved_plan_and_success_receipts_survive_confirmation_failure(monkeypatch):
    plans=[];mutations=[];posts=[]
    monkeypatch.setattr(dispatch,'get_state',lambda _:[])
    monkeypatch.setattr(dispatch,'get_work',lambda:[])
    def plan(*args,**kwargs):
        plans.append(1);return json.dumps(C['create_plan'])
    monkeypatch.setattr(dispatch,'call_chain',plan)
    monkeypatch.setattr(dispatch.store,'ensure_item',lambda *args,**kwargs:mutations.append(args)
                        or {'item':F['schedule3_created_item'],'decision':'created'})
    def post(*args,**kwargs):
        posts.append(1)
        if len(posts)==1: raise OSError('synthetic confirmation failure')
    monkeypatch.setattr(dispatch,'_post',post)
    with pytest.raises(OSError):
        dispatch.dispatch(F['stream'],F['request'],msg_id=F['message_id'])
    assert dispatch.dispatch(F['stream'],F['request'],msg_id=F['message_id']) is True
    assert len(plans)==1 and len(mutations)==2 and len(posts)==2

def test_ack_failure_does_not_prevent_durable_dispatch(inbox,monkeypatch):
    ingest.poll_stream(F['stream'],C['channel_id'],'synthetic',C['owner_id'])
    monkeypatch.setattr(ingest,'poll',lambda **k:{})
    monkeypatch.setattr(ingest,'load_registry',lambda:{'reader':{'bot_token':'synthetic'}})
    monkeypatch.setattr(ingest_tick,'_log',lambda *a:None)
    monkeypatch.setattr(commands,'route',lambda msgs,*a,**k:([],msgs,[]))
    def ack(*args): raise OSError('synthetic ack failure')
    monkeypatch.setattr(ingest,'ack_seen',ack)
    monkeypatch.setattr(ingest,'ack_done',ack)
    calls=[]
    monkeypatch.setattr(dispatch,'dispatch',lambda *a,**k:calls.append(k) or True)
    ingest_tick.run(post=False)
    ingest_tick.run(post=False)
    assert len(calls)==1


def test_enqueue_keeps_origin_message_separate_from_action_identity(monkeypatch,tmp_path):
    import agent_task
    agent_task.store.init_db()
    items=[]
    monkeypatch.setattr(agent_task,'runs_root',lambda:str(tmp_path/'runs'))
    for action in C['agent_plan']['actions']:
        items.append(agent_task.enqueue(F['stream'],action['request'],msg_id=F['message_id'],
                                        idempotency_key=action['request']))
    for item in items:
        ext=agent_task.store.get_item(item['id'])['ext']
        assert ext[agent_task.EXT_MSG]==F['message_id']
    keys=[agent_task.store.get_item(item['id'])['idempotency_key'] for item in items]
    assert len(keys)==2 and keys[0]!=keys[1]


def test_dispatch_preserves_origin_and_separates_actions_channels_reactions(monkeypatch):
    import inbound
    calls=[]
    monkeypatch.setattr(dispatch,'get_state',lambda _:[])
    monkeypatch.setattr(dispatch,'get_work',lambda:[])
    monkeypatch.setattr(dispatch,'call_chain',lambda *a,**k:json.dumps(C['agent_plan']))
    def enqueue(stream,request,**kwargs):
        calls.append(kwargs)
        return {'id':request}
    monkeypatch.setattr(dispatch.agent_task,'enqueue',enqueue)
    sources=[{'channel_id':F['channels'][0]['id']},
             {'channel_id':F['channels'][1]['id']},
             {'channel_id':F['channels'][0]['id'],
              'inbound_id':inbound.identity('reaction',F['channels'][0]['id'],C['reaction']['key'])}]
    for source in sources:
        assert dispatch.dispatch(F['stream'],F['request'],post=False,msg_id=F['message_id'],**source)
        assert dispatch.dispatch(F['stream'],F['request'],post=False,msg_id=F['message_id'],**source)
    assert len(calls)==6
    assert all(row['msg_id']==F['message_id'] for row in calls)
    assert len({row['idempotency_key'] for row in calls})==6


@pytest.mark.parametrize('kind',['text','reaction'])
def test_tick_passes_origin_message_and_durable_identity(inbox,monkeypatch,kind):
    import inbound
    payload=F['message'] if kind=='text' else C['reaction']
    record=inbound.stage(F['stream'],C['channel_id'],
                         F['message_id'] if kind=='text' else payload['key'],
                         kind,payload,ingest.state_dir())
    monkeypatch.setattr(ingest,'poll',lambda **k:{})
    monkeypatch.setattr(ingest,'load_registry',lambda:{})
    monkeypatch.setattr(ingest_tick,'_log',lambda *a:None)
    monkeypatch.setattr(commands,'route',lambda msgs,*a,**k:([],msgs,[]))
    calls=[]
    monkeypatch.setattr(dispatch,'dispatch',lambda *a,**k:calls.append(k) or True)
    assert ingest_tick.run(post=False)['handled'][F['stream']]=='ok'
    assert len(calls)==1
    assert calls[0]['msg_id']==F['message_id']
    assert calls[0]['inbound_id']==record['id']


def _tick_world(inbox,monkeypatch,outcome):
    record=inbound.stage(F['stream'],C['channel_id'],F['message_id'],'text',F['message'],ingest.state_dir())
    monkeypatch.setattr(ingest,'poll',lambda **k:{})
    monkeypatch.setattr(ingest,'load_registry',lambda:{})
    monkeypatch.setattr(ingest_tick,'_log',lambda *a:None)
    monkeypatch.setattr(commands,'route',lambda msgs,*a,**k:([],msgs,[]))
    calls=[]
    monkeypatch.setattr(dispatch,'dispatch',lambda *a,**k:calls.append(k) or outcome)
    return record,calls

def test_unfinished_dispatch_stops_after_the_retry_cap_until_rearmed(inbox,monkeypatch):
    record,calls=_tick_world(inbox,monkeypatch,False)
    for _ in range(inbound.MAX_FAILURES+2):
        ingest_tick.run(post=False)
    assert len(calls)==inbound.MAX_FAILURES, 'a reply that never finishes is not retried forever'
    assert [call['final'] for call in calls]==[False]*(inbound.MAX_FAILURES-1)+[True]
    current=inbound.pending(ingest.state_dir())
    assert current==[]
    inbound.retry_command(record['id'],ingest.state_dir())
    ingest_tick.run(post=False)
    assert len(calls)==inbound.MAX_FAILURES+1, 'an explicit rearm gives it another attempt'

def test_tick_bounds_the_planner_by_its_time_budget(inbox,monkeypatch):
    _,calls=_tick_world(inbox,monkeypatch,True)
    ingest_tick.run(post=False)
    assert 0<calls[0]['timeout']<=ingest_tick.PLANNER_S

def test_tick_takes_no_new_record_after_the_intake_budget(inbox,monkeypatch):
    _,calls=_tick_world(inbox,monkeypatch,True)
    clock=iter([0.0,0.0]+[ingest_tick.INTAKE_S+1.0]*10)  # tick start, dispatch start, then past the window
    monkeypatch.setattr(ingest_tick.time,'monotonic',lambda:next(clock))
    ingest_tick.run(post=False)
    assert calls==[] and len(inbound.pending(ingest.state_dir()))==1

def test_idle_tick_does_not_load_the_dispatch_layer(monkeypatch):
    monkeypatch.setattr(ingest,'poll',lambda **k:{})
    monkeypatch.setattr(ingest,'pending_work',lambda:[])
    monkeypatch.setattr(ingest_tick,'_handlers',lambda:pytest.fail('idle tick imported dispatch'))
    assert ingest_tick.run(post=False)['handled']=={}
