"""Real storage and CLI tests; no model, worker, or external message is run."""
import json
from pathlib import Path
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import dispatch
import store
import agent_task
from tools.make_fixtures import dispatch_case, dispatch_occurrences, watchdog_database


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(tmp_path / 'pool.sqlite3'))
    monkeypatch.setenv('AGENT_CENTER_RUNS', str(tmp_path / 'runs'))
    store.init_db()
    return tmp_path


def items():
    return store.list_items(limit=100)['items']


def test_same_create_replayed_preserves_edited_terminal_item(pool):
    plan, _ = dispatch_case(pool)
    dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1')
    item = items()[0]
    store.update_item(item['id'], title='Acme edited title')
    store.transition(item['id'], 'done')
    dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1')
    assert len(items()) == 1
    assert items()[0]['title'] == 'Acme edited title'
    assert items()[0]['state'] == 'done'
    assert items()[0]['idempotency_key']


def test_distinct_dates_can_create_same_titled_occurrences(pool):
    for plan, (stream, request) in zip(dispatch_occurrences(),
            [('infra', 'message-1'), ('infra', 'message-2'), ('support', 'message-1')]):
        dispatch.execute(stream, dispatch.STREAMS[stream], plan, [], request_id=request)
    assert len(items()) == 3


def test_concurrent_create_retries_have_one_identity(pool):
    plan, _ = dispatch_case(pool)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: dispatch.execute(
            'infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1'), range(4)))
    assert len(items()) == 1
    assert all(not r['skipped'] for r in results)


def test_action_identity_survives_saved_plan_field_order(pool):
    _, plan = dispatch_case(pool)
    action = dict(plan['actions'][0], id='synthetic-origin')
    saved = json.loads(json.dumps(action, sort_keys=True))
    assert dispatch._action_identity('infra', 'synthetic-occurrence', 0, action) == (
        dispatch._action_identity('infra', 'synthetic-occurrence', 0, saved))


@pytest.mark.parametrize('legacy', ['canonical-complete', 'original-order', 'missing', 'failed'])
def test_legacy_saved_plan_never_reexecutes_unproved_action_identity(pool, monkeypatch, legacy):
    import inbound
    plan, _ = dispatch_case(pool)
    origin = store.add_item(plan['actions'][0]['title'])
    action = {'title': origin['title'], 'op': 'create'}
    source = inbound.identity('request', 'infra', 'synthetic-legacy')
    key = ("dispatch:" + inbound.identity('infra', source, 0, action) if legacy == 'original-order'
           else dispatch._action_identity('infra', source, 0, action))
    with inbound.dispatch_record('infra', source) as (record, save):
        record.update(plan={'actions': [action]}, authorized_ids=[], authorized_work_ids=[],
                      outcomes={} if legacy == 'missing' else {
                          key: {'op': 'create', 'status': 'failed' if legacy == 'failed' else 'succeeded',
                                'id': origin['id'], 'decision': 'created', 'reason': 'synthetic failure'}})
        save()
    monkeypatch.setattr(dispatch, 'get_state', lambda _: [])
    monkeypatch.setattr(dispatch, 'get_work', lambda: [])
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **k: pytest.fail('legacy plan was replaced'))
    monkeypatch.setattr(dispatch, '_rem', lambda *a, **k: pytest.fail('unproved legacy identity mutated work'))
    before = items()
    assert dispatch.dispatch('infra', origin['title'], request_id='synthetic-legacy', post=False) is (
        legacy == 'canonical-complete')
    assert items() == before


def test_agent_retry_keeps_one_order_and_original_request(pool):
    _, plan = dispatch_case(pool)
    first = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1')
    second = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1')
    assert first['enqueued'] == second['enqueued']
    assert len(items()) == 1
    assert agent_task.read_request(items()[0]) == plan['actions'][0]['request']


def test_dispatch_retry_reuses_first_plan_even_if_model_would_change_it(pool, monkeypatch):
    plan, _ = dispatch_case(pool)
    calls = []
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **k: calls.append(1) or json.dumps(plan))
    assert dispatch.dispatch('infra', 'Acme request', request_id='message-1', post=False)
    plan['actions'].append(dict(plan['actions'][0], title='Another Acme task'))
    assert dispatch.dispatch('infra', 'Acme request', request_id='message-1', post=False)
    assert len(items()) == 1 and len(calls) == 1


def test_generic_stream_can_see_existing_items_and_follow_up_without_duplication(pool):
    plan, _ = dispatch_case(pool)
    dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-1')
    state = dispatch.get_state(dict(dispatch.STREAMS['infra'], source='agent-center:infra'))
    assert [it['id'] for it in state] == [items()[0]['id']]
    followup = {'actions': [{'op': 'update', 'id': state[0]['id'], 'note': 'Include Acme appendix'}]}
    for _ in range(2):
        dispatch.execute('infra', dispatch.STREAMS['infra'], followup, state, request_id='message-2')
    assert len(items()) == 1
    assert items()[0]['description'].count('Include Acme appendix') == 1


def test_follow_up_rejects_unshown_ids(pool):
    item = store.add_item('Acme existing')
    plan = {'actions': [{'op': 'update', 'id': item['id'], 'note': 'Acme appendix'}]}
    result = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-2')
    assert result['skipped'] and not store.get_item(item['id'])['description']


def test_state_read_failure_never_becomes_empty_pool(monkeypatch):
    monkeypatch.setattr(dispatch, '_rem', lambda *a: {'_err': 'unavailable'})
    with pytest.raises(RuntimeError):
        dispatch.get_state(dict(dispatch.STREAMS['infra'], source='agent-center:infra'))


def test_reviewed_watchdogs_are_signals_without_hiding_other_source_work(pool):
    from reminder_work_feed import read_work_feed
    feed = read_work_feed(db_path=str(watchdog_database(pool / 'watchdogs.sqlite3')))
    assert feed['coverage']['roles'] == {'signal': 2, 'tracked_item': 1}
    assert sum(row['role'] == 'signal' for row in feed['items']) == 2


def test_ingest_keeps_source_identity_when_batch_changes(pool, monkeypatch):
    import ingest_tick
    import ingest
    import inbound
    monkeypatch.setattr(ingest_tick, '_log', lambda *a: None)
    monkeypatch.setattr(ingest, 'load_registry', lambda: {})
    monkeypatch.setattr(ingest, 'poll_all_reactions', lambda **k: {})
    message = {'id': 'message-1', 'content': 'Prepare Acme report'}
    def poll(**kwargs):
        inbound.stage('infra', 'channel-1', message['id'], 'text', message, ingest.state_dir())
        return {'infra': 1}
    monkeypatch.setattr(ingest, 'poll_all', poll)
    captured = []
    monkeypatch.setattr(dispatch, 'dispatch', lambda *a, **k: captured.append(k) or True)
    ingest_tick.run(post=False)
    assert captured[0]['msg_id'] == 'message-1'
    assert captured[0]['inbound_id'] == inbound.identity('text', 'channel-1', 'message-1')
    assert captured[0]['channel_id'] == 'channel-1'


def _lose_first_outcome_write(monkeypatch):
    import inbound
    write = inbound._write
    lost = []

    def fail_once(path, value):
        if value.get('outcomes') and not lost:
            lost.append(True)
            raise OSError('synthetic outcome write lost')
        return write(path, value)

    monkeypatch.setattr(inbound, '_write', fail_once)
    return lost


@pytest.mark.parametrize('operation', ['update', 'agent'])
def test_saved_authorization_recovers_existing_receipt_after_origin_finishes(pool, monkeypatch, operation):
    create, agent = dispatch_case(pool)
    origin = store.add_item(create['actions'][0]['title'])
    action = ({'op': 'update', 'id': origin['id'], 'note': agent['actions'][0]['request']}
              if operation == 'update' else dict(agent['actions'][0], id=origin['id']))
    plan = {'actions': [action]}
    plans = []
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **k: plans.append(1) or json.dumps(plan))
    lost = _lose_first_outcome_write(monkeypatch)
    logs = []
    with pytest.raises(OSError, match='outcome write lost'):
        dispatch.dispatch('infra', action.get('note') or action['request'],
                          request_id='synthetic-recovery', post=False, log=logs.append)
    assert lost == [True]
    import inbound
    source = inbound.identity('request', 'infra', 'synthetic-recovery')
    with inbound.dispatch_record('infra', source) as (record, _):
        assert record['action_identity_version'] == 2
        assert record['action_keys'] == [dispatch._action_identity('infra', source, 0, action)]
    if operation == 'agent':
        assert len(items()) == 2, logs
    else:
        assert action['note'] in store.get_item(origin['id'])['description'], logs
    store.transition(origin['id'], 'done')
    before = items()
    mutation = 'append_dispatch_note' if operation == 'update' else 'enqueue'
    owner = store if operation == 'update' else agent_task
    monkeypatch.setattr(owner, mutation, lambda *a, **k: pytest.fail('historical authorization mutated work'))
    assert dispatch.dispatch('infra', action.get('note') or action['request'],
                             request_id='synthetic-recovery', post=False, log=logs.append), logs
    assert items() == before
    assert plans == [1]


@pytest.mark.parametrize('operation', ['update', 'agent'])
def test_historical_authorization_without_matching_receipt_cannot_mutate(pool, monkeypatch, operation):
    create, agent = dispatch_case(pool)
    origin = store.add_item(create['actions'][0]['title'])
    store.transition(origin['id'], 'done')
    action = ({'op': 'update', 'id': origin['id'], 'note': agent['actions'][0]['request']}
              if operation == 'update' else dict(agent['actions'][0], id=origin['id']))
    before = items()
    mutation = 'append_dispatch_note' if operation == 'update' else 'enqueue'
    owner = store if operation == 'update' else agent_task
    monkeypatch.setattr(owner, mutation, lambda *a, **k: pytest.fail('missing receipt triggered mutation'))
    result = dispatch.execute('infra', dispatch.STREAMS['infra'], {'actions': [action]}, [],
                              request_id='synthetic-missing', authorized_ids=[origin['id']])
    assert result['skipped'] and not result['updated'] and not result['enqueued']
    assert items() == before


@pytest.mark.parametrize('damage', ['preparing', 'invalid-state', 'origin', 'request-hash', 'request-file', 'source'])
def test_historical_work_receipt_must_prove_published_matching_request(pool, monkeypatch, damage):
    create, agent = dispatch_case(pool)
    origin = store.add_item(create['actions'][0]['title'])
    action = dict(agent['actions'][0], id=origin['id'])
    plan = {'actions': [action]}
    first = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [origin], request_id='synthetic-damaged')
    assert len(first['enqueued']) == 1
    work = store.get_item(first['enqueued'][0])
    if damage in ('preparing', 'invalid-state'):
        store.update_item(work['id'], ext={agent_task.EXT_STATE: damage})
    elif damage == 'origin':
        store.update_item(work['id'], ext={'x_console_origin_item': 'synthetic-other-origin'})
    elif damage == 'request-hash':
        store.update_item(work['id'], ext={agent_task.EXT_REQUEST_SHA: '0' * 64})
    elif damage == 'request-file':
        (Path(agent_task.run_dir(work)) / 'request.txt').write_text(origin['title'], encoding='utf-8')
    else:
        store.update_item(work['id'], source='synthetic-other-source')
    store.transition(origin['id'], 'done')
    before = items()
    monkeypatch.setattr(agent_task, 'enqueue', lambda *a, **k: pytest.fail('damaged receipt mutated work'))
    result = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [],
                              request_id='synthetic-damaged', authorized_ids=[origin['id']])
    assert result['failed'] and not result['enqueued']
    assert items() == before


@pytest.mark.parametrize('retry_state', ['same-generation', 'replacement-generation', 'missing-generation'])
def test_saved_stop_authorization_cannot_stop_replacement_generation(pool, monkeypatch, retry_state):
    import inbound
    import agent_tick
    _, plan = dispatch_case(pool)
    work = agent_task.enqueue('infra', plan['actions'][0]['request'], workspace=str(pool))
    assert agent_task.claim(work['id'])
    action = {'op': 'stop', 'id': work['id']}
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **k: json.dumps({'actions': [action]}))
    monkeypatch.setattr(agent_tick, '_log', lambda *a: None)
    monkeypatch.setattr(agent_tick, '_post_owner', lambda *a, **k: True)
    _lose_first_outcome_write(monkeypatch)
    with pytest.raises(OSError, match='outcome write lost'):
        dispatch.dispatch('infra', 'Stop the synthetic Acme work', request_id='synthetic-stop', post=False)
    assert store.get_item(work['id'])['state'] == 'cancelled'
    if retry_state == 'replacement-generation':
        assert agent_task.release(work['id'], 1)
        store.transition(work['id'], 'pending')
        store.update_item(work['id'], ext={agent_task.EXT_STATE: agent_task.STATE_QUEUED})
        assert agent_task.claim(work['id'])
        assert agent_task.operation(work['id'])['generation'] == 2
    elif retry_state == 'missing-generation':
        source = inbound.identity('request', 'infra', 'synthetic-stop')
        with inbound.dispatch_record('infra', source) as (record, save):
            record.pop('authorized_work_generations', None)
            save()
    before = items()
    assert dispatch.dispatch('infra', 'Stop the synthetic Acme work',
                             request_id='synthetic-stop', post=False) is (retry_state == 'same-generation')
    assert items() == before


@pytest.mark.parametrize('operation,label', [('update', '更新1'), ('reuse', '复用1')])
def test_confirmation_reports_followup_and_reuse_with_affected_id(pool, monkeypatch, operation, label):
    create, agent = dispatch_case(pool)
    dispatch.execute('infra', dispatch.STREAMS['infra'], create, [], request_id='synthetic-seed')
    origin = items()[0]
    plan = ({'actions': [{'op': 'update', 'id': origin['id'], 'note': agent['actions'][0]['request']}]}
            if operation == 'update' else create)
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **k: json.dumps(plan))
    posts = []
    monkeypatch.setattr(dispatch, '_post', lambda stream, text, *a, **k: posts.append(text) or True)
    assert dispatch.dispatch('infra', agent['actions'][0]['request'],
                             request_id='synthetic-confirmation', post=False)
    assert label in posts[0]
    assert origin['id'] in posts[0]
