"""Capability acceptance uses generated records, real workers, and offline platform adapters."""
from copy import deepcopy
from datetime import datetime, timedelta
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
import capabilities as cap
import dispatch
import ingest
import ingest_tick
import installer
import private_data
import relay
import scheduler_worker
import store

FIX = json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


def configured(tmp_path, monkeypatch, selection='store,remind'):
    database = tmp_path/'db.sqlite3'
    config = tmp_path/'registry.json'
    config.write_text(json.dumps(FIX['delivery_registry']), encoding='utf-8')
    store.init_db(str(database))
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(database))
    monkeypatch.setenv('SCHEDULE_CAPABILITIES', selection)
    plan = cap.plan(selection, str(database), config=str(config))
    rows = {r['task']['name']: r for name, r in plan['capabilities'].items() if name in cap.TASKS and r['selected']}
    monkeypatch.setattr(cap, 'read_task_xml', lambda name: installer.task_xml(rows[name], FIX['user']))
    return database, config, plan


def snapshot(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in root.rglob('*') if path.is_file()}


def test_plan_default_empty_unsupported_without_effects(tmp_path, monkeypatch):
    monkeypatch.delenv('SCHEDULE_CAPABILITIES', raising=False)
    before = snapshot(tmp_path)
    planned = cap.plan()
    assert [k for k, r in planned['capabilities'].items() if r['selected']] == ['store', 'remind']
    assert set(planned['capabilities']) == set(cap.GROUPS)
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


@pytest.mark.parametrize('group,seconds', [('remind', 300), ('ingest', 600), ('work', 120)])
def test_plan_dependencies_and_correct_task(group, seconds):
    plan = cap.plan(group)
    row = plan['capabilities'][group]
    assert row['selected'] and row['runtime'] and row['adapter']
    assert row['task']['interval_seconds'] == seconds
    assert plan['dependencies']['store']['required'] and plan['dependencies']['relay']['required']
    assert plan['dependencies']['llmcall']['required'] == (group != 'remind')
    assert cap.verify_task(installer.task_xml(row, FIX['user']), row)[0]


@pytest.mark.parametrize('element,value', [
    ('./Actions/Exec/Command', 'different'), ('./Actions/Exec/Arguments', '--different'),
    ('./Actions/Exec/WorkingDirectory', 'different'), ('./Settings/Enabled', 'false'),
    ('./Triggers/TimeTrigger/Enabled', 'false'), ('./Triggers/TimeTrigger/Repetition/Interval', 'PT1M'),
    ('./Triggers/TimeTrigger/Repetition/Duration', 'P1D'), ('./Triggers/TimeTrigger/EndBoundary', FIX['now']),
])
def test_task_readback_rejects_action_and_schedule_drift(element, value):
    row = cap.plan('remind')['capabilities']['remind']
    root = ET.fromstring(installer.task_xml(row, FIX['user']))
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    parent, name = element.rsplit('/', 1)
    node = root.find(element)
    if node is None:
        node = ET.SubElement(root.find(parent), name)
    node.text = value
    assert not cap.verify_task(ET.tostring(root, encoding='unicode'), row)[0]


def test_registration_success_requires_separate_readback(tmp_path, monkeypatch):
    database, config, plan = configured(tmp_path, monkeypatch)
    row = plan['capabilities']['remind']
    calls = []
    monkeypatch.setattr(cap, 'read_task_xml', lambda name: '<Task/>')
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
    monkeypatch.setattr(cap, 'read_task_xml', lambda name: installer.task_xml(row, FIX['user']))
    assert installer.register(row, tmp_path)['registered'] is False
    assert len(calls) == 1


def test_selected_import_probe_uses_actual_runtime_and_adapter(monkeypatch):
    for group in cap.TASKS:
        row = cap.plan(group)['capabilities'][group]
        assert cap.probe_runtime(row)
        row['runtime'] = str(Path(row['runtime']).with_name('missing-python'))
        with pytest.raises(OSError):
            cap.probe_runtime(row)


def test_installer_does_not_call_report_success_readiness(tmp_path, monkeypatch):
    database, config, _ = configured(tmp_path, monkeypatch)
    code, result = installer.install('store,remind', str(database), config=str(config))
    assert code != 0
    assert result['health']['readiness']['capabilities']['remind']['status'] == 'unmeasured'
    assert installer.install('store', str(database))[0] == 0


def wire_delivery(monkeypatch, response=None, fail=False):
    delivered = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self):
            return json.dumps(response if response is not None else {'id': FIX['message_id']}).encode()
    def send(request, **kwargs):
        delivered.append(request.full_url)
        if fail:
            raise OSError('synthetic transport failure')
        return Response()
    monkeypatch.setattr(relay.urllib.request, 'urlopen', send)
    return delivered


def test_actual_reminder_worker_publishes_normal_evidence_health_reads_only(tmp_path, monkeypatch):
    database, config, plan = configured(tmp_path, monkeypatch)
    delivered = wire_delivery(monkeypatch)
    store.add_item(title=FIX['title'], due_at=FIX['now'], db_path=str(database))
    monkeypatch.setenv('SCHEDULE_NOW', FIX['now'])
    assert scheduler_worker.run('remind', str(database), str(config))['status'] == 'completed'
    assert len(delivered) == 1 and 'wait=true' in delivered[0]
    record = json.loads((tmp_path/'readiness.json').read_text())['tasks']['remind']
    assert cap.valid_evidence(record, cap.task_identity(plan['capabilities']['remind'], config))[0]
    before = snapshot(tmp_path)
    measured = store.health(db_path=str(database))
    assert measured['readiness']['ready']
    assert snapshot(tmp_path) == before and len(delivered) == 1


@pytest.mark.parametrize('failure', ['exception', 'no_receipt'])
def test_failed_worker_never_publishes_delivered_success(tmp_path, monkeypatch, failure):
    database, config, _ = configured(tmp_path, monkeypatch)
    wire_delivery(monkeypatch, response={}, fail=failure == 'exception')
    store.add_item(title=FIX['title'], due_at=FIX['now'], db_path=str(database))
    monkeypatch.setenv('SCHEDULE_NOW', FIX['now'])
    result = scheduler_worker.run('remind', str(database), str(config))
    assert result['status'] == 'partial' and result['retried']
    assert not (tmp_path/'readiness.json').exists()


@pytest.mark.parametrize('age,valid', [(900, True), (900.001, False), (-60, True), (-60.001, False)])
def test_evidence_time_boundaries(tmp_path, age, valid):
    config = tmp_path/'registry.json'
    config.write_text(json.dumps(FIX['registry']))
    row = cap.plan('remind', config=str(config))['capabilities']['remind']
    identity = cap.task_identity(row, config)
    now = datetime.fromisoformat(FIX['now'])
    stamp = (now-timedelta(seconds=age)).isoformat()
    record = {**identity, 'last_success': stamp,
              'dispatch': {**identity, **FIX['external_receipt'], 'completed_at': stamp}}
    assert cap.valid_evidence(record, identity, now)[0] is valid


@pytest.mark.parametrize('field', cap.IDENTITY)
def test_evidence_binds_each_worker_and_dispatch_identity(tmp_path, field):
    config = tmp_path/'registry.json'
    config.write_text(json.dumps(FIX['registry']))
    identity = cap.task_identity(cap.plan('remind', config=str(config))['capabilities']['remind'], config)
    record = {**identity, 'last_success': FIX['now'],
              'dispatch': {**identity, **FIX['external_receipt'], 'completed_at': FIX['now']}}
    now = datetime.fromisoformat(FIX['now'])
    for container in (record, record['dispatch']):
        before = container[field]
        container[field] = None
        assert not cap.valid_evidence(record, identity, now)[0]
        container[field] = before


def test_config_change_and_remind_receipt_do_not_promote_other_workers(tmp_path, monkeypatch):
    database, config, _ = configured(tmp_path, monkeypatch, 'store,remind,ingest,work')
    cap.publish_delivery('remind', FIX['external_receipt'], database, config)
    rows = store.health(db_path=str(database))['readiness']['capabilities']
    assert rows['remind']['status'] == 'ready'
    assert all(rows[name]['selected'] and rows[name]['status'] == 'unmeasured' for name in ('ingest', 'work'))
    config.write_text(json.dumps(FIX['changed_config']))
    assert store.health(db_path=str(database))['readiness']['capabilities']['remind']['status'] == 'partial'


def test_synthetic_delivery_cannot_establish_external_readiness(tmp_path, monkeypatch):
    database, config, _ = configured(tmp_path, monkeypatch)
    cap.publish_delivery('remind', FIX['receipt'], database, config)
    report = store.health(db_path=str(database))['readiness']
    assert not report['ready'] and not report['capabilities']['remind']['external_ready']


def test_unknown_unmanaged_and_nested_paths_fail_before_writes(tmp_path):
    for name, remote in [('unknown', FIX['unknown_remote']), ('public', FIX['public_remote'])]:
        root = tmp_path/name
        (root/'.git').mkdir(parents=True)
        (root/'.git/config').write_text('[remote "origin"]\nurl = '+remote)
        target = root/'absent'/'db.sqlite3'
        with pytest.raises(store.SkillError, match='PRIVATE'):
            store.init_db(str(target))
        assert not target.parent.exists()
    with pytest.raises(ValueError, match='PRIVATE'):
        private_data.prove_private(tmp_path.parent/'unmanaged'/'data.json')


def test_linked_private_repo_and_explicit_db_precedence(tmp_path, monkeypatch):
    linked = tmp_path/'linked'
    linked.mkdir()
    meta = tmp_path/'.git/worktrees/linked'
    meta.mkdir(parents=True)
    (meta/'commondir').write_text('../..')
    (linked/'.git').write_text('gitdir: '+str(meta))
    path = linked/'db.sqlite3'
    assert private_data.prove_private(path)['visibility'] == 'PRIVATE'
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(tmp_path/'environment.sqlite3'))
    store.init_db(str(path))
    assert path.exists() and not (tmp_path/'environment.sqlite3').exists()


def test_missing_read_views_and_permission_failure(tmp_path, monkeypatch):
    path = str(tmp_path/'missing'/'db.sqlite3')
    assert store.list_items(db_path=path) == {'items': [], 'next_cursor': None}
    assert store.due_items(db_path=path) == []
    assert not Path(path).parent.exists()
    def denied(*args): raise PermissionError('synthetic permission denial')
    monkeypatch.setattr(private_data, 'prove_private', denied)
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
    monkeypatch.setattr(ingest, 'poll_all', lambda **kw: {
        FIX['stream']: len(ingest.poll_stream(FIX['stream'], channel, 'synthetic', owner))})
    monkeypatch.setattr(ingest, 'poll_all_reactions', lambda **kw: {})
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
        agent_run._llm(FIX['request'], None, None, 'agent')
    with pytest.raises(ModuleNotFoundError):
        dispatch.call_chain(FIX['request'])


@pytest.mark.parametrize('value', [[], None, False])
def test_malformed_evidence_still_produces_incomplete_health(tmp_path, monkeypatch, value):
    database, _, _ = configured(tmp_path, monkeypatch)
    (tmp_path/'readiness.json').write_text(json.dumps(value))
    report = store.health(db_path=str(database))['readiness']
    assert not report['ready']
    assert report['capabilities']['remind']['status'] == 'unmeasured'


def test_boolean_exit_code_cannot_publish_success(tmp_path, monkeypatch):
    database, config, _ = configured(tmp_path, monkeypatch)
    receipt = {**FIX['receipt'], 'exit_code': False}
    assert not cap.publish_delivery('remind', receipt, database, config)
    assert not (tmp_path/'readiness.json').exists()


def test_dispatch_uses_result_text_and_installed_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(sys.modules['llmcall'], 'call', lambda prompt, **kwargs:
                        calls.append(kwargs) or SimpleNamespace(**FIX['model_result'], error=None))
    assert dispatch.call_chain(FIX['request']) == FIX['model_result']['text']
    assert calls == [{'mode': 'judge', 'log': None}]
