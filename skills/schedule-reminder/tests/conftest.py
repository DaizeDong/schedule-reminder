"""Install synthetic profile and subprocess boundaries before test collection."""
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = json.loads((ROOT/'skills/schedule-reminder/tests/capability_cases.json').read_text(encoding='utf-8'))
sys.path.insert(0, str(ROOT/'skills/schedule-reminder/tests'))
sys.path.insert(0, str(ROOT/'skills/schedule-reminder/scripts'))
PROFILE = Path(tempfile.mkdtemp(prefix='schedule-tests-profile-'))
language_rule = os.environ.get('SCHEDULE_TEST_LANGUAGE_RULE')
if not language_rule:
    raise RuntimeError('canonical test dependency missing: prepare notification_language.py and set SCHEDULE_TEST_LANGUAGE_RULE')
sys.path.insert(0, str(ROOT/'tools'))
from prepare_test_dependency import regular_bytes, validate
rule_path = Path(language_rule).expanduser().absolute()
if rule_path.name != 'notification_language.py':
    raise ValueError('test language dependency must name the code-only notification rule')
target = PROFILE/'route-scripts/notification_language.py'
target.parent.mkdir(parents=True)
target.write_bytes(validate(regular_bytes(rule_path)))
for name in list(os.environ):
    if name.startswith(('SCHEDULE_', 'AGENT_CENTER_', 'AGENT_EXEC_', 'LLMCALL_')) and name not in {
            'SCHEDULE_TEST_TRACE', 'SCHEDULE_TEST_TASK_CONSOLE_ROOT'}:
        os.environ.pop(name)
os.environ.update(HOME=str(PROFILE), USERPROFILE=str(PROFILE), GH_CONFIG_DIR=str(PROFILE/'gh'),
                  USERDOMAIN=FIXTURE['user'].split('\\')[0], USERNAME=FIXTURE['user'].split('\\')[1],
                  PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0',
                  SCHEDULE_TEST_PROFILE=str(PROFILE), SCHEDULE_ROUTE_SCRIPTS_DIR=str(target.parent))
bootstrap = PROFILE/'bootstrap'
bootstrap.mkdir()
(bootstrap/'sitecustomize.py').write_text(
    'import sys\nsys.path.insert(0, '+repr(str(ROOT/'skills/schedule-reminder/tests'))+')\n'
    'import offline_support\noffline_support.install()\n', encoding='utf-8')
os.environ['PYTHONPATH'] = str(bootstrap)
import offline_support
offline_support.install()


@pytest.fixture(autouse=True)
def synthetic_companion(tmp_path, monkeypatch):
    metadata = tmp_path/'.git'
    metadata.mkdir(exist_ok=True)
    data = FIXTURE
    (metadata/'config').write_text('[remote "origin"]\nurl = '+data['private_remote'], encoding='utf-8')
    monkeypatch.setenv('SCHEDULE_REMINDER_CONFIG', str(tmp_path))
    monkeypatch.setenv('SCHEDULE_DB_PATH', str(tmp_path/'reminders.sqlite3'))
    monkeypatch.setenv('AGENT_CENTER_RUNS', str(tmp_path/'runs'))
    monkeypatch.setenv('AGENT_CENTER_CONFIG', str(tmp_path/'registry.json'))


def pytest_sessionfinish(session, exitstatus):
    offline_support.ACTIVE = False
