"""Grouped installer planning and read-only measured readiness."""
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import private_data

HERE = Path(__file__).resolve().parent
GROUPS = ('store', 'remind', 'ingest', 'work')
TASKS = {'remind': ('ScheduleReminderTick', 300), 'ingest': ('AgentCenterIngestTick', 600),
         'work': ('AgentCenterWorkTick', 120)}
IDENTITY = ('task_name', 'interpreter', 'arguments', 'working_directory', 'interval_seconds',
            'adapter_sha256', 'config_sha256')


def select(value=None):
    if value is None:
        value = os.environ.get('SCHEDULE_CAPABILITIES', 'store,remind')
    names = [name.strip() for name in value.split(',') if name.strip()]
    unknown = set(names)-set(GROUPS)
    if unknown:
        raise ValueError('unsupported capabilities: '+', '.join(sorted(unknown)))
    return set(names)


def file_hash(path):
    path = Path(path).expanduser().resolve()
    if path.name.lower() == 'agent-center.md':
        raise ValueError('excluded document is not a runtime adapter or configuration')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan(selection=None, db_path=None, python=None, config=None):
    selected = select(selection)
    db = str(Path(db_path or os.environ.get('SCHEDULE_DB_PATH') or private_data.data_dir()/'db.sqlite3').expanduser().resolve())
    interpreter = str(Path(python or os.environ.get('SCHEDULE_PYTHON') or sys.executable).expanduser().resolve())
    registry = str(Path(config).expanduser().resolve() if config else private_data.registry_path())
    rows = {name: {'selected': name in selected, 'status': 'unmeasured' if name in selected else 'not_selected',
                   'reasons': ['not yet measured' if name in selected else 'not selected']} for name in GROUPS}
    for name in selected.intersection(TASKS):
        task, interval = TASKS[name]
        adapter = str(HERE/'scheduler_worker.py')
        arguments = subprocess.list2cmdline([adapter, '--capability', name, '--db', db, '--config', registry])
        rows[name].update(runtime=interpreter, adapter=adapter,
                         task={'name': task, 'interval_seconds': interval,
                               'interval': 'PT%dM' % (interval//60), 'arguments': arguments,
                               'working_directory': str(HERE)})
    return {'capabilities': rows, 'dependencies': {
        'store': {'required': bool(selected), 'db_path': db, 'private_versioned': True},
        'relay': {'required': bool(selected-{'store'}), 'config': registry},
        'llmcall': {'required': bool(selected & {'ingest', 'work'})}}, 'config': registry}


def task_identity(row, config):
    task = row['task']
    return {'task_name': task['name'], 'interpreter': row['runtime'], 'arguments': task['arguments'],
            'working_directory': task['working_directory'], 'interval_seconds': task['interval_seconds'],
            'adapter_sha256': file_hash(row['adapter']), 'config_sha256': file_hash(config)}


def read_task_xml(name):
    result = subprocess.run(['schtasks', '/Query', '/TN', name, '/XML'], capture_output=True,
                            timeout=20)
    if result.returncode:
        raise ValueError('registered task XML unavailable')
    raw = result.stdout
    if isinstance(raw, str):
        return raw
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ('utf-16-le' if b'\x00' in raw[:64] else 'utf-8-sig')
    return raw.decode(encoding)


def _arguments(value):
    return [part.strip('"') for part in shlex.split(value or '', posix=False)]


def verify_task(xml, row):
    root = ET.fromstring(xml)
    for node in root.iter():
        node.tag = node.tag.rsplit('}', 1)[-1]
    tasks = row['task']
    actions = root.findall('./Actions/Exec')
    triggers = root.findall('./Triggers/TimeTrigger')
    if (len(actions) != 1 or len(triggers) != 1 or len(root.findall('./Actions/*')) != 1
            or len(root.findall('./Triggers/*')) != 1 or root.findtext('./Settings/Enabled') != 'true'):
        return False, 'task must have one action, one time trigger and enabled settings'
    action, trigger = actions[0], triggers[0]
    path_equal = lambda a, b: os.path.normcase(os.path.abspath(a or '')) == os.path.normcase(os.path.abspath(b))
    valid = (path_equal(action.findtext('Command'), row['runtime'])
             and _arguments(action.findtext('Arguments')) == _arguments(tasks['arguments'])
             and path_equal(action.findtext('WorkingDirectory'), tasks['working_directory'])
             and trigger.findtext('Enabled') == 'true'
             and trigger.findtext('Repetition/Interval') == tasks['interval']
             and trigger.find('Repetition/Duration') is None and trigger.find('EndBoundary') is None)
    return valid, 'task identity and unbounded interval verified' if valid else 'task action, trigger or repetition mismatch'


def probe_runtime(row):
    required = {TASKS['remind'][0]: 'store,notify,relay',
                TASKS['ingest'][0]: 'store,relay,llmcall,ingest_tick,ingest,dispatch',
                TASKS['work'][0]: 'store,relay,llmcall,agent_tick,agent_task,agent_run'}[row['task']['name']]
    code = ('import importlib,json,sys;sys.path.insert(0,sys.argv[1]);'
            '[importlib.import_module(name) for name in sys.argv[2].split(",")];'
            'import scheduler_worker;print(json.dumps({"runtime":True}))')
    result = subprocess.run([row['runtime'], '-B', '-c', code, str(HERE), required],
                            capture_output=True, text=True, encoding='utf-8', timeout=20)
    return result.returncode == 0 and json.loads(result.stdout.strip()).get('runtime') is True


def evidence_path(db_path):
    override = os.environ.get('SCHEDULE_READINESS_EVIDENCE')
    return Path(override).expanduser().resolve() if override else Path(db_path).resolve().parent/'readiness.json'


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})', value):
        raise ValueError('evidence timestamp must be RFC3339')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('evidence timestamp must include timezone')
    return result


def valid_evidence(record, identity, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(record, dict) or any(record.get(key) != value for key, value in identity.items()):
        return False, 'worker identity does not match configured task'
    dispatch = record.get('dispatch')
    if not isinstance(dispatch, dict) or any(dispatch.get(key) != value for key, value in identity.items()):
        return False, 'dispatch identity missing or mismatched'
    if (dispatch.get('delivered') is not True or type(dispatch.get('exit_code')) is not int
            or dispatch['exit_code'] != 0 or not isinstance(dispatch.get('receipt_id'), str)
            or not dispatch['receipt_id'].strip() or not isinstance(dispatch.get('kind'), str) or not dispatch['kind'].strip()):
        return False, 'confirmed delivery receipt unavailable'
    try:
        for stamp in (record.get('last_success'), dispatch.get('completed_at')):
            age = (now-_timestamp(stamp)).total_seconds()
            if not -60 <= age <= 3*identity['interval_seconds']:
                return False, 'worker evidence is stale or future-dated'
    except (ValueError, TypeError, AttributeError):
        return False, 'invalid worker timestamp'
    return True, ('synthetic local delivery verified; external readiness unmeasured'
                  if dispatch['kind'] == 'synthetic-local' else 'current identity-bound delivery verified')


def readiness(store_health, selection=None, python=None, config=None):
    result = plan(selection, store_health['db_path'], python, config)
    rows, dependencies = result['capabilities'], result['dependencies']
    selected = {name for name, row in rows.items() if row['selected']}
    if not selected:
        return {'ready': False, 'capabilities': rows, 'dependencies': dependencies}
    try:
        proof = private_data.prove_private(store_health['db_path'])
        store_ok = all(store_health.get(key) for key in ('db_ok', 'wal_ok', 'integrity_ok')) and store_health.get('schema_user_version') == 1
        store_reason = 'PRIVATE initialized store verified' if store_ok else 'store is not initialized or healthy'
        dependencies['store'].update(ready=store_ok, proof=proof)
    except (OSError, ValueError) as error:
        store_ok, store_reason = False, str(error)
        dependencies['store'].update(ready=False, reason=store_reason)
    if rows['store']['selected']:
        rows['store'].update(status='ready' if store_ok else 'unavailable', reasons=[store_reason])
    evidence, evidence_error = {}, 'worker evidence unavailable'
    try:
        path = evidence_path(store_health['db_path'])
        private_data.prove_private(path)
        evidence = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(evidence, dict) or evidence.get('schema_version') != 1 or not isinstance(evidence.get('tasks'), dict):
            raise ValueError('invalid worker evidence schema')
    except (OSError, ValueError, TypeError) as error:
        evidence, evidence_error = {}, str(error)
    for name in selected.intersection(TASKS):
        row = rows[name]
        reasons = [store_reason]
        if not store_ok:
            row.update(status='unavailable', reasons=reasons)
            continue
        try:
            task_ok, reason = verify_task(read_task_xml(row['task']['name']), row)
            reasons.append(reason)
            if not task_ok:
                row.update(status='partial', reasons=reasons)
                continue
            if not probe_runtime(row):
                raise ValueError('selected runtime or adapter import unavailable')
            reasons.append('selected runtime and adapter import verified')
            row['runtime_ready'] = True
            private_data.prove_private(result['config'])
            identity = task_identity(row, result['config'])
            record = evidence.get('tasks', {}).get(name)
            if record is None:
                row.update(status='unmeasured', reasons=reasons+[evidence_error])
                continue
            valid, reason = valid_evidence(record, identity)
            external = bool(valid and record['dispatch']['kind'] != 'synthetic-local')
            row.update(status='ready' if external else 'partial', reasons=reasons+[reason],
                       external_ready=bool(valid and record['dispatch']['kind'] != 'synthetic-local'))
        except (OSError, ValueError, TypeError, KeyError, ET.ParseError, subprocess.SubprocessError) as error:
            row.update(status='unavailable', reasons=reasons+[str(error)])
    for name, groups, field in [('relay', selected.intersection(TASKS), 'external_ready'),
                               ('llmcall', selected.intersection({'ingest', 'work'}), 'runtime_ready')]:
        ready = bool(groups) and all(rows[group].get(field, False) for group in groups)
        dependencies[name].update(ready=ready,
                                 status='ready' if ready else ('unmeasured' if groups else 'not_required'))
    return {'ready': bool(selected) and all(rows[name]['status'] == 'ready' for name in selected),
            'capabilities': rows, 'dependencies': dependencies}


def publish_delivery(capability, receipt, db_path, config=None, python=None):
    """Only a worker that returned successfully with a confirmed receipt calls this."""
    if (not isinstance(receipt, dict) or receipt.get('delivered') is not True
            or type(receipt.get('exit_code')) is not int or receipt['exit_code'] != 0
            or any(not isinstance(receipt.get(key), str) or not receipt[key].strip()
                   for key in ('receipt_id', 'kind'))):
        return False
    selected = plan(capability, db_path, python, config)
    identity = task_identity(selected['capabilities'][capability], selected['config'])
    stamp = datetime.now(timezone.utc).isoformat()
    record = {**identity, 'last_success': stamp,
              'dispatch': {**receipt, **identity, 'completed_at': stamp}}
    path = Path(db_path).resolve().parent/'readiness.json'
    private_data.prepare_parent(path)
    with private_data.file_lock(str(path)+'.lock'):
        _write_evidence(path, capability, record)
    return True


def _write_evidence(path, capability, record):
    try:
        existing = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        existing = {'schema_version': 1, 'tasks': {}}
    if not isinstance(existing, dict) or existing.get('schema_version') != 1 or not isinstance(existing.get('tasks'), dict):
        raise ValueError('cannot overwrite malformed readiness evidence')
    existing['tasks'][capability] = record
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='readiness-', suffix='.tmp')
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(existing, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
