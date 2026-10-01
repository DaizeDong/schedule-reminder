"""Behavior regressions using generated inputs and intercepted external services."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import store
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


def test_empty_selection_has_no_ready_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv('SCHEDULE_CAPABILITIES', '')
    report = store.health(db_path=str(tmp_path/'missing.sqlite3'))
    readiness = report['readiness']
    assert not readiness['ready']
    assert set(readiness['capabilities']) == {'store', 'remind', 'ingest', 'work'}
    assert all(not row['selected'] and row['status'] == 'not_selected' and row['reasons']
               for row in readiness['capabilities'].values())


def test_explicit_database_in_nested_public_repo_is_refused(tmp_path):
    nested = tmp_path/'nested'
    (nested/'.git').mkdir(parents=True)
    (nested/'.git/config').write_text('[remote "origin"]\nurl = '+FIX['public_remote'], encoding='utf-8')
    with pytest.raises(store.SkillError, match='PRIVATE'):
        store.init_db(str(nested/'blocked.sqlite3'))
    assert not (nested/'blocked.sqlite3').exists()


def test_model_call_does_not_override_installed_policy(monkeypatch):
    calls = []
    result = SimpleNamespace(**FIX['model_result'], error=None)
    monkeypatch.setattr(sys.modules['llmcall'], 'call', lambda prompt, **kw: calls.append(kw) or result)
    agent_run._llm(FIX['request'], None, None, 'agent')
    assert calls and calls[0]['mode'] == 'agent'
    assert not ({'chain', 'model', 'providers', 'timeout', 'fallback'} & calls[0].keys())


def test_duplicate_message_creates_one_work_order_and_run(tmp_path, monkeypatch):
    store.init_db()
    def rem(*args):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert reminder.main(list(args)) == 0
        return json.loads(out.getvalue())
    monkeypatch.setattr(agent_task, 'rem', rem)
    first = agent_task.enqueue(FIX['stream'], FIX['request'], workspace=str(tmp_path), msg_id=FIX['message_id'])
    second = agent_task.enqueue(FIX['stream'], FIX['request'], workspace=str(tmp_path), msg_id=FIX['message_id'])
    assert first['id'] == second['id']
    runs = list((tmp_path/'runs').iterdir())
    assert len(runs) == 1
    assert len((runs[0]/'events.jsonl').read_text(encoding='utf-8').splitlines()) == 1


def test_listen_false_excludes_registry_and_discovery(monkeypatch):
    reg = FIX['notification_registry']
    monkeypatch.setattr(ingest, '_get', lambda *a: FIX['channels'])
    assert ingest._streams(reg) == {}
    assert all(str(channel) != '3101' for _, channel in ingest.discovered_channels(reg, 'synthetic-token'))
