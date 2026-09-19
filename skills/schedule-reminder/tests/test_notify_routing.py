"""Owner policies over shared receipts; fake transport only."""
import os
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import notification_client as client
import notify as notify_mod

def capture(monkeypatch, state='sent'):
    calls = []
    def submit(*args, **options):
        calls.append((args, options))
        return {'state': state}
    monkeypatch.setattr(client, 'submit', submit)
    for key in ('SCHEDULE_RELAY_CMD', 'SCHEDULE_RELAY_PY', 'SCHEDULE_RELAY_STREAM'):
        monkeypatch.delenv(key, raising=False)
    return calls

def test_default_reminders_stream_and_explicit_fallback(monkeypatch):
    calls = capture(monkeypatch)
    assert notify_mod.notify('synthetic', run_id='occurrence') is True
    args, options = calls[0]
    assert args[:5] == ('schedule-reminder', 'occurrence', 'reminder', 'due', 'reminders')
    assert options['fallback'] == 'big_brother' and 'command' not in options

def test_override_keeps_custom_target(monkeypatch):
    calls = capture(monkeypatch)
    monkeypatch.setenv('SCHEDULE_RELAY_CMD', 'python synthetic-notifier.py')
    assert notify_mod.notify('synthetic', run_id='occurrence') is True
    assert calls[0][1]['command']['argv'] == ['python', 'synthetic-notifier.py']
    assert 'fallback' not in calls[0][1]

def test_explicit_relay_not_replaced_with_default_stream(monkeypatch, tmp_path):
    path = tmp_path / 'custom-relay.py'
    path.write_text('# synthetic')
    calls = capture(monkeypatch)
    monkeypatch.setenv('SCHEDULE_RELAY_PY', str(path))
    notify_mod.notify('synthetic', run_id='occurrence')
    assert calls[0][1]['command']['argv'][1] == str(path)

def test_stream_configurable(monkeypatch):
    calls = capture(monkeypatch)
    monkeypatch.setenv('SCHEDULE_RELAY_STREAM', 'infra')
    notify_mod.notify('synthetic', run_id='occurrence')
    assert calls[0][0][4] == 'infra'

def test_standalone_bigbrother_target(monkeypatch, tmp_path):
    calls = capture(monkeypatch)
    monkeypatch.setenv('SCHEDULE_RELAY_PY', str(tmp_path / 'missing.py'))
    notify_mod.notify('synthetic', run_id='occurrence')
    assert Path(calls[0][1]['command']['argv'][1]).name == 'bigbrother.py'

@pytest.mark.parametrize('state', ['failed', 'uncertain', 'pending', 'refused'])
def test_non_sent_is_false_and_visible(monkeypatch, capsys, state):
    capture(monkeypatch, state)
    assert notify_mod.notify('synthetic', run_id='occurrence') is False
    assert state in capsys.readouterr().err

def test_missing_identity_never_sends(monkeypatch, capsys):
    monkeypatch.delenv('TASK_RUN_ID', raising=False)
    monkeypatch.delenv('SCHEDULE_RUN_ID', raising=False)
    assert notify_mod.notify('synthetic') is False
    assert 'stable_run_id_required' in capsys.readouterr().err
