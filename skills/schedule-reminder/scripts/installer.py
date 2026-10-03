"""Capability installation with an effect-free plan and independent task readback."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

import capabilities
import private_data
import store


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
        current = capabilities.read_task_xml(name)
        matches, _ = capabilities.verify_task(current, row)
    except (OSError, ValueError, ET.ParseError, subprocess.SubprocessError):
        matches = False
    if not matches:
        private_data.prove_private(data_directory)
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
    verified, reason = capabilities.verify_task(capabilities.read_task_xml(name), row)
    if not verified:
        raise RuntimeError('task readback failed: '+name+': '+reason)
    return {'name': name, 'registered': not matches, 'readback_verified': True}


def install(selection=None, db_path=None, python=None, config=None, no_task=False):
    selection = 'store,remind' if selection is None else selection
    planned = capabilities.plan(selection, db_path, python, config)
    selected = [name for name, row in planned['capabilities'].items() if row['selected']]
    if not selected:
        return 0, {**planned, 'noop': True}
    database = planned['dependencies']['store']['db_path']
    private_data.prove_private(database)
    for name in selected:
        row = planned['capabilities'][name]
        if name in capabilities.TASKS:
            private_data.prove_private(planned['config'])
            capabilities.task_identity(row, planned['config'])
            if not capabilities.probe_runtime(row):
                raise RuntimeError('selected runtime or adapter unavailable: '+name)
    store.init_db(database)
    registered = []
    if not no_task:
        for name in selected:
            if name in capabilities.TASKS:
                registered.append(register(planned['capabilities'][name], Path(database).parent))
    measured = store.health(db_path=database, capabilities='')
    measured['readiness'] = capabilities.readiness(measured, ','.join(selected), python, config)
    return (0 if measured['readiness']['ready'] else 1), {'health': measured, 'tasks': registered}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--capabilities', default=None)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--no-task', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.plan:
            result, code = capabilities.plan('store,remind' if args.capabilities is None else args.capabilities), 0
        else:
            code, result = install(args.capabilities, no_task=args.no_task)
    except Exception as error:
        result, code = {'ok': False, 'error': str(error)}, 1
    print(json.dumps(result))
    return code


if __name__ == '__main__':
    sys.exit(main())
