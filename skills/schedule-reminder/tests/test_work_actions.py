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
    assert not offer['available']
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
    monkeypatch.setattr(actions, '_task_link', lambda *args: 'synthetic/task')
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
