"""CLI failures retain actionable evidence and never publish successful delivery."""
import builtins
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
import dispatch
import notify
import scheduler_worker
import store

FIX = json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


def dispatch_cli(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['dispatch.py', '--stream', FIX['stream'],
                                    '--reply', FIX['request'], '--no-post'])
    monkeypatch.setattr(dispatch, 'get_work', lambda: [])


def test_missing_llmcall_cli_is_structured_and_actionable(monkeypatch, capsys):
    dispatch_cli(monkeypatch)
    monkeypatch.setitem(sys.modules, 'llmcall', None)
    def forbidden(*args, **kwargs):
        pytest.fail('missing model dependency must not execute or post')
    monkeypatch.setattr(dispatch, 'execute', forbidden)
    monkeypatch.setattr(dispatch, '_post', forbidden)
    assert dispatch.main() != 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report['ok'] is False and report['status'] == 'unavailable'
    assert report['error_code'] and 'llmcall' in report['message']
    assert 'install' in report['action'].lower() and 'retry' in report['action'].lower()
    assert 'Traceback' not in captured.err


def test_available_llmcall_preserves_cli_success_and_default_policy(monkeypatch, capsys):
    dispatch_cli(monkeypatch)
    calls = []
    def call(prompt, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(FIX['dispatch_noop_plan']), error=None)
    monkeypatch.setitem(sys.modules, 'llmcall', SimpleNamespace(call=call))
    assert dispatch.main() == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True}
    assert len(calls) == 1 and calls[0]['mode'] == 'judge'
    assert set(calls[0]) == {'mode', 'log'}


def test_unrelated_missing_module_is_not_reported_as_llmcall_missing(monkeypatch):
    dispatch_cli(monkeypatch)
    real_import = builtins.__import__
    def broken_import(name, *args, **kwargs):
        if name == 'llmcall':
            raise ModuleNotFoundError('synthetic transitive dependency missing', name='synthetic_dependency')
        return real_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', broken_import)
    with pytest.raises(ModuleNotFoundError, match='transitive'):
        dispatch.main()


@pytest.mark.parametrize('available', [False, True])
def test_dispatch_script_exits_for_missing_and_available_model(tmp_path, available):
    code = (
        'import json,runpy,sys; from pathlib import Path; from types import SimpleNamespace; '
        'script,available,plan,reply=sys.argv[1:]; sys.path.insert(0,str(Path(script).parent)); '
        'sys.modules["llmcall"]=(SimpleNamespace(call=lambda *a,**kw: '
        'SimpleNamespace(text=plan,error=None)) if available=="yes" else None); '
        'sys.argv=[script,"--stream","synthetic-stream","--reply",reply,"--no-post"]; '
        'runpy.run_path(script,run_name="__main__")'
    )
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, dispatch.__file__,
                             'yes' if available else 'no', json.dumps(FIX['dispatch_noop_plan']), FIX['request']],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    report = json.loads(result.stdout)
    assert result.returncode == (0 if available else 1)
    assert report['ok'] is available
    if not available:
        assert report['status'] == 'unavailable' and report['action']
        assert 'Traceback' not in result.stderr


def configured(tmp_path, monkeypatch):
    database = str(tmp_path/'db.sqlite3')
    config = str(tmp_path/'registry.json')
    Path(config).write_text(json.dumps(FIX['registry']), encoding='utf-8')
    store.init_db(database)
    monkeypatch.setenv('SCHEDULE_NOW', FIX['now'])
    item = store.add_item(title=FIX['title'], due_at=FIX['now'], db_path=database)
    return database, config, item


@pytest.mark.parametrize('failure', ['false', 'receipt', 'exception'])
def test_failed_reminder_cli_explains_retry_without_success(tmp_path, monkeypatch, capsys, failure):
    database, config, item = configured(tmp_path, monkeypatch)
    def deliver(text):
        if failure == 'exception':
            raise OSError('synthetic delivery failure')
        return FIX['failed_receipt'] if failure == 'receipt' else False
    monkeypatch.setattr(notify, 'deliver', deliver)
    assert scheduler_worker.main(['--capability', 'remind', '--db', database, '--config', config]) != 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'partial' and report['retried'] == [item['id']]
    assert report['error_code'] and 'delivery' in report['message'].lower()
    assert 'retry' in report['action'].lower()
    assert not report['delivery_receipts'] and not (tmp_path/'readiness.json').exists()
    stored = store.get_item(item['id'], db_path=database)
    assert stored['notified_at'] is None and stored['retry_count'] == 1 and stored['next_retry_at']
    assert not any(event['event_type'] == 'notified' for event in store.get_events(item['id'], db_path=database))


def test_successful_reminder_and_repeated_tick_preserve_delivery(tmp_path, monkeypatch, capsys):
    database, config, item = configured(tmp_path, monkeypatch)
    deliveries = []
    monkeypatch.setattr(notify, 'deliver', lambda text: deliveries.append(text) or FIX['receipt'])
    argv = ['--capability', 'remind', '--db', database, '--config', config]
    assert scheduler_worker.main(argv) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'completed' and report['dispatched'] == [item['id']]
    assert 'error_code' not in report and (tmp_path/'readiness.json').exists()
    assert scheduler_worker.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['dispatched'] == []
    assert len(deliveries) == 1


def test_partial_delivery_keeps_success_and_does_not_publish_ready(tmp_path, monkeypatch, capsys):
    database, config, item = configured(tmp_path, monkeypatch)
    second = store.add_item(title=FIX['request'], due_at=FIX['now'], db_path=database)
    results = iter([FIX['receipt'], FIX['failed_receipt']])
    monkeypatch.setattr(notify, 'deliver', lambda text: next(results))
    assert scheduler_worker.main(['--capability', 'remind', '--db', database, '--config', config]) != 0
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'partial' and report['dispatched'] == [item['id']]
    assert report['retried'] == [second['id']] and report['delivery_receipts'] == [FIX['receipt']]
    assert report['error_code'] and report['action'] and not (tmp_path/'readiness.json').exists()


def test_delivery_retry_exhaustion_remains_explicitly_blocked(tmp_path, monkeypatch, capsys):
    database, config, item = configured(tmp_path, monkeypatch)
    monkeypatch.setattr(notify, 'deliver', lambda text: False)
    for attempt in range(store._NOTIFY_MAX_RETRIES):
        assert scheduler_worker.main(['--capability', 'remind', '--db', database, '--config', config]) != 0
        report = json.loads(capsys.readouterr().out)
        assert report['status'] == 'partial' and report['error_code'] and report['action']
        current = store.get_item(item['id'], db_path=database)
        if attempt < store._NOTIFY_MAX_RETRIES - 1:
            monkeypatch.setenv('SCHEDULE_NOW', current['next_retry_at'])
    assert report['blocked'] == [item['id']] and not report['retried']
    assert current['state'] == 'blocked' and current['notified_at'] is None
    assert not (tmp_path/'readiness.json').exists()


@pytest.mark.parametrize('delivered', [False, True])
def test_reminder_script_exit_and_persisted_delivery_agree(tmp_path, monkeypatch, delivered):
    database, config, item = configured(tmp_path, monkeypatch)
    code = (
        'import json,runpy,sys; from pathlib import Path; '
        'script,response,database,config=sys.argv[1:]; sys.path.insert(0,str(Path(script).parent)); '
        'import notify; notify.deliver=lambda text: json.loads(response); '
        'sys.argv=[script,"--capability","remind","--db",database,"--config",config]; '
        'runpy.run_path(script,run_name="__main__")'
    )
    receipt = FIX['receipt'] if delivered else FIX['failed_receipt']
    result = subprocess.run([sys.executable, '-X', 'utf8', '-B', '-c', code, scheduler_worker.__file__,
                             json.dumps(receipt), database, config],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    report = json.loads(result.stdout)
    assert result.returncode == (0 if delivered else 1)
    assert report['status'] == ('completed' if delivered else 'partial')
    assert bool(store.get_item(item['id'], db_path=database)['notified_at']) is delivered
    assert (tmp_path/'readiness.json').exists() is delivered
    if not delivered:
        assert report['error_code'] and report['action'] and 'Traceback' not in result.stderr
