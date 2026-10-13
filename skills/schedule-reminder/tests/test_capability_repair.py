"""Behavior regressions using generated inputs and intercepted external services."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import store
import subprocess
import reminder
import agent_run
import agent_task
import ingest

FIX = json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


def test_uninitialized_health_does_not_create_database(tmp_path, capsys):
    path = tmp_path/'uninitialized'/'db.sqlite3'
    assert reminder.main(['--db', str(path), 'health']) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['ok'] is True and report['health']['db_ok'] is False
    assert not path.parent.exists()


def test_health_is_quick_check_plus_registry_presence(tmp_path, monkeypatch):
    """health reads the database and checks the relay registry file; it starts no process."""
    def forbidden(*args, **kwargs):
        raise AssertionError('health must not start a process (no schtasks, no import probe)')
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.delenv('SCHEDULE_RELAY_CMD', raising=False)
    database = tmp_path/'db.sqlite3'
    store.init_db(str(database))
    registry = tmp_path/'registry.json'
    monkeypatch.setenv('AGENT_CENTER_CONFIG', str(registry))
    report = store.health(db_path=str(database))
    assert report['db_ok'] and report['integrity_ok'] and report['wal_ok']
    assert report['schema_user_version'] == store.SCHEMA_USER_VERSION
    assert report['relay_ok'] is False and any('registry' in w for w in report['warnings'])
    registry.write_text('{}', encoding='utf-8')
    assert store.health(db_path=str(database))['relay_ok'] is True


def test_model_call_does_not_override_installed_policy(monkeypatch):
    calls = []
    result = SimpleNamespace(**FIX['model_result'], error=None)
    monkeypatch.setattr(sys.modules['llmcall'], 'call', lambda prompt, **kw: calls.append(kw) or result)
    agent_run._llm(FIX['request'], None, 'agent')
    assert calls and calls[0]['mode'] == 'agent'
    assert not ({'chain', 'model', 'providers', 'timeout', 'fallback'} & calls[0].keys())


def test_duplicate_message_creates_one_work_order_and_run(tmp_path, monkeypatch):
    store.init_db()
    first = agent_task.enqueue(FIX['stream'], FIX['request'], workspace=str(tmp_path), msg_id=FIX['message_id'])
    second = agent_task.enqueue(FIX['stream'], FIX['request'], workspace=str(tmp_path), msg_id=FIX['message_id'])
    assert first['id'] == second['id']
    runs = [path for path in (tmp_path/'runs').iterdir() if path.is_dir()]
    assert not list((tmp_path/'runs').rglob('*.lock')), 'enqueue takes no lock'
    assert len(runs) == 1
    assert len((runs[0]/'events.jsonl').read_text(encoding='utf-8').splitlines()) == 1


def test_listen_false_excludes_registry_and_discovery(monkeypatch):
    reg = FIX['notification_registry']
    monkeypatch.setattr(ingest, '_get', lambda *a: FIX['channels'])
    assert ingest._streams(reg) == {}
    assert all(str(channel) != '3101' for _, channel in ingest.discovered_channels(reg, 'synthetic-token'))
