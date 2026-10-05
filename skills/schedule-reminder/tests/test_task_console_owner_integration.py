"""Real consumer source with generated storage; never registers or runs a task."""
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
CONSOLE_ROOT = os.environ.get('SCHEDULE_TEST_TASK_CONSOLE_ROOT')
pytestmark = pytest.mark.skipif(not CONSOLE_ROOT,
    reason='Set SCHEDULE_TEST_TASK_CONSOLE_ROOT for real Task Console consumer integration')


@pytest.fixture
def consumer(monkeypatch):
    root = Path(CONSOLE_ROOT)
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    monkeypatch.syspath_prepend(str(root / 'scripts/task_console'))
    spec = importlib.util.spec_from_file_location('console_owner_fixtures', root / 'tools/make_fixtures.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_source_console_declaration_compiles_with_synthetic_bindings(consumer):
    from task_console.registration import _compile
    manifest = json.loads((ROOT / '.console.json').read_text(encoding='utf-8'))
    request = consumer.example_request()
    template = next(iter(request['bindings']['tasks'].values()))
    request['components'] = [manifest]
    request['bindings']['tasks'] = {}
    for index, task in enumerate(manifest['tasks']):
        binding = deepcopy(template)
        binding['name'] += str(index)
        binding['checks'] = [{'id': check['id'], 'legacy': deepcopy(template['checks'][0]['legacy'])}
                             for check in task['checks']]
        request['bindings']['tasks'][manifest['component'] + '/' + task['id']] = binding
    before = deepcopy(request)
    result = _compile({'request': request})
    assert request == before
    assert {row['task_id'] for row in result['task_specs']} == set(request['bindings']['tasks'])
    assert len(result['task_specs']) == 4
    work = next(row for row in result['task_specs']
                if row['task_id'] == 'schedule-reminder/agentcenterworktick')
    assert work['kind'] == 'dispatcher'
    assert result['mode'] == 'read-only' and not result['applicable']


def test_consumer_feed_and_manual_completion_use_owner_cli(consumer, tmp_path):
    from task_console import work_status, work_actions
    from tools.make_fixtures import action_database
    import store

    database = tmp_path / 'work.sqlite3'
    item = action_database(database)
    env = dict(os.environ, TASK_CONSOLE_REMINDER_CLI=str(ROOT / 'skills/schedule-reminder/scripts/reminder.py'),
               TASK_CONSOLE_REMINDER_DB=str(database))
    feed = work_status.read_configured(env)
    assert feed['available']
    projected = next(row for row in feed['items'] if row['id'] == item['id'])
    request = {'item_id': item['id'], 'action_id': 'complete',
               'revision': projected['actions']['revision'], 'request_id': 'synthetic-console-01'}
    result = work_actions.submit(request, env=env)
    replay = work_actions.submit(request, env=env)
    assert result['ok'] and result['status'] == 'done'
    assert replay['action']['id'] == result['action']['id']
    assert store.get_item(item['id'], db_path=database)['state'] == 'done'


def test_actual_compiler_binding_governs_task_action_ack(consumer, tmp_path):
    from task_console.registration import _compile
    from tools.make_fixtures import action_database, task_control_result
    import reminder_actions
    import reminder_link_review
    import store

    database = tmp_path / 'linked-work.sqlite3'
    item = action_database(database)
    task_input = consumer.example_request()
    task = _compile({'request': task_input})['task_specs'][0]
    decisions = {'schemaVersion': 1, 'links': {item['id']: task['task_id']},
                 'unmapped_items': 'reviewed-unlinked'}
    review = reminder_link_review.build_review(db_path=database, decisions=decisions, task_request=task_input)
    reminder_link_review.apply_review(review, review['review_revision'], db_path=database, task_request=task_input)
    projection = reminder_actions.inspect_item(str(database), item['id'])
    offer = next(row for row in projection['offers'] if row['id'] == 'task')
    assert offer['task_binding']['name'] == task['name']
    request = {'item_id': item['id'], 'action_id': 'task', 'revision': projection['revision'],
               'request_id': 'synthetic-console-task-01'}
    action = reminder_actions.start(request, db_path=str(database))
    assert action['dispatch']['task_id'] == task['task_id']
    result = dict(task_control_result(), name=task['name'])
    reply = reminder_actions.record_result(action['action']['id'], result, db_path=str(database))
    assert reply['status'] == 'task_requested'
    assert store.get_item(item['id'], db_path=database)['state'] == 'pending'
