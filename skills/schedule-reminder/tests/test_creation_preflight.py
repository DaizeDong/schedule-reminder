"""Creation behavior with generated inputs and real isolated owner storage."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import store
import dispatch
import agent_task
from tools.make_fixtures import creation_case, dispatch_case


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(tmp_path / 'pool.sqlite3'))
    monkeypatch.setenv('AGENT_CENTER_RUNS', str(tmp_path / 'runs'))
    store.init_db()
    return tmp_path


def test_cross_source_equivalent_creates_reuse_one_record(pool):
    first = store.ensure_item(**creation_case())
    second = store.ensure_item(**creation_case('synthetic-b', 'request-b'))
    assert first['decision'] == 'created'
    assert second['decision'] == 'reused'
    assert first['item']['id'] == second['item']['id']
    assert len(store.list_items(limit=100)['items']) == 1


def test_preflight_is_read_only_and_includes_other_producers(pool):
    original = store.add_item(**creation_case())
    before = store.get_events(original['id'])
    result = store.creation_preflight(**creation_case('synthetic-b', 'request-b'))
    assert result['decision'] == 'reuse'
    assert result['matches'][0]['item']['id'] == original['id']
    assert store.get_events(original['id']) == before


def test_concurrent_new_request_keys_share_the_same_item(pool):
    def create(index):
        return store.ensure_item(**creation_case('synthetic-%s' % index, 'request-%s' % index))
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(create, range(12)))
    assert len({r['item']['id'] for r in results}) == 1
    assert sum(r['decision'] == 'created' for r in results) == 1


@pytest.mark.parametrize('field,value', [
    ('due_at', '2030-05-02T12:00:00Z'),
    ('project', 'Acme other project'),
    ('alarms', [{'lead': 7200}]),
])
def test_distinct_occurrences_and_targets_remain_separate(pool, field, value):
    first = store.ensure_item(**creation_case())
    changed = creation_case('synthetic-b', 'request-b')
    changed[field] = value
    second = store.ensure_item(**changed)
    assert first['item']['id'] != second['item']['id']


def test_external_identity_change_requires_content_review(pool):
    first = store.ensure_item(**creation_case())
    proposed = creation_case('synthetic-b', 'request-b')
    proposed['ext'] = {'x_synthetic_order_id': 'order-b'}
    with pytest.raises(store.SkillError, match='creation review required'):
        store.ensure_item(**proposed)
    check = store.creation_preflight(**proposed)
    assert check['matches'][0]['item']['id'] == first['item']['id']
    second = store.ensure_item(**proposed, distinct_reason='A separately requested synthetic order.')
    assert second['item']['id'] != first['item']['id']


def test_reuse_retry_preserves_edits_and_does_not_revive_completed_item(pool):
    first = store.ensure_item(**creation_case())
    request = creation_case('synthetic-b', 'request-b')
    store.ensure_item(**request)
    store.update_item(first['item']['id'], title='Acme reviewed report')
    store.transition(first['item']['id'], 'done')
    retry = store.ensure_item(**request)
    assert retry['decision'] == 'replayed'
    assert retry['item']['id'] == first['item']['id']
    assert retry['item']['state'] == 'done'
    assert retry['item']['title'] == 'Acme reviewed report'


def test_reviewed_follow_up_appends_once_without_replacing_original(pool):
    first = store.ensure_item(**creation_case())
    proposed = creation_case('synthetic-b', 'request-b')
    proposed['title'] = 'Acme report appendix'
    check = store.creation_preflight(**proposed)
    target = next(r for r in check['matches'] if r['item']['id'] == first['item']['id'])
    result = store.ensure_item(**proposed, reuse_id=first['item']['id'],
                              expected_revision=target['revision'], note='Include synthetic appendix.')
    replay = store.ensure_item(**proposed, reuse_id=first['item']['id'],
                              expected_revision=target['revision'], note='Include synthetic appendix.')
    assert result['decision'] == 'merged' and replay['decision'] == 'replayed'
    assert replay['item']['description'] == creation_case()['description'] + '\n\nInclude synthetic appendix.'


def test_review_cannot_overwrite_a_changed_target(pool):
    first = store.ensure_item(**creation_case())
    proposed = creation_case('synthetic-b', 'request-b')
    target = store.creation_preflight(**proposed)['matches'][0]
    store.update_item(first['item']['id'], title='Acme changed report')
    with pytest.raises(store.SkillError, match='changed'):
        store.ensure_item(**proposed, reuse_id=first['item']['id'], expected_revision=target['revision'])


def test_new_message_cannot_recreate_a_completed_dated_obligation(pool):
    first = store.ensure_item(**creation_case())
    store.transition(first['item']['id'], 'done')
    with pytest.raises(store.SkillError, match='creation review required'):
        store.ensure_item(**creation_case('synthetic-b', 'request-b'))
    assert len(store.list_items(limit=100)['items']) == 1


def test_changed_request_key_payload_is_rejected(pool):
    request = creation_case()
    store.ensure_item(**request)
    request['title'] += ' changed'
    with pytest.raises(store.SkillError, match='request changed'):
        store.ensure_item(**request)


def test_existing_legacy_key_keeps_its_original_identity(pool):
    request = creation_case()
    original = store.add_item(**request)
    store.update_item(original['id'], title='Acme revised obligation')
    other = store.add_item(**creation_case('synthetic-b', 'request-b'))
    result = store.ensure_item(**request)
    assert result['item']['id'] == original['id']
    assert result['item']['id'] != other['id']


@pytest.mark.parametrize('target', ['different', 'same'])
def test_legacy_key_cannot_silently_consume_explicit_reuse_options(pool, target):
    import creation_guard
    proposed = creation_case()
    original = store.add_item(**proposed)
    other = store.add_item(**creation_case('synthetic-b', 'request-b'))
    selected = other if target == 'different' else original
    before = store.get_events(original['id'])
    with pytest.raises(store.SkillError, match='new request identity'):
        store.ensure_item(**proposed, reuse_id=selected['id'],
                          expected_revision=creation_guard.revision(selected), note=proposed['description'])
    assert store.get_item(original['id']) == original
    assert store.get_item(other['id']) == other
    assert store.get_events(original['id']) == before


def test_uninitialized_preflight_does_not_create_a_database(tmp_path):
    target = tmp_path / 'uninitialized.sqlite3'
    with pytest.raises(store.SkillError, match='initialize'):
        store.creation_preflight(**creation_case(), db_path=str(target))
    assert not target.exists()


def test_generic_dispatch_can_see_other_sources_before_judging(pool):
    original = store.add_item(**creation_case())
    state = dispatch.get_state(dict(dispatch.STREAMS['infra'], source='agent-center:infra'))
    assert original['id'] in {row['id'] for row in state}


def test_status_notice_cannot_replace_a_human_obligation(pool):
    notice = store.add_item(**creation_case('task-health', 'notice-a'))
    obligation = store.ensure_item(**creation_case())
    assert obligation['decision'] == 'created'
    assert obligation['item']['id'] != notice['id']


def test_cross_source_judgment_does_not_load_status_notices(pool):
    notice = store.add_item(**creation_case('task-health', 'notice-a'))
    obligation = store.add_item(**creation_case())
    state = dispatch.get_state(dict(dispatch.STREAMS['infra'], source='agent-center:infra'))
    assert obligation['id'] in {row['id'] for row in state}
    assert notice['id'] not in {row['id'] for row in state}


def test_duplicate_creates_in_different_messages_reuse_existing_item(pool):
    plan, _ = dispatch_case(pool)
    dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-a')
    result = dispatch.execute('infra', dispatch.STREAMS['infra'], plan, [], request_id='message-b')
    assert len(store.list_items(limit=100)['items']) == 1
    assert result['created'] == 0 and result['reused'] == 1


def test_identical_active_agent_requests_reuse_the_order(pool):
    _, plan = dispatch_case(pool)
    action = plan['actions'][0]
    first = agent_task.enqueue('infra', action['request'], workspace=str(pool), idempotency_key='work-a')
    second = agent_task.enqueue('support', action['request'], workspace=str(pool), idempotency_key='work-b')
    assert first['id'] == second['id']
    assert agent_task.read_request(second) == action['request']
