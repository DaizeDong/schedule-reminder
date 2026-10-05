import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[3]
SCRIPTS=ROOT/'skills/schedule-reminder/scripts'
sys.path.insert(0,str(SCRIPTS))
from reminder_work_feed import read_work_feed
spec=importlib.util.spec_from_file_location('work_fixtures',ROOT/'tools/make_fixtures.py')
fixtures=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixtures)


def test_cancelled_cleanup_reservation_is_visible_to_waiting_work(tmp_path):
    path=tmp_path/'queue.sqlite3'
    old,waiting=fixtures.blocked_queue_database(path)
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=read_work_feed(db_path=path)
    assert result['available']
    assert result['queue']['state']=='blocked'
    rows={row['id']:row for row in result['items']}
    assert rows[old['id']]['execution']['state']=='stopped'
    assert rows[waiting['id']]['execution']['queue_reason']=='cleanup_unconfirmed'
    assert rows[waiting['id']]['execution']['blocked_by']==[old['id']]
    assert 'cleanup_receipt' not in json.dumps(result)
    assert before==hashlib.sha256(path.read_bytes()).hexdigest()


def test_actionable_email_and_reviewed_links_are_projected_without_private_metadata(tmp_path):
    path=tmp_path/'email.sqlite3'
    parent,child=fixtures.email_work_database(path)
    result=read_work_feed(db_path=path)
    rows={r['id']:r for r in result['items']}
    assert rows[parent['id']]['role']=='tracked_item'
    assert any(o['id']=='complete' for o in rows[parent['id']]['actions']['offers'])
    assert rows[child['id']]['role']=='signal'
    assert rows[child['id']]['group_parent_id']==parent['id']
    assert 'never forward' not in json.dumps(result)
    assert result['coverage']['roles']=={'tracked_item':1,'signal':1}


def test_event_projection_uses_the_same_role_as_actionable_email_items(tmp_path):
    path = tmp_path / 'email.sqlite3'
    parent, child = fixtures.email_work_database(path)
    result = read_work_feed(db_path=path)
    assert {row['item_id'] for row in result['events']} == {parent['id']}
    assert result['coverage']['event_total'] == len(result['events'])


def test_owner_feed_is_read_only_and_separates_signals(tmp_path):
    path=fixtures.work_database(tmp_path/'work.sqlite3')
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    result=read_work_feed(db_path=path)
    assert result['available']
    assert result['coverage']['roles']=={'agent_work':3,'tracked_item':1,'signal':1}
    assert result['capabilities']['decisions']['available'] is False
    assert result['capabilities']['validation']['available'] is False
    assert len(result['events'])==2
    assert all(row['item_id']!='signal' for row in result['events'])
    assert 'secret' not in json.dumps(result)
    assert all('ext' not in item for item in result['items'])
    assert before==hashlib.sha256(path.read_bytes()).hexdigest()
    assert not path.with_suffix('.sqlite3-journal').exists()


def test_missing_database_cli_does_not_create_or_initialize(tmp_path):
    path=tmp_path/'missing'/'work.sqlite3'
    result=subprocess.run([sys.executable,str(SCRIPTS/'reminder.py'),'--db',str(path),'work-feed'],
                          capture_output=True,text=True,encoding='utf-8',timeout=15)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['available'] is False
    assert not path.parent.exists()


def test_limit_prioritizes_work_and_reports_omissions(tmp_path):
    result=read_work_feed(db_path=fixtures.work_database(tmp_path/'work.sqlite3'),limit=2,event_limit=1)
    assert all(row['role']=='agent_work' for row in result['items'])
    assert result['coverage']['omitted']==3
    assert result['coverage']['event_total']==2
    assert len(result['events'])==1


def test_malformed_record_is_visible_and_old_schema_is_not_migrated(tmp_path):
    path=fixtures.work_database(tmp_path/'work.sqlite3')
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE items SET ext='[]' WHERE id='tracked'")
        conn.execute('DROP TABLE events')
    result=read_work_feed(db_path=path)
    assert result['available'] and result['coverage']['invalid']==1
    assert result['coverage']['events_available'] is False
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='events'").fetchone() is None


def test_invalid_bindings_and_unsupported_schema(tmp_path):
    assert not read_work_feed(db_path='relative.sqlite3')['available']
    path=tmp_path/'empty.sqlite3'
    with sqlite3.connect(path):pass
    assert read_work_feed(db_path=path)['reason']=='unsupported_work_schema'
    assert read_work_feed(db_path=path,limit=True)['reason']=='invalid_limit'


def test_legacy_running_work_is_visible_as_a_writer_reservation(tmp_path):
    import store
    from tools.make_fixtures import creation_case
    path = tmp_path / 'legacy-running.sqlite3'
    store.init_db(str(path))
    legacy = store.add_item(**dict(creation_case(), source='agent-center:work', state='doing',
                                   ext={'x_agent_exec_state': 'running'}), db_path=str(path))
    waiting = store.add_item(**dict(creation_case('synthetic-b', 'request-b'), source='agent-center:work',
                                    ext={'x_agent_exec_state': 'queued'}), db_path=str(path))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = read_work_feed(db_path=path)
    assert result['queue']['state'] == 'busy'
    projected = next(row for row in result['items'] if row['id'] == waiting['id'])
    assert projected['execution']['queue_reason'] == 'writer_busy'
    assert projected['execution']['blocked_by'] == [legacy['id']]
    assert before == hashlib.sha256(path.read_bytes()).hexdigest()
