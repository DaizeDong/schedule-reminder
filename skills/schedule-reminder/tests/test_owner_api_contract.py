"""Offline CLI regressions for the restored owner contracts."""
import json
from pathlib import Path
import subprocess
import sys

import store
import pytest

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / 'skills/schedule-reminder/scripts/reminder.py'
FIX = json.loads((Path(__file__).parent / 'capability_cases.json').read_text(encoding='utf-8'))


def run(database, *args, payload=None):
    return subprocess.run([sys.executable, '-B', str(CLI), '--db', str(database), *args],
                          input=json.dumps(payload) if payload is not None else None,
                          capture_output=True, text=True, encoding='utf-8')


def test_work_feed_cli_serves_existing_readonly_snapshot(tmp_path):
    database = tmp_path / 'work.sqlite3'
    store.init_db(database)
    item = store.add_item(FIX['schedule3']['title'], db_path=database)
    result = run(database, 'work-feed')
    assert result.returncode == 0, result.stderr
    feed = json.loads(result.stdout)
    assert feed['available'] and [row['id'] for row in feed['items']] == [item['id']]


def test_work_action_cli_rejects_stale_revision_without_completion(tmp_path):
    database = tmp_path / 'work.sqlite3'
    store.init_db(database)
    item = store.add_item(FIX['schedule3']['title'], db_path=database)
    request = {'item_id': item['id'], 'action_id': 'complete', 'revision': 'stale',
               'request_id': 'synthetic-request-01'}
    result = run(database, 'work-action', payload=request)
    assert result.returncode == 1, result.stderr
    assert json.loads(result.stderr)['error_code'] == 'stale_recommendation'
    assert store.get_item(item['id'], db_path=database)['state'] == 'pending'


def test_creation_preflight_and_ensure_reuse_cross_source_obligation(tmp_path):
    database = tmp_path / 'work.sqlite3'
    store.init_db(database)
    item = store.add_item(FIX['schedule3']['title'], db_path=database)
    proposed = {'source': 'synthetic-source', 'idempotency_key': 'synthetic-request-01'}
    preflight = store.creation_preflight(item['title'], db_path=database, **proposed)
    assert preflight['decision'] == 'reuse'
    result = store.ensure_item(item['title'], db_path=database, **proposed)
    assert result['item']['id'] == item['id'] and result['decision'] == 'reused'
    assert store.ensure_item(item['title'], db_path=database, **proposed)['decision'] == 'replayed'


def test_owner_read_contracts_refuse_lost_private_admission(tmp_path, monkeypatch):
    import private_data
    import reminder_actions
    import reminder_link_review
    import reminder_linked_items
    import reminder_work_feed

    database = tmp_path / 'work.sqlite3'
    store.init_db(database)
    item = store.add_item(FIX['schedule3']['title'], db_path=database)
    def unproven(*args, **kwargs):
        raise ValueError('synthetic visibility unavailable')
    monkeypatch.setattr(private_data, 'prove_private', unproven)
    assert not reminder_work_feed.read_work_feed(db_path=database)['available']
    assert not reminder_actions.inspect_item(database, item['id'])['available']
    assert reminder_linked_items.read_linked_items([], db_path=database)['status'] == 'error'
    with pytest.raises(store.SkillError, match='synthetic visibility unavailable'):
        reminder_link_review.build_review(db_path=database,
            decisions={'schemaVersion': 1, 'links': {}, 'unmapped_items': 'reviewed-unlinked'})


def test_followup_receipt_rejects_changed_note_under_same_identity(tmp_path):
    database = tmp_path / 'work.sqlite3'
    store.init_db(database)
    item = store.add_item(FIX['schedule3']['title'], db_path=database)
    note = FIX['schedule3']['reason']
    store.append_dispatch_note(item['id'], note, 'synthetic-request-01', db_path=database)
    store.append_dispatch_note(item['id'], note, 'synthetic-request-01', db_path=database)
    assert store.get_item(item['id'], db_path=database)['description'].count(note) == 1
    with pytest.raises(store.SkillError):
        store.append_dispatch_note(item['id'], note + ' changed', 'synthetic-request-01', db_path=database)
