"""Capability installation with an effect-free plan and independent task readback.

Each selected scheduled capability registers its real entrypoint: `reminder.py --db DB tick`,
`ingest_tick.py` or `agent_tick.py`. The ingest and work entrypoints read SCHEDULE_DB_PATH and
AGENT_CENTER_CONFIG from the user environment, as the live registrations do.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import private_data
import store

HERE = Path(__file__).resolve().parent
GROUPS = ('store', 'remind', 'ingest', 'work')
TASKS = {'remind': ('ScheduleReminderTick', 300), 'ingest': ('AgentCenterIngestTick', 600),
         'work': ('AgentCenterWorkTick', 120)}
ENTRYPOINTS = {'remind': 'reminder.py', 'ingest': 'ingest_tick.py', 'work': 'agent_tick.py'}


def select(value=None):
    if value is None:
        value = os.environ.get('SCHEDULE_CAPABILITIES', 'store,remind')
    names = [name.strip() for name in value.split(',') if name.strip()]
    unknown = set(names)-set(GROUPS)
    if unknown:
        raise ValueError('unsupported capabilities: '+', '.join(sorted(unknown)))
    return set(names)


def plan(selection=None, db_path=None, python=None, config=None):
    selected = select(selection)
    db = str(Path(db_path or os.environ.get('SCHEDULE_DB_PATH') or private_data.data_dir()/'db.sqlite3').expanduser().resolve())
    interpreter = str(Path(python or os.environ.get('SCHEDULE_PYTHON') or sys.executable).expanduser().resolve())
    registry = str(Path(config).expanduser().resolve() if config else private_data.registry_path())
    rows = {name: {'selected': name in selected, 'status': 'planned' if name in selected else 'not_selected'}
            for name in GROUPS}
    for name in selected.intersection(TASKS):
        task, interval = TASKS[name]
        entrypoint = str(HERE/ENTRYPOINTS[name])
        argv = [entrypoint, '--db', db, 'tick'] if name == 'remind' else [entrypoint]
        rows[name].update(runtime=interpreter, entrypoint=entrypoint,
                          task={'name': task, 'interval_seconds': interval,
                                'interval': 'PT%dM' % (interval//60),
                                'arguments': subprocess.list2cmdline(argv),
                                'working_directory': str(HERE)})
    return {'capabilities': rows, 'dependencies': {
        'store': {'required': bool(selected), 'db_path': db, 'private_versioned': True},
        'relay': {'required': bool(selected-{'store'}), 'config': registry},
        'llmcall': {'required': bool(selected & {'ingest', 'work'})}}, 'config': registry}


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


def verify_task(xml, row, now=None):
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
    try:
        boundary = trigger.findtext('StartBoundary') or ''
        if 'T' not in boundary:
            raise ValueError('missing time component')
        start = datetime.fromisoformat(boundary.replace('Z', '+00:00')).astimezone(timezone.utc)
        clock = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return False, 'task start boundary is missing or invalid'
    if start > clock:
        return False, 'task start boundary has not been reached'
    path_equal = lambda a, b: os.path.normcase(os.path.abspath(a or '')) == os.path.normcase(os.path.abspath(b))
    valid = (path_equal(action.findtext('Command'), row['runtime'])
             and _arguments(action.findtext('Arguments')) == _arguments(tasks['arguments'])
             and path_equal(action.findtext('WorkingDirectory'), tasks['working_directory'])
             and trigger.findtext('Enabled') == 'true'
             and trigger.findtext('Repetition/Interval') == tasks['interval']
             and trigger.find('Repetition/Duration') is None and trigger.find('EndBoundary') is None)
    return valid, 'task identity and active unbounded interval verified' if valid else 'task action, trigger or repetition mismatch'


def task_xml(row, user=None):
    root = ET.Element('Task', version='1.3', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
    trigger = ET.SubElement(ET.SubElement(root, 'Triggers'), 'TimeTrigger')
    ET.SubElement(trigger, 'Enabled').text = 'true'
    ET.SubElement(trigger, 'StartBoundary').text = datetime.now().isoformat(timespec='seconds')
    repetition = ET.SubElement(trigger, 'Repetition')
    ET.SubElement(repetition, 'Interval').text = row['task']['interval']
    ET.SubElement(repetition, 'StopAtDurationEnd').text = 'false'
    principal = ET.SubElement(ET.SubElement(root, 'Principals'), 'Principal', id='Author')
    ET.SubElement(principal, 'UserId').text = user or (os.environ.get('USERDOMAIN', '')+'\\'+os.environ.get('USERNAME', ''))
    ET.SubElement(principal, 'LogonType').text = 'InteractiveToken'
    ET.SubElement(principal, 'RunLevel').text = 'LeastPrivilege'
    settings = ET.SubElement(root, 'Settings')
    for key, value in {'Enabled': 'true', 'MultipleInstancesPolicy': 'IgnoreNew',
                       'StartWhenAvailable': 'true', 'DisallowStartIfOnBatteries': 'false',
                       'StopIfGoingOnBatteries': 'false', 'ExecutionTimeLimit': 'PT0S'}.items():
        ET.SubElement(settings, key).text = value
    action = ET.SubElement(ET.SubElement(root, 'Actions', Context='Author'), 'Exec')
    for key, value in {'Command': row['runtime'], 'Arguments': row['task']['arguments'],
                       'WorkingDirectory': row['task']['working_directory']}.items():
        ET.SubElement(action, key).text = value
    return ET.tostring(root, encoding='unicode')


def register(row, data_directory):
    name = row['task']['name']
    try:
        matches, _ = verify_task(read_task_xml(name), row)
    except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError):
        matches = False
    if not matches:
        private_data.check_destination(data_directory)
        descriptor, filename = tempfile.mkstemp(prefix='task-', suffix='.xml', dir=data_directory)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-16') as stream:
                stream.write('<?xml version="1.0" encoding="UTF-16"?>\n'+task_xml(row))
            result = subprocess.run(['schtasks', '/Create', '/TN', name, '/XML', filename, '/F'],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise RuntimeError('task registration failed: '+name)
        finally:
            Path(filename).unlink(missing_ok=True)
    verified, reason = verify_task(read_task_xml(name), row)
    if not verified:
        raise RuntimeError('task readback failed: '+name+': '+reason)
    return {'name': name, 'registered': not matches, 'readback_verified': True}


def install(selection=None, db_path=None, python=None, config=None, no_task=False):
    """Initialize the store, register selected tasks and report health.

    Exit code 0 means the database is healthy and, when a scheduled capability is selected, the
    relay registry is present. Task readback failures raise."""
    selection = 'store,remind' if selection is None else selection
    planned = plan(selection, db_path, python, config)
    selected = [name for name, row in planned['capabilities'].items() if row['selected']]
    if not selected:
        return 0, {**planned, 'noop': True}
    database = planned['dependencies']['store']['db_path']
    private_data.check_destination(database)
    scheduled = [name for name in selected if name in TASKS]
    if scheduled:
        private_data.check_destination(planned['config'])
    store.init_db(database)
    registered = []
    if not no_task:
        for name in scheduled:
            registered.append(register(planned['capabilities'][name], Path(database).parent))
    measured = store.health(db_path=database)
    relay_ready = not scheduled or Path(planned['config']).is_file()
    ok = measured['db_ok'] and measured['integrity_ok'] and relay_ready
    return (0 if ok else 1), {'health': measured, 'tasks': registered, 'relay_registry': relay_ready}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--capabilities', default=None)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--no-task', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.plan:
            result, code = plan('store,remind' if args.capabilities is None else args.capabilities), 0
        else:
            code, result = install(args.capabilities, no_task=args.no_task)
    except Exception as error:
        result, code = {'ok': False, 'error': str(error)}, 1
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
