"""Installer plans, task readback and end-to-end ingest use generated records and offline adapters."""
from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest
import agent_run
import agent_task
import dispatch
import ingest
import ingest_tick
import installer
import private_data
import store

FIX = json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


def configured(tmp_path, monkeypatch, selection='store,remind'):
    database = tmp_path/'db.sqlite3'
    config = tmp_path/'registry.json'
    config.write_text(json.dumps(FIX['delivery_registry']), encoding='utf-8')
    store.init_db(str(database))
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(database))
    monkeypatch.setenv('SCHEDULE_CAPABILITIES', selection)
    return database, config, installer.plan(selection, str(database), config=str(config))


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


def test_plan_default_empty_unsupported_without_effects(tmp_path, monkeypatch):
    monkeypatch.delenv('SCHEDULE_CAPABILITIES', raising=False)
    before = snapshot(tmp_path)
    planned = installer.plan()
    assert [k for k, r in planned['capabilities'].items() if r['selected']] == ['store', 'remind']
    assert set(planned['capabilities']) == set(installer.GROUPS)
    assert installer.install('')[0] == 0
    with pytest.raises(ValueError, match='unsupported'):
        installer.install(FIX['invalid_capability'])
    assert snapshot(tmp_path) == before


@pytest.mark.skipif(os.name != 'nt', reason='public PowerShell installer wrapper')
@pytest.mark.parametrize('selection', [None, '', 'work', FIX['invalid_capability']])
def test_public_powershell_plan_selection_without_effects(tmp_path, monkeypatch, selection):
    monkeypatch.setenv('SCHEDULE_PYTHON', sys.executable)
    before = snapshot(tmp_path)
    argv = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(Path(installer.__file__).with_name('install.ps1')), '-Plan']
    if selection is not None:
        argv += ['-Capabilities', selection]
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=25)
    report = json.loads(result.stdout)
    assert snapshot(tmp_path) == before
    if selection == FIX['invalid_capability']:
        assert result.returncode != 0 and report['ok'] is False
    else:
        assert result.returncode == 0, result.stderr
        expected = {'store', 'remind'} if selection is None else ({selection} if selection else set())
        assert {name for name, row in report['capabilities'].items() if row['selected']} == expected


@pytest.mark.parametrize('group,seconds,entry', [('remind', 300, 'reminder.py'),
                                                 ('ingest', 300, 'ingest_tick.py'),
                                                 ('work', 120, 'agent_tick.py')])
def test_plan_registers_the_real_entrypoint(group, seconds, entry):
    plan = installer.plan(group)
    row = plan['capabilities'][group]
    assert row['selected'] and row['runtime'] and Path(row['entrypoint']).name == entry
    assert Path(row['entrypoint']).is_file()
    arguments = installer._arguments(row['task']['arguments'])
    assert arguments[0] == '-B' and Path(arguments[1]).name == entry
    if group == 'remind':
        assert arguments[2:] == ['--db', plan['dependencies']['store']['db_path'], 'tick']
    assert row['task']['interval_seconds'] == seconds
    assert plan['dependencies']['store']['required'] and plan['dependencies']['relay']['required']
    assert plan['dependencies']['llmcall']['required'] == (group != 'remind')
    assert installer.verify_task(installer.task_xml(row, FIX['user']), row)[0]


@pytest.mark.parametrize('element,value', [
    ('./Actions/Exec/Command', 'different'), ('./Actions/Exec/Arguments', '--different'),
    ('./Actions/Exec/WorkingDirectory', 'different'), ('./Settings/Enabled', 'false'),
    ('./Triggers/TimeTrigger/Enabled', 'false'), ('./Triggers/TimeTrigger/Repetition/Interval', 'PT1M'),
    ('./Triggers/TimeTrigger/Repetition/Duration', 'P1D'), ('./Triggers/TimeTrigger/EndBoundary', FIX['now']),
])
def test_task_readback_rejects_action_and_schedule_drift(element, value):
    row = installer.plan('remind')['capabilities']['remind']
    root = ET.fromstring(installer.task_xml(row, FIX['user']))
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    parent, name = element.rsplit('/', 1)
    node = root.find(element)
    if node is None:
        node = ET.SubElement(root.find(parent), name)
    node.text = value
    assert not installer.verify_task(ET.tostring(root, encoding='unicode'), row)[0]


def test_registration_success_requires_separate_readback(tmp_path, monkeypatch):
    database, config, plan = configured(tmp_path, monkeypatch)
    row = plan['capabilities']['remind']
    calls = []
    monkeypatch.setattr(installer, 'read_task_xml', lambda name: '<Task/>')
    original = subprocess.run
    def run(argv, **kwargs):
        if argv[0] == 'schtasks':
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        return original(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='readback'):
        installer.register(row, tmp_path)
    assert len(calls) == 1
    monkeypatch.setattr(installer, 'read_task_xml', lambda name: installer.task_xml(row, FIX['user']))
    assert installer.register(row, tmp_path)['registered'] is False
    assert len(calls) == 1


def test_install_succeeds_on_a_healthy_store_and_present_registry(tmp_path, monkeypatch):
    """Install used to exit nonzero forever, waiting for readiness evidence nothing produced."""
    database, config, _ = configured(tmp_path, monkeypatch)
    code, result = installer.install('store,remind', str(database), config=str(config), no_task=True)
    assert code == 0 and result['health']['integrity_ok'] and result['relay_registry'] is True
    config.unlink()
    assert installer.install('store,remind', str(database), config=str(config), no_task=True)[0] == 1
    assert installer.install('store', str(database))[0] == 0


def test_linked_private_repo_and_explicit_db_precedence(tmp_path, monkeypatch):
    linked = tmp_path/'linked'
    linked.mkdir()
    meta = tmp_path/'.git/worktrees/linked'
    meta.mkdir(parents=True)
    (meta/'commondir').write_text('../..')
    (linked/'.git').write_text('gitdir: '+str(meta))
    path = linked/'db.sqlite3'
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(tmp_path/'environment.sqlite3'))
    store.init_db(str(path))
    assert path.exists() and not (tmp_path/'environment.sqlite3').exists()


def test_missing_read_views_and_permission_failure(tmp_path, monkeypatch):
    path = str(tmp_path/'missing'/'db.sqlite3')
    assert store.list_items(db_path=path) == {'items': [], 'next_cursor': None}
    assert store.due_items(db_path=path) == []
    assert not Path(path).parent.exists()
    def denied(*args): raise PermissionError('synthetic permission denial')
    monkeypatch.setattr(private_data, 'check_destination', denied)
    with pytest.raises(store.SkillError) as error:
        store.init_db(path)
    assert error.value.error_code == 'ERR_PERMISSION'


def test_ingest_preserves_ids_through_real_dispatch_and_enqueue(tmp_path, monkeypatch):
    store.init_db()
    channel = FIX['channels'][0]['id']
    owner = FIX['schedule3']['owner_id']
    message = deepcopy(FIX['message'])
    message['author']['id'] = owner
    monkeypatch.setattr(ingest, '_STATE_DIR', str(tmp_path/'state'))
    ingest.prepare_state()
    Path(ingest._last_file(channel)).write_text('0', encoding='utf-8')
    monkeypatch.setattr(ingest, '_fetch', lambda *a, **kw: [message])
    monkeypatch.setattr(ingest, 'poll', lambda **kw: {'text': {
        FIX['stream']: len(ingest.poll_stream(FIX['stream'], channel, 'synthetic', owner))}})
    monkeypatch.setattr(ingest, 'load_registry', lambda: FIX['registry'])
    monkeypatch.setattr(dispatch, 'call_chain', lambda *a, **kw: json.dumps(FIX['dispatch_plan']))
    monkeypatch.setattr(dispatch, 'get_state', lambda cfg: [])
    monkeypatch.setattr(dispatch, 'get_work', lambda: [])
    assert ingest_tick.run(post=False)['handled'][FIX['stream']] == 'ok'
    assert FIX['stream'] not in ingest_tick.run(post=False)['handled']
    orders = agent_task.orders()
    assert len(orders) == 1 and orders[0]['ext'][agent_task.EXT_MSG] == FIX['message_id']
    assert len(list((tmp_path/'runs').glob('*/events.jsonl'))) == 1


def test_missing_llmcall_and_runner_environment_are_preserved(monkeypatch):
    monkeypatch.setenv('LLMCALL_AGENT_RUNNER', FIX['user'])
    importlib.reload(agent_run)
    assert os.environ['LLMCALL_AGENT_RUNNER'] == FIX['user']
    monkeypatch.setitem(sys.modules, 'llmcall', None)
    with pytest.raises(ModuleNotFoundError):
        agent_run._llm(FIX['request'], None, 'agent')
    with pytest.raises(ModuleNotFoundError):
        dispatch.call_chain(FIX['request'])


def test_dispatch_uses_result_text_and_installed_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(sys.modules['llmcall'], 'call', lambda prompt, **kwargs:
                        calls.append(kwargs) or SimpleNamespace(**FIX['model_result'], error=None))
    assert dispatch.call_chain(FIX['request']) == FIX['model_result']['text']
    assert calls == [{'mode': 'judge', 'log': None, 'timeout': None}]
