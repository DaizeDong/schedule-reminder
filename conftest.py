"""Install synthetic profile and subprocess boundaries before test collection."""
import json
import os
from pathlib import Path
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURE = json.loads((ROOT/'skills/schedule-reminder/tests/capability_cases.json').read_text(encoding='utf-8'))
sys.path.insert(0, str(ROOT/'skills/schedule-reminder/tests'))
sys.path.insert(0, str(ROOT/'skills/schedule-reminder/scripts'))
PROFILE = Path(tempfile.mkdtemp(prefix='schedule-tests-profile-'))
language_rule = os.environ.get('SCHEDULE_TEST_LANGUAGE_RULE')
if language_rule:
    rule_path = Path(language_rule).expanduser().resolve()
    if rule_path.name != 'notification_language.py':
        raise ValueError('test language dependency must name the code-only notification rule')
    target = PROFILE/'.claude/scripts/notification_language.py'
    target.parent.mkdir(parents=True)
    target.write_bytes(rule_path.read_bytes())
for name in list(os.environ):
    if name.startswith(('SCHEDULE_', 'AGENT_CENTER_', 'AGENT_EXEC_', 'LLMCALL_')) and name != 'SCHEDULE_TEST_TRACE':
        os.environ.pop(name)
os.environ.update(HOME=str(PROFILE), USERPROFILE=str(PROFILE), GH_CONFIG_DIR=str(PROFILE/'gh'),
                  USERDOMAIN=FIXTURE['user'].split('\\')[0], USERNAME=FIXTURE['user'].split('\\')[1],
                  PYTHONDONTWRITEBYTECODE='1', GIT_OPTIONAL_LOCKS='0',
                  SCHEDULE_TEST_PROFILE=str(PROFILE))
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
