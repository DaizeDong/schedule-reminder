import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'skills/schedule-reminder/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('action_fixtures', ROOT / 'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def module():
    assert (SCRIPTS / 'reminder_actions.py').is_file(), 'Owner action contract has not been implemented'
    return __import__('reminder_actions')


@pytest.fixture
def case(tmp_path, monkeypatch):
    database = tmp_path / 'work.sqlite3'
    workspace = tmp_path / 'private-output'
    workspace.mkdir()
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(database))
    monkeypatch.setenv('SCHEDULE_REMINDER_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AGENT_CENTER_RUNS', str(tmp_path / 'runs'))
    item = fixtures.action_database(database)
    return database, workspace, item


def request(actions, database, workspace, item, request_id='synthetic-request-01'):
    offers = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    return {'item_id': item['id'], 'action_id': 'agent', 'revision': offers['revision'], 'request_id': request_id}


def test_readonly_recommendation_never_submits_work(case):
    actions = module()
    database, workspace, item = case
    before = database.read_bytes()
    offer = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    assert offer['available'] and offer['offers'][0]['id'] == 'agent'
    assert offer['current'] is None and database.read_bytes() == before


def test_click_creates_one_linked_work_order_and_replays_without_mutation(case):
    actions = module()
    database, workspace, item = case
    payload = request(actions, database, workspace, item)
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    second = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert first['ok'] and first['status'] == 'queued'
    assert second['action']['id'] == first['action']['id']
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM items WHERE source='agent-center:work'").fetchone()[0] == 1
    current = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))['current']
    assert current['work_item_id'] == first['action']['work_item_id']


def test_different_request_ids_cannot_duplicate_active_work(case):
    actions = module()
    database, workspace, item = case
    payload = request(actions, database, workspace, item)
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    payload['request_id'] = 'synthetic-request-02'
    second = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert second['action']['id'] == first['action']['id']


def test_stale_revision_and_same_key_different_intent_are_rejected(case):
    actions = module()
    database, workspace, item = case
    payload = request(actions, database, workspace, item)
    stale = dict(payload, revision='sha256:stale')
    with pytest.raises(actions.ActionError, match='stale'):
        actions.start(stale, db_path=str(database), workspace_root=str(workspace))
    actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    with pytest.raises(actions.ActionError, match='conflict'):
        actions.start(dict(payload, action_id='arbitrary-command'), db_path=str(database), workspace_root=str(workspace))


def test_missing_workspace_or_legacy_schema_does_not_fall_back(case, tmp_path):
    actions = module()
    database, workspace, item = case
    offer = actions.inspect_item(str(database), item['id'], workspace_root=None)
    assert offer['available'] and [row['id'] for row in offer['offers']] == ['complete']
    legacy = fixtures.work_database(tmp_path / 'legacy.sqlite3')
    before = legacy.read_bytes()
    offer = actions.inspect_item(str(legacy), 'tracked', workspace_root=str(workspace))
    assert not offer['available'] and before == legacy.read_bytes()


def test_parallel_submissions_publish_exactly_one_work(case):
    from concurrent.futures import ThreadPoolExecutor
    actions = module()
    database, workspace, item = case
    payload = request(actions, database, workspace, item)
    def submit(index):
        return actions.start(dict(payload, request_id='synthetic-concurrent-%02d' % index),
                             db_path=str(database), workspace_root=str(workspace))
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(submit, range(12)))
    assert len({reply['action']['id'] for reply in replies}) == 1
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM items WHERE source='agent-center:work'").fetchone()[0] == 1


def test_stop_replay_and_stop_during_preparation_are_fenced(case, monkeypatch):
    import agent_task
    actions = module()
    database, workspace, item = case
    original = agent_task.enqueue
    stop_payloads = []
    def paused(*args, **kwargs):
        projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
        stop_payload = {'item_id': item['id'], 'action_id': projection['current']['id'],
                        'revision': projection['revision'], 'request_id': 'synthetic-stop-prepare'}
        stop_payloads.append(stop_payload)
        assert actions.stop(stop_payload, db_path=str(database))['status'] == 'stopped'
        return original(*args, **kwargs)
    monkeypatch.setattr(agent_task, 'enqueue', paused)
    reply = actions.start(request(actions, database, workspace, item), db_path=str(database), workspace_root=str(workspace))
    assert reply['status'] == 'stopped'
    assert actions.stop(stop_payloads[0], db_path=str(database))['status'] == 'stopped'
    with sqlite3.connect(database) as conn:
        assert all(json.loads(row[0]).get('x_agent_exec_state') != 'queued'
                   for row in conn.execute("SELECT ext FROM items WHERE source='agent-center:work'"))


def test_stop_queued_work_cancels_only_owned_work(case):
    actions = module()
    database, workspace, item = case
    reply = actions.start(request(actions, database, workspace, item), db_path=str(database), workspace_root=str(workspace))
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': reply['action']['id'], 'revision': projection['revision'],
               'request_id': 'synthetic-stop-queued'}
    assert actions.stop(payload, db_path=str(database))['status'] == 'stopped'
    from reminder_work_feed import read_work_feed
    stopped_work = next(row for row in read_work_feed(db_path=str(database))['items'] if row['id'] == reply['action']['work_item_id'])
    assert stopped_work['execution']['state'] == 'stopped'
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT state FROM items WHERE id=?', (reply['action']['work_item_id'],)).fetchone()[0] == 'cancelled'
    with pytest.raises(actions.ActionError, match='conflict'):
        actions.stop(dict(payload, item_id='another-origin'), db_path=str(database))


def test_cli_submit_and_feed_are_connected(case):
    import os
    import subprocess
    actions = module()
    database, workspace, item = case
    env = dict(os.environ, SCHEDULE_ACTION_WORKSPACE=str(workspace))
    payload = request(actions, database, workspace, item)
    def cli(verb, payload=None):
        return subprocess.run([sys.executable, str(SCRIPTS / 'reminder.py'), '--db', str(database), verb],
                              input=json.dumps(payload) if payload else '', env=env, text=True, encoding='utf-8', capture_output=True)
    reply = cli('work-action', payload)
    assert reply.returncode == 0, reply.stderr
    receipt = json.loads(reply.stdout)
    feed = json.loads(cli('work-feed').stdout)
    todo = next(row for row in feed['items'] if row['id'] == item['id'])
    assert todo['actions']['current']['id'] == receipt['action']['id']
    bad = cli('work-action', dict(payload, action_id='shell'))
    assert bad.returncode == 1 and json.loads(bad.stderr)['error_code'] == 'request_conflict'


def test_task_receipt_result_is_cas_and_unknown_is_not_replayed(case, monkeypatch):
    actions = module()
    database, workspace, item = case
    monkeypatch.setattr(actions, '_task_link', lambda *args: fixtures.task_control_binding())
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert first['dispatch']['task_id'] == 'synthetic/task'
    assert 'dispatch' not in actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    result = actions.record_result(first['action']['id'], {'ok':False,'status':'cleanup_uncertain'}, db_path=str(database))
    assert result['status'] == 'reconcile'
    with pytest.raises(actions.ActionError):
        actions.record_result(first['action']['id'], {'ok':True}, db_path=str(database))


def test_workspace_must_be_inside_owner_private_storage(case, tmp_path):
    actions = module()
    database, workspace, item = case
    assert actions._workspace(str(tmp_path.parent)) is None


def test_missing_submit_dependency_records_failure_and_allows_explicit_retry(case, monkeypatch):
    actions = module()
    database, workspace, item = case
    payload = request(actions, database, workspace, item)
    with monkeypatch.context() as missing:
        missing.setitem(sys.modules, 'agent_task', None)
        with pytest.raises(actions.ActionError, match='work_submission_failed'):
            actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    assert projection['current']['state'] == 'failed'
    assert actions.start(payload, db_path=str(database), workspace_root=str(workspace))['status'] == 'failed'
    retry = request(actions, database, workspace, item, 'synthetic-retry-after-import')
    assert actions.start(retry, db_path=str(database), workspace_root=str(workspace))['status'] == 'queued'
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM items WHERE source='agent-center:work'").fetchone()[0] == 1


def test_missing_stop_dependency_keeps_execution_unconfirmed_and_blocks_duplicate(case, monkeypatch):
    import agent_task
    actions = module()
    database, workspace, item = case
    started = actions.start(request(actions, database, workspace, item),
                            db_path=str(database), workspace_root=str(workspace))
    work_id = started['action']['work_item_id']
    assert agent_task.claim(work_id)
    assert agent_task.begin_spawn(work_id, 1)
    assert agent_task.start_runner(work_id, 1, 123, 456)
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': started['action']['id'],
               'revision': projection['revision'], 'request_id': 'synthetic-stop-import-failure'}
    with monkeypatch.context() as missing:
        missing.setitem(sys.modules, 'agent_tick', None)
        stopped = actions.stop(payload, db_path=str(database))
    assert stopped['status'] == 'reconcile'
    assert actions.stop(payload, db_path=str(database))['status'] == 'reconcile'
    next_click = request(actions, database, workspace, item, 'synthetic-click-after-stop-failure')
    reply = actions.start(next_click, db_path=str(database), workspace_root=str(workspace))
    assert reply['action']['id'] == started['action']['id'] and reply['status'] == 'reconcile'
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM items WHERE source='agent-center:work'").fetchone()[0] == 1


@pytest.mark.parametrize('already_running', [False, True])
def test_task_handoff_ack_releases_only_the_request_reservation(case, monkeypatch, already_running):
    import store
    actions = module()
    database, workspace, item = case
    monkeypatch.setattr(actions, '_task_link', lambda *args: fixtures.task_control_binding())
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    result = fixtures.task_control_result()
    if already_running:
        result.update(status='already_running', acknowledged=False, before='Running')
    reply = actions.record_result(first['action']['id'], result, db_path=str(database))
    assert reply['status'] == 'task_requested'
    assert reply['action']['task_id'] == first['dispatch']['task_id']
    assert store.get_item(item['id'])['state'] == item['state']
    projection = actions.inspect_item(str(database), item['id'])
    assert next(offer for offer in projection['offers'] if offer['id'] == 'complete')['enabled']
    replay = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert replay['action']['id'] == first['action']['id'] and 'dispatch' not in replay


@pytest.mark.parametrize('missing', ['schemaVersion', 'name', 'verb', 'acknowledged',
                                   'before', 'after', 'payload_success'])
def test_incomplete_task_ack_preserves_reconciliation(case, monkeypatch, missing):
    actions = module()
    database, workspace, item = case
    monkeypatch.setattr(actions, '_task_link', lambda *args: fixtures.task_control_binding())
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    result = fixtures.task_control_result()
    del result[missing]
    assert actions.record_result(first['action']['id'], result, db_path=str(database))['status'] == 'reconcile'
    projection = actions.inspect_item(str(database), item['id'])
    assert not next(offer for offer in projection['offers'] if offer['id'] == 'complete')['enabled']


@pytest.mark.parametrize('ok', [False, True])
def test_bare_task_result_preserves_reconciliation(case, monkeypatch, ok):
    actions = module()
    database, workspace, item = case
    monkeypatch.setattr(actions, '_task_link', lambda *args: fixtures.task_control_binding())
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert actions.record_result(first['action']['id'], {'ok': ok}, db_path=str(database))['status'] == 'reconcile'
    projection = actions.inspect_item(str(database), item['id'])
    assert not next(offer for offer in projection['offers'] if offer['id'] == 'complete')['enabled']


def test_legacy_task_requested_without_ack_does_not_unlock_actions(case, monkeypatch):
    import reminder_action_store
    actions = module()
    database, workspace, item = case
    monkeypatch.setattr(actions, '_task_link', lambda *args: fixtures.task_control_binding())
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    reminder_action_store.update(str(database), first['action']['id'], 'task_requested', {'ok': True})
    projection = actions.inspect_item(str(database), item['id'])
    assert projection['current']['state'] == 'reconcile'
    assert not next(offer for offer in projection['offers'] if offer['id'] == 'complete')['enabled']


def test_task_ack_cannot_substitute_another_controller_name(case, monkeypatch):
    actions = module()
    database, workspace, item = case
    binding = fixtures.task_control_binding()
    monkeypatch.setattr(actions, '_task_link', lambda *args: binding)
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    result = fixtures.task_control_result()
    result['name'] += '-other'
    result['expected_task'] = dict(binding, name=result['name'])
    assert actions.record_result(first['action']['id'], result, db_path=str(database))['status'] == 'reconcile'
    projection = actions.inspect_item(str(database), item['id'])
    assert not next(offer for offer in projection['offers'] if offer['id'] == 'complete')['enabled']


def test_historical_task_ack_requires_its_original_target_binding(case, monkeypatch):
    import reminder_action_store
    actions = module()
    database, workspace, item = case
    binding = fixtures.task_control_binding()
    monkeypatch.setattr(actions, '_task_link', lambda *args: binding)
    payload = dict(request(actions, database, workspace, item), action_id='task')
    first = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    result = fixtures.task_control_result()
    for expected in (None, dict(binding, task_id=binding['task_id'] + '-other'),
                     dict(binding, name=binding['name'] + '-other')):
        result['expected_task'] = expected
        reminder_action_store.update(str(database), first['action']['id'], 'task_requested', result)
        assert actions.inspect_item(str(database), item['id'])['current']['state'] == 'reconcile'


def test_legacy_stopped_receipt_with_queued_work_does_not_unlock_actions(case):
    import reminder_action_store
    actions = module()
    database, workspace, item = case
    first = actions.start(request(actions, database, workspace, item),
                          db_path=str(database), workspace_root=str(workspace))
    reminder_action_store.update(str(database), first['action']['id'], 'stopped')
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    assert projection['current']['state'] == 'reconcile'
    assert not any(offer['enabled'] for offer in projection['offers'])


def test_manual_completion_works_without_executor_and_replays_one_receipt(case):
    import store
    actions = module()
    database, _, item = case
    projection = actions.inspect_item(str(database), item['id'])
    offer = next((row for row in projection['offers'] if row['id'] == 'complete'), None)
    assert offer and offer['enabled'] and offer['kind'] == 'complete'
    payload = {'item_id': item['id'], 'action_id': 'complete', 'revision': projection['revision'],
               'request_id': 'synthetic-complete-01'}
    first = actions.start(payload, db_path=str(database))
    second = actions.start(payload, db_path=str(database))
    assert first['ok'] and first['status'] == 'done'
    assert first['action']['id'] == second['action']['id']
    assert not first.get('wakeup') and 'dispatch' not in first and not first['action']['work_item_id']
    done = store.get_item(item['id'], db_path=str(database))
    assert done['state'] == 'done' and done['progress'] == 100 and done['end_at']
    assert done['source'] == item['source'] and done['description'] == item['description']
    assert not actions.inspect_item(str(database), item['id'])['offers']
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM items').fetchone()[0] == 1
        assert conn.execute('SELECT count(*) FROM work_actions').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='status_change' AND to_state='done'").fetchone()[0] == 1


def test_manual_completion_refuses_stale_and_active_execution(case):
    actions = module()
    database, workspace, item = case
    payload = dict(request(actions, database, workspace, item), action_id='complete')
    with pytest.raises(actions.ActionError, match='stale'):
        actions.start(dict(payload, revision='sha256:stale'), db_path=str(database))
    actions.start(request(actions, database, workspace, item), db_path=str(database), workspace_root=str(workspace))
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    offer = next(row for row in projection['offers'] if row['id'] == 'complete')
    assert not offer['enabled'] and offer['reason']
    with pytest.raises(actions.ActionError, match='another_action_active'):
        actions.start(dict(payload, request_id='synthetic-complete-active', revision=projection['revision']), db_path=str(database))


def test_manual_completion_preserves_dependency_guard_and_rolls_back_receipt(tmp_path):
    import store
    actions = module()
    database = tmp_path / 'blocked.sqlite3'
    item = fixtures.action_database(database, blocked=True)
    projection = actions.inspect_item(str(database), item['id'])
    payload = {'item_id': item['id'], 'action_id': 'complete', 'revision': projection['revision'],
               'request_id': 'synthetic-blocked-complete'}
    with pytest.raises(store.SkillError, match='depends-on'):
        actions.start(payload, db_path=str(database))
    assert store.get_item(item['id'], db_path=str(database))['state'] == 'blocked'
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM work_actions').fetchone()[0] == 0


def test_manual_completion_is_not_offered_for_signals(tmp_path):
    actions = module()
    database = tmp_path / 'signal.sqlite3'
    item = fixtures.action_database(database, source='daily-hotspots')
    projection = actions.inspect_item(str(database), item['id'])
    assert not any(row['id'] == 'complete' for row in projection['offers'])
    with pytest.raises(actions.ActionError, match='action_unavailable'):
        actions.start({'item_id': item['id'], 'action_id': 'complete', 'revision': projection['revision'],
                       'request_id': 'synthetic-signal-complete'}, db_path=str(database))


def test_parallel_manual_completion_commits_once(case):
    from concurrent.futures import ThreadPoolExecutor
    actions = module()
    database, workspace, item = case
    payload = dict(request(actions, database, workspace, item), action_id='complete')
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: actions.start(payload, db_path=str(database)), range(8)))
    assert len({reply['action']['id'] for reply in replies}) == 1
    with pytest.raises(actions.ActionError, match='stale'):
        actions.start(dict(payload, request_id='synthetic-complete-new-click'), db_path=str(database))
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT count(*) FROM work_actions').fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='status_change' AND to_state='done'").fetchone()[0] == 1


def test_latest_generation_retains_unknown_cleanup_reservation(case):
    import agent_task
    import store
    actions = module()
    database, workspace, item = case
    action = actions.start(request(actions, database, workspace, item),
                           db_path=str(database), workspace_root=str(workspace))['action']
    work_id = action['work_item_id']
    for generation in (1, 2):
        assert agent_task.claim(work_id)
        assert agent_task.begin_spawn(work_id, generation)
        assert agent_task.start_runner(work_id, generation, 123, 456)
        if generation == 2:
            assert agent_task.child_started(work_id, generation, {'phase': 'actor'})
            assert agent_task.child_finished(work_id, generation, {'phase': 'actor'}, quiescent=False)
        assert agent_task.finish(work_id, False, exec_state_value='review_unavailable', generation=generation)
        if generation == 1:
            assert agent_task.release(work_id, generation)
            store.transition(work_id, 'pending', ext={'x_agent_exec_state': 'queued'})
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    assert projection['current']['state'] == 'reconcile'
    assert not any(offer['enabled'] for offer in projection['offers'])


def test_stale_action_revision_cannot_stop_a_replacement_generation(case, monkeypatch):
    import agent_task
    import agent_tick
    import store
    actions = module()
    database, workspace, item = case
    action = actions.start(request(actions, database, workspace, item),
                           db_path=str(database), workspace_root=str(workspace))['action']
    work_id = action['work_item_id']
    assert agent_task.claim(work_id)
    assert agent_task.begin_spawn(work_id, 1)
    assert agent_task.start_runner(work_id, 1, 123, 456)
    first = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': action['id'], 'revision': first['revision'],
               'request_id': 'synthetic-stop-old-generation'}
    assert agent_task.finish(work_id, False, generation=1)
    assert agent_task.release(work_id, 1)
    store.transition(work_id, 'pending', ext={'x_agent_exec_state': 'queued'})
    assert agent_task.claim(work_id)
    assert agent_task.begin_spawn(work_id, 2)
    assert agent_task.start_runner(work_id, 2, 123, 789)
    calls = []
    monkeypatch.setattr(agent_tick, 'stop', lambda *args, **kwargs: calls.append((args, kwargs)) or [])
    with pytest.raises(actions.ActionError, match='stale_recommendation'):
        actions.stop(payload, db_path=str(database))
    assert calls == []
    assert agent_task.owns(work_id, 2)


def test_stop_passes_the_captured_generation_to_runtime(case, monkeypatch):
    import agent_task
    import agent_tick
    actions = module()
    database, workspace, item = case
    action = actions.start(request(actions, database, workspace, item),
                           db_path=str(database), workspace_root=str(workspace))['action']
    work_id = action['work_item_id']
    assert agent_task.claim(work_id)
    assert agent_task.begin_spawn(work_id, 1)
    assert agent_task.start_runner(work_id, 1, 123, 456)
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': action['id'], 'revision': projection['revision'],
               'request_id': 'synthetic-stop-captured-generation'}
    calls = []
    monkeypatch.setattr(agent_tick, 'stop', lambda *args, **kwargs: calls.append(kwargs) or [])
    assert actions.stop(payload, db_path=str(database))['status'] == 'reconcile'
    assert calls[0]['expected_generation'] == 1


def test_stop_crash_after_commit_revokes_queued_work(case, monkeypatch):
    import agent_tick
    import agent_task
    actions = module()
    database, workspace, item = case
    started = actions.start(request(actions, database, workspace, item),
                            db_path=str(database), workspace_root=str(workspace))
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': started['action']['id'],
               'revision': projection['revision'], 'request_id': 'synthetic-stop-crash'}
    def crash(*args, **kwargs):
        raise SystemExit('synthetic process exit')
    monkeypatch.setattr(agent_tick, 'stop', crash)
    try:
        actions.stop(payload, db_path=str(database))
    except SystemExit:
        pass
    assert not agent_task.claim(started['action']['work_item_id'])
    assert actions.inspect_item(str(database), item['id'])['current']['state'] in ('stopped', 'reconcile')


def test_stop_crash_after_commit_revokes_running_ownership(case, monkeypatch):
    import agent_tick
    import agent_task
    actions = module()
    database, workspace, item = case
    started = actions.start(request(actions, database, workspace, item),
                            db_path=str(database), workspace_root=str(workspace))
    work_id = started['action']['work_item_id']
    assert agent_task.claim(work_id)
    assert agent_task.begin_spawn(work_id, 1)
    assert agent_task.start_runner(work_id, 1, 123, 456)
    projection = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    payload = {'item_id': item['id'], 'action_id': started['action']['id'],
               'revision': projection['revision'], 'request_id': 'synthetic-stop-running-crash'}
    def crash(*args, **kwargs):
        raise SystemExit('synthetic process exit')
    monkeypatch.setattr(agent_tick, 'stop', crash)
    with pytest.raises(SystemExit):
        actions.stop(payload, db_path=str(database))
    assert not agent_task.owns(work_id, 1)
    assert actions.inspect_item(str(database), item['id'])['current']['state'] == 'reconcile'
    assert not agent_task.cancel(work_id).get('_err')
    assert agent_task.release(work_id, 1)
    assert actions.inspect_item(str(database), item['id'])['current']['state'] == 'stopped'


def test_work_feed_proves_the_action_workspace_once_not_per_todo(case, monkeypatch):
    """The console reader has a 20 s budget; one PRIVATE proof costs ~3 s on a real companion.

    Proving the workspace again for every tracked todo made work-feed run for minutes."""
    import threading
    import store
    actions = module()
    database, workspace, item = case
    todos = [item['id']] + [store.add_item('Acme follow-up %d' % n, source='user', db_path=str(database))['id']
                            for n in range(4)]
    calls = []
    proven = Path(workspace).resolve()
    monkeypatch.setattr(actions, '_workspace', lambda root: calls.append(root) or proven)
    monkeypatch.setenv('SCHEDULE_ACTION_WORKSPACE', str(workspace))
    from reminder_work_feed import read_work_feed
    feed = read_work_feed(db_path=str(database))
    assert feed['available'], feed
    offers = {row['id']: [offer['id'] for offer in row['actions']['offers']] for row in feed['items'] if row['id'] in todos}
    assert set(offers) == set(todos) and all('agent' in ids for ids in offers.values())
    assert calls == [str(workspace)]
    assert not any(thread.name == 'workspace-proof' for thread in threading.enumerate())


def test_one_private_proof_per_work_action_and_per_stop(case, monkeypatch):
    """The console gives work-action and work-action-stop 30 s; one PRIVATE proof costs ~3 s live.

    Each CLI command proved the same companion again for the database, the action workspace, the
    run directory, every lock and every record (22 full proofs for one start). One process must
    prove an unchanged companion once and reuse it everywhere, the DB proof included."""
    import private_data
    actions = module()
    database, workspace, item = case
    live_queries = []
    query = private_data._query
    monkeypatch.setattr(private_data, '_query', lambda argv: live_queries.append(argv[3]) or query(argv))
    reset = getattr(private_data, 'clear_proof_memo', lambda: None)

    payload = request(actions, database, workspace, item)
    reset()  # a CLI command is a fresh process
    live_queries.clear()
    started = actions.start(payload, db_path=str(database), workspace_root=str(workspace))
    assert started['ok'] and started['status'] == 'queued', started
    assert live_queries == ['example-owner/private-data']

    offer = actions.inspect_item(str(database), item['id'], workspace_root=str(workspace))
    stop = {'item_id': item['id'], 'action_id': offer['current']['id'], 'revision': offer['revision'],
            'request_id': 'synthetic-stop-01'}
    reset()
    live_queries.clear()
    stopped = actions.stop(stop, db_path=str(database), workspace_root=str(workspace))
    assert stopped['status'] == 'stopped', stopped
    assert live_queries == ['example-owner/private-data']
