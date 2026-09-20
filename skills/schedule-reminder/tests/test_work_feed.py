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
