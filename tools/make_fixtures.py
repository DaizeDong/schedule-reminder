"""Deterministic synthetic inputs for capability and DATA-boundary tests."""
import argparse
import json
from pathlib import Path

FIXTURE = 'skills/schedule-reminder/tests/capability_cases.json'


def sqlite_path_stat(kind):
    """Generate filesystem observations without creating aliases or real DATA."""
    import stat
    from types import SimpleNamespace
    mode, links, attributes = {
        'regular': (stat.S_IFREG | 0o600, 1, 0),
        'unlinked': (stat.S_IFREG | 0o600, 0, 0),
        'hardlink': (stat.S_IFREG | 0o600, 2, 0),
        'symlink': (stat.S_IFLNK | 0o777, 1, 0),
        'reparse': (stat.S_IFREG | 0o600, 1, 1024),
        'unlinked-reparse': (stat.S_IFREG | 0o600, 0, 1024),
    }[kind]
    return SimpleNamespace(st_mode=mode, st_nlink=links, st_file_attributes=attributes)


def sqlite_path_race_cases():
    """Generate unlink races and stable refusal controls for SQLite sidecars."""
    return [
        {'name': name, 'observations': observations, 'allowed': allowed}
        for name, observations, allowed in [
            ('missing', ['missing'], True),
            ('regular', ['regular'], True),
            ('unlinked-then-missing', ['unlinked', 'missing'], True),
            ('unlinked-then-regular', ['unlinked', 'regular'], True),
            ('unlinked-twice-then-regular', ['unlinked', 'unlinked', 'regular'], True),
            ('stable-unlinked', ['unlinked'], False),
            ('hardlink', ['hardlink', 'regular'], False),
            ('symlink', ['symlink', 'regular'], False),
            ('reparse', ['reparse', 'regular'], False),
            ('unlinked-reparse', ['unlinked-reparse', 'regular'], False),
            ('unlinked-then-hardlink', ['unlinked', 'hardlink', 'regular'], False),
            ('unlinked-then-symlink', ['unlinked', 'symlink', 'regular'], False),
            ('unlinked-then-reparse', ['unlinked', 'reparse', 'regular'], False),
        ]
    ]


def runtime_storage_case(root, run, *, versioned=True):
    """Build native Git metadata and fresh synthetic visibility without live accounts."""
    from datetime import datetime, timezone
    import os
    root = Path(root)
    home, repository = root/'home', root/'repository'
    (home/'.pii-guard').mkdir(parents=True)
    repository.mkdir()
    environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'PATH')
                   if key in os.environ}
    environment.update(HOME=str(home), USERPROFILE=str(home), PROGRAMDATA=str(home/'programdata'),
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                       GIT_AUTHOR_NAME='Synthetic User', GIT_COMMITTER_NAME='Synthetic User',
                       GIT_AUTHOR_EMAIL='user1@example.com', GIT_COMMITTER_EMAIL='user1@example.com')
    def git(*arguments, input=None):
        return run(['git', '-C', str(repository), *arguments], env=environment,
                   input=input, capture_output=True, text=True, encoding='utf-8', check=True).stdout.strip()
    git('init', '-q')
    git('remote', 'add', 'origin', cases()['private_remote'])
    (repository/'.gitignore').write_text('ignored/\n*.lock\n', encoding='utf-8')
    if versioned:
        git('add', '.gitignore')
        tree = git('write-tree')
        commit = git('commit-tree', tree, input='Generated synthetic storage fixture\n')
        git('update-ref', 'HEAD', commit)
    (home/'.pii-guard/visibility.json').write_text(json.dumps({
        '_refreshed': datetime.now(timezone.utc).isoformat(), **cases()['visibility']}), encoding='utf-8')
    return {'repository': repository, 'environment': environment, 'record': cases()['adapter_text'],
            'git': git, 'ignored_record_rule': 'record.json\n',
            'changed_commit_message': 'Generated synthetic next revision\n'}


def creation_case(source='synthetic-a', request='request-a'):
    return {'title': 'Prepare Acme report', 'description': 'Review the synthetic quarterly figures.',
            'due_at': '2030-05-01T12:00:00Z', 'source': source, 'idempotency_key': request}


def dispatch_occurrences():
    return [dict(dispatch_case()[0], actions=[dict(dispatch_case()[0]['actions'][0],
            due_at='2030-05-%02dT12:00:00Z' % day)]) for day in (1, 2, 3)]


def cleanup_recovery_evidence():
    return {'note': 'Synthetic containment audit confirms the stopped process tree is gone',
            'evidence_sha256': 'a' * 64}


def llm_call_result(*, text='', provider='synthetic-provider', error='', data=None,
                    attempts=None, effective_model='synthetic-model',
                    model_family='synthetic-family', execution_started=False,
                    outcome='success', effects='none', cleanup_confirmed=True,
                    include_metadata=True):
    """Generate results without requiring optional fields in installed llmcall."""
    from types import SimpleNamespace
    values = {'text': text, 'provider': provider, 'error': error, 'data': data,
              'attempts': list(attempts or []), 'depth': 0, 'group': None}
    if include_metadata:
        values.update(effective_model=effective_model, model_family=model_family,
                      execution_started=execution_started, outcome=outcome,
                      effects=effects, cleanup_confirmed=cleanup_confirmed)
    return SimpleNamespace(**values)


def cleanup_call_result(confirmed):
    """Generate an execution timeout with an explicit cleanup observation."""
    from types import SimpleNamespace
    attempt = SimpleNamespace(provider='synthetic-provider', success=False, elapsed=1,
                              outcome='timeout', execution_started=True,
                              cleanup_confirmed=confirmed)
    return llm_call_result(error='synthetic timeout', outcome='timeout',
                           execution_started=True, effects='possible',
                           attempts=[attempt], cleanup_confirmed=confirmed)


def notification_case():
    """Generate notification claims, content and transport credentials for tests."""
    return {
        'registry': {'streams': {'test': {'channel_id': '10001'}},
                     'reader': {'bot_token': 'SYNTHETIC_TOKEN'},
                     'big_brother': {'user_id': '20002'}},
        'event': {'owner': 'test-owner', 'run_id': 'synthetic-run',
                  'phase': 'terminal', 'condition': 'success', 'stream': 'test'},
        'content': '任务已完成', 'changed_content': '任务失败',
        'attachment': 'Acme synthetic attachment.\n',
        'changed_attachment': 'Acme changed synthetic attachment.\n',
        'command_echo_code': 'import sys; print(sys.argv[-1])',
        'command_wait_code': 'import time; time.sleep(30)',
        'command_file_code': 'from pathlib import Path; import sys; print(Path(sys.argv[-1][1:]).read_text(encoding="utf-8"), end="")',
    }


def task_control_result():
    """Generate the controller's acknowledged task request, without a success claim."""
    return {'schemaVersion': 1, 'ok': True, 'name': 'SyntheticTask', 'verb': 'run',
            'status': 'run_requested', 'acknowledged': True, 'before': 'Ready',
            'after': 'Running', 'payload_success': None,
            'message': 'Synthetic scheduler accepted run'}


def task_control_binding():
    """Generate an owner-reviewed task identity for injected authority controls."""
    return {'task_id': 'synthetic/task', 'name': task_control_result()['name'],
            'review_revision': 'sha256:' + 'a' * 64}


def linkage_request():
    """Generate a small request for injected linkage-authority unit tests."""
    return {
        'components': [{'component': 'acme-maintenance', 'tasks': [{'id': 'sync'}]}],
        'bindings': {'tasks': {'acme-maintenance/sync': {'name': 'AcmeSync'}}},
    }


def blocked_queue_database(path):
    import store
    store.init_db(str(path))
    old = store.add_item('Acme interrupted work', source='agent-center:work',
        ext={'x_agent_exec_state': 'queued'}, db_path=str(path))
    store.claim_work(old['id'], db_path=str(path))
    for action, fields in [('spawn', {}), ('start', {'pid': 123, 'pstart': 456}),
            ('child_start', {'receipt': {'phase': 'actor'}}),
            ('child_finish', {'receipt': {'outcome': 'timeout'}, 'outcome': 'unknown'})]:
        store.advance_work(old['id'], 1, action, db_path=str(path), **fields)
    store.transition(old['id'], 'cancelled', db_path=str(path))
    waiting = store.add_item('Acme queued work', source='agent-center:work',
        ext={'x_agent_exec_state': 'queued'}, db_path=str(path))
    return old, waiting


def dispatch_case(workspace=None):
    return {'actions': [{'op': 'create', 'title': 'Prepare Acme report',
                         'due_at': '2030-05-01T12:00:00Z'}]}, {
        'actions': [{'op': 'agent', 'request': 'Prepare the synthetic Acme report',
                     'workspace': str(workspace) if workspace else None}]}


def watchdog_database(path):
    import store
    store.init_db(str(path))
    for key in ('weekly-poll-watchdog', 'monthly-refresh-watchdog', 'research-followup'):
        store.add_item('Acme monitor ' + key, source='market-intel',
                       idempotency_key='market-intel:' + key, db_path=str(path))
    return path


def artifact_workspace(path, size=24):
    """Generate a non-code workspace containing a synthetic report."""
    path.mkdir(parents=True, exist_ok=True)
    (path / 'report.txt').write_text(('Acme synthetic report.\n' * (size // 22 + 1))[:size], encoding='utf-8')
    return path


def action_database(path, *, source='user', blocked=False):
    """Generate a full owner database and one actionable synthetic todo."""
    import store
    store.init_db(str(path))
    item = store.add_item('整理 Acme 报告', description='整理合成资料，输出一份报告。',
                          source=source, db_path=str(path))
    if blocked:
        blocker = store.add_item('Acme prerequisite', source='user', db_path=str(path))
        item = store.block(item['id'], blocker_id=blocker['id'], db_path=str(path))
    return item


def email_work_database(path):
    import store
    store.init_db(str(path))
    parent = store.add_item('Review Acme invoice', source='email-monitor', kind='task', db_path=str(path))
    child = store.add_item('Acme invoice received', source='email-monitor', kind='event', db_path=str(path),
        ext={'x_console_consolidation':{'duplicate_of':parent['id'],'secret':'never forward'}})
    return parent,child


def work_database(path):
    import sqlite3
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE items(id TEXT PRIMARY KEY,title TEXT,state TEXT,source TEXT,
            description TEXT,priority INTEGER,progress INTEGER,due_at TEXT,scheduled_at TEXT,
            project TEXT,created_at TEXT,updated_at TEXT,ext TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY,ts TEXT,item_id TEXT,actor TEXT,event_type TEXT,
            from_state TEXT,to_state TEXT);''')
        rows = [
            ('work-done','Acme result','done','agent-center:work',{'x_agent_exec_state':'done','x_agent_exec_note':'Synthetic summary','secret':'never forward'}),
            ('work-stalled','Acme draft','blocked','agent-center:work',{'x_agent_exec_state':'stalled'}),
            ('work-running','Acme active','doing','agent-center:work',{'x_agent_exec_state':'running'}),
            ('tracked','Synthetic commitment','pending','user',{}),
            ('signal','Synthetic news','pending','daily-hotspots',{}),
        ]
        for item_id,title,state,source,ext in rows:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (item_id,title,state,source,'Synthetic description',1,0,None,None,'Acme',
                 '2030-01-01T00:00:00Z','2030-01-02T00:00:00Z',json.dumps(ext)))
        for seq,item_id in enumerate(('work-done','signal','tracked'),1):
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                (seq,'2030-01-02T00:00:00Z',item_id,'synthetic','transition','pending','done'))
    return path


def cases():
    return {
        "synthetic_thread_titles": ["\u9700\u56de\u590d:\u5408\u6210\u4efb\u52a1A", "\u5f85\u529e:\u5408\u6210\u4efb\u52a1B"],
        'schedule3_created_item': {'id': 'synthetic-created-31', 'state': 'pending'},
        'schedule3': {
            'channel_id': '3102', 'owner_id': '3103',
            'messages': [
                {'id': '3105', 'content': 'Remember synthetic obligation A',
                 'author': {'id': '3103'}, 'timestamp': '2031-04-12T14:00:00+00:00'},
                {'id': '3106', 'content': 'Remember synthetic obligation B',
                 'author': {'id': '3103'}, 'timestamp': '2031-04-12T14:01:00+00:00'},
            ],
            'create_plan': {'actions': [
                {'op': 'create', 'title': 'Synthetic obligation A'},
                {'op': 'create', 'title': 'Synthetic obligation B'},
            ], 'confirm': 'Everything is complete.'},
            'agent_plan': {'actions': [
                {'op': 'agent', 'request': 'Write synthetic result A.'},
                {'op': 'agent', 'request': 'Write synthetic result B.'},
            ]},
            'reaction': {'key': 'synthetic-message-31:synthetic:3103',
                         'message_id': 'synthetic-message-31', 'emoji': 'synthetic',
                         'content': 'Synthetic reaction target',
                         'timestamp': '2031-04-12T14:00:00+00:00'},
            'future': '2031-05-12T14:00:00+00:00',
            'past': '2031-04-01T14:00:00+00:00',
            'title': 'Synthetic Schedule3 obligation',
            'reason': 'Synthetic dependency pending',
            'recurrence': 'FREQ=DAILY',
            'before_claim': ['done', 'cancelled', 'snooze', 'reschedule'],
            'bad_patches': [
                ['done', {'progress': 20}, 'ERR_BAD_PROGRESS'],
                ['done', {'end_at': None}, 'ERR_BAD_INPUT'],
                ['cancelled', {'end_at': None}, 'ERR_BAD_INPUT'],
                ['pending', {'kind': 'synthetic-unsupported'}, 'ERR_BAD_KIND'],
                ['pending', {'title': '  '}, 'ERR_BAD_INPUT'],
                ['blocked', {'block_reason': None}, 'ERR_BLOCK_REASON_REQUIRED'],
            ],
        },
        'private_remote': 'https://github.com/example-owner/private-data.git',
        'public_remote': 'https://github.com/example-owner/public-data.git',
        'visibility': {'example-owner/private-data': 'PRIVATE', 'example-owner/public-data': 'PUBLIC'},
        'message_id': 'synthetic-message-31', 'stream': 'synthetic-stream',
        'request': 'Write the synthetic result file.', 'title': 'Review synthetic fixture',
        'now': '2031-04-12T14:00:00+00:00',
        'registry': {'schema_version': 1, 'streams': {}},
        'notification_registry': {'guild_id': '3100', 'streams': {
            'synthetic-alerts': {'channel_id': '3101', 'listen': False}}},
        'channels': [{'id': '3101', 'type': 0, 'name': 'synthetic-alerts'},
                     {'id': '3102', 'type': 0, 'name': 'synthetic-work'}],
        'receipt': {'kind': 'synthetic-local', 'receipt_id': 'synthetic-delivery-31',
                    'delivered': True, 'exit_code': 0},
        'model_result': {'text': 'synthetic response', 'provider': 'synthetic-route'},
        'delivery_registry': {'streams': {'reminders': {
            'channel_id': '3101', 'webhook': 'https://discord.example/api/webhooks/synthetic'}}},
        'external_receipt': {'kind': 'discord-message', 'receipt_id': 'synthetic-message-31',
                             'delivered': True, 'exit_code': 0},
        'message': {'id': 'synthetic-message-31', 'content': 'Write the synthetic result file.',
                    'timestamp': '2031-04-12T14:00:00+00:00', 'author': {'username': 'user1'}},
        'dispatch_plan': {'actions': [{'op': 'agent', 'request': 'Write the synthetic result file.'}]},
        'dispatch_noop_plan': {'actions': [], 'confirm': 'Synthetic reply acknowledged.'},
        'failed_receipt': {'kind': 'synthetic-local', 'receipt_id': 'synthetic-failure-31',
                           'delivered': False, 'exit_code': 1},
        'adapter_text': 'print("synthetic adapter")\n',
        'invalid_capability': 'unknown-synthetic',
        'unknown_remote': 'https://github.com/example-owner/unknown-data.git',
        'user': 'SYNTHETIC\\user1',
    }



def expired_email_cases():
    """Generate the complete synthetic input and expected sets for the verifier."""
    reply = "\u9700\u56de\u590d:"
    review = "\u5f85\u67e5\u770b:"
    rows = [
        [reply + "Synthetic expired", "2031-04-12T13:59:59Z", "email-monitor", "verify:expired"],
        [reply + "Synthetic boundary", "2031-04-12T14:00:00Z", "email-monitor", "verify:boundary"],
        [reply + "Synthetic future", "2031-04-12T14:00:01Z", "email-monitor", "verify:future"],
        [review + "Synthetic retained review", "2031-04-12T13:00:00Z", "email-monitor", "verify:review"],
        [reply + "Synthetic other source", "2031-04-12T13:00:00Z", "synthetic-source", "verify:other"],
        [reply + "Synthetic undated", None, "email-monitor", "verify:undated"],
    ]
    return {"now": "2031-04-12T14:00:00Z", "rows": rows,
            "active": [rows[index][0] for index in (2, 3, 4, 5)],
            "email_active": [rows[index][0] for index in (2, 3, 5)],
            "overdue": [rows[index][0] for index in (0, 1)]}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out')
    args = parser.parse_args()
    destination = Path(args.out)/Path(FIXTURE).name if args.out else Path(__file__).resolve().parents[1]/FIXTURE
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(cases(), indent=2)+'\n', encoding='utf-8', newline='\n')
    destination.with_name('test_schedule11_queries.py').write_text(
        schedule11_regression_source(), encoding='utf-8', newline='\n')
    destination.with_name('test_runtime_defaults.py').write_text(
        runtime_defaults_regression_source(), encoding='utf-8', newline='\n')
    example_root = Path(args.out) if args.out else Path(__file__).resolve().parents[1]
    for filename, value in {
        'examples.json': public_example(),
        'db.sqlite3.example': {'format': 'SQLite', 'schema_user_version': 6,
                               'tables': ['items', 'events', 'meta', 'agent_operations',
                                          'notification_receipts', 'work_actions', 'work_action_stops'],
                               'ddl': 'skills/schedule-reminder/scripts/store.py::_DDL',
                               'action_ddl': 'skills/schedule-reminder/scripts/reminder_action_store.py::migrate'},
    }.items():
        (example_root/filename).write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8', newline='\n')



def schedule4_cases():
    """Synthetic inputs for writable aliases, lifecycle races and confirmation retries."""
    return {
        "payload": b"synthetic protected record\n",
        "replacement": "synthetic replacement",
        "pid": 4101, "process_start": 41,
        "item_id": "synthetic-order-41", "title": "Synthetic Schedule4 obligation",
        "stream": "synthetic-stream", "message": "synthetic-message-41",
        "missing_dependency": "synthetic-missing-dependency-41",
        "plan": {"actions": [{"op": "create", "title": "Synthetic Schedule4 obligation"}]},
        "created": {"id": "synthetic-created-41", "state": "pending"},
    }


def write_schedule4_record(path, payload=None):
    """Generate a singleton synthetic runtime record."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(schedule4_cases()["payload"] if payload is None else payload)
    return path


def make_schedule4_hardlink(source, alias):
    """Create a synthetic second directory entry; callers remove it before reading bytes."""
    import os
    source, alias = Path(source), Path(alias)
    if not source.exists():
        write_schedule4_record(source)
    assert source.lstat().st_nlink == 1
    before = source.read_bytes()
    alias.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, alias)
    assert source.lstat().st_nlink == alias.lstat().st_nlink == 2
    return before


def schedule4_order(workspace):
    """Generate a queued order without a process identity."""
    case = schedule4_cases()
    return {"id": case["item_id"], "title": case["title"], "state": "pending",
            "ext": {"x_agent_exec_state": "queued", "x_agent_exec_workspace": str(workspace),
                    "x_agent_exec_stream": case["stream"]}}


def schedule6_cases():
    """Synthetic command, storage and notification inputs for failure recovery."""
    return {
        "stream": "synthetic-command-stream", "channel": "6101",
        "message": {"id": "synthetic-message-61", "content": "synthetic-command payload",
                    "timestamp": "2031-06-12T10:00:00Z"},
        "registry": {"commands": {"synthetic-command": {
            "trigger": "^synthetic-command", "exec": ["synthetic-unexecuted-handler"], "timeout": 1}}},
        "title": "Synthetic Schedule6 obligation",
        "note": "Synthetic finalization outcome",
        "now": "2031-06-12T10:00:00Z",
        "later": "2031-07-12T10:00:00Z",
        "error": "synthetic handler failure",
        "unknown_ext": {"x_synthetic_preserved": "kept"},
    }


def command_handler_script(record_path, returncode=0):
    return "import json,sys\npayload = json.load(sys.stdin)\nwith open(r'%s', 'w', encoding='utf-8') as f:\n    json.dump(payload, f, ensure_ascii=False)\nsys.exit(%d)\n" % (str(record_path).replace('\\', '\\\\'), returncode)


def command_timeout_script():
    return "import time\ntime.sleep(30)\n"


def command_message(message_id, text):
    return {"id": message_id, "content": text, "timestamp": "2031-06-12T10:00:00Z",
            "author": {"bot": False, "id": "SYNTHETIC_OWNER"}}


def model_mapping_sender_script(kind):
    """Generate synthetic senders with selectable notification and reload behavior."""
    return {'NOTIFY_SENDER': '\nimport os\nSTREAM = os.environ["FAKE_STREAM"]\n\n\ndef notify(text):\n    if os.environ.get("FAKE_SILENT"):\n        return True          # claims delivery, sends nothing: the unmigrated-sender shape\n    if os.environ.get("FAKE_MUTE_VERDICT"):\n        open(os.environ["FAKE_OUTBOX"], "a", encoding="utf-8").write(text + "\\n")\n        return None          # delivered, but cannot say so\n    open(os.environ["FAKE_OUTBOX"], "a", encoding="utf-8").write(text + "\\n")\n    return True\n', 'APPLY_SENDER': '\nimport os\nSTREAM = os.environ["FAKE_STREAM"]\n\n\ndef current_map():\n    return {"opus": "Model-A"}\n\n\ndef apply_map(new_map, rationale, notifier=None, reloader=None, writer=None):\n    if os.environ.get("FAKE_NO_CHANGE"):\n        return False\n    writer(new_map)\n    if os.environ.get("FAKE_RELOAD_ALLOW_DROP"):\n        reloader(allow_drop=("synthetic-removed-tier",))\n    else:\n        reloader()\n    if not os.environ.get("FAKE_SILENT"):\n        prefix = ("cc model map updated: " if os.environ.get("FAKE_ENGLISH")\n                  else "cc ????????")\n        open(os.environ["FAKE_OUTBOX"], "a", encoding="utf-8").write(\n            prefix + rationale + "\\n")\n    return True\n'}[kind]

def public_example():
    return {"api_version": "1.0.0", "schema_version": 1, "ok": True,
            "item": {"id": "00000000-0000-7000-8000-000000000061", "kind": "task",
                     "title": "Synthetic example obligation", "state": "pending", "progress": 0,
                     "due_at": "2031-06-12T10:00:00.000000+00:00",
                     "source": "synthetic-example", "idempotency_key": "synthetic-example:61"}}




def schedule7_cases():
    """Synthetic transaction, persisted authorization and first-channel reaction cases."""
    return {
        "title": "Synthetic Schedule7 obligation",
        "blocker_title": "Synthetic Schedule7 blocker",
        "reason": "Synthetic dependency remains open",
        "now": "2031-07-12T10:00:00Z",
        "until": "2031-07-13T10:00:00Z",
        "ext": {"x_synthetic_preserved": "retained"},
        "authorized_id": "synthetic-authorized-71",
        "unshown_id": "synthetic-unshown-71",
        "work_id": "synthetic-work-71",
        "later_work_id": "synthetic-later-work-71",
        "stream": "reminders", "message_id": "7105",
        "channel": "7101", "owner": "7102", "token": "synthetic-token",
        "guild": "7100",
        "old_message": {"id": "7105", "content": "Synthetic prior instruction",
                        "author": {"id": "7102"}, "timestamp": "2031-07-12T09:00:00Z",
                        "reactions": [{"emoji": {"name": "synthetic-old", "id": None},
                                       "count": 1, "me": False}]},
        "new_reaction": {"emoji": {"name": "synthetic-new", "id": None},
                         "count": 1, "me": False},
        "new_message": {"id": "7106", "content": "Synthetic new instruction",
                        "author": {"id": "7102"}, "timestamp": "2031-07-12T10:01:00Z"},
        "failure": "Synthetic persistence failure",
        "rejected_block_states": [("pending", "done"), ("done", "done"),
                                  ("cancelled", "pending")],
        "mutation_ops": ["done", "dismiss", "snooze"],
    }


def schedule8_cases():
    """Synthetic retry, schema and transport controls for the Schedule8 repair."""
    return {
        "title": "Synthetic Schedule8 obligation",
        "failure": "Synthetic Schedule8 persistence failure",
        "work_id": "synthetic-work-81", "other_work_id": "synthetic-work-82",
        "stream": "reminders", "message_id": "8105",
        "invalid_priorities": [-1, 10, "high", None, 1.5, True],
        "valid_priorities": [0, 1, 9, "4"],
        "blank_reasons": [" ", "\t", "\n"],
        "private_url": "https://github.com/example-owner/private-data.git",
        "existing_record": "synthetic durable record",
        "bad_digest": "{synthetic broken json",
        "preserved_ext": {"x_synthetic_existing": "keep"},
    }



def schedule10_review_cases():
    """Generate complete requests with a trailing synthetic completion requirement."""
    prefix = "Perform the synthetic maintenance task.\n"
    tail = "\nRequire SYNTHETIC_TAIL_ACK in the final artifact."
    cases = []
    for length in (128, 2000, 2034, 8000):
        request = prefix + "x" * (length - len(prefix) - len(tail)) + tail
        cases.append({
            "name": "request_" + str(length),
            "request": request,
            "trailing_requirement": tail.strip(),
            "workspace": "synthetic-workspace",
            "summary": "Synthetic partial implementation.",
            "changed": ["synthetic-result.txt"],
            "changed_via": "synthetic-inspection",
            "command": "synthetic-check",
            "return_code": 0,
            "output": "Synthetic check output.",
        })
    return cases


def schedule11_query_cases():
    """Generate complete queue reads and failures that must never authorize an action."""
    def item(identity, execution="queued"):
        return {"id": identity, "title": "Synthetic work " + identity,
                "state": "doing" if execution in ("running", "stop_pending") else "pending",
                "ext": {"x_agent_exec_state": execution, "x_agent_exec_stream": "infra"}}
    first = [item("synthetic-%03d" % index) for index in range(100)]
    positives = [
        {"name": "empty", "expected": []},
        {"name": "multipage_running", "expected": first + [item("synthetic-100", "running")]},
        {"name": "multipage_stopping", "expected": first + [item("synthetic-100", "stop_pending")]},
    ]
    found = item("wo-1")
    return {"positives": positives, "found": {"item": found}, "identity": "wo-1",
            "reply": "Perform the synthetic maintenance task.",
            "plan": {"actions": []}, "stream": "infra",
            "retry_plans": {"active_ids": {"actions": [{"op": "done", "id": "wo-1"}]},
                            "get": {"actions": [{"op": "stop", "id": "wo-1"}]}}}


def schedule11_regression_source():
    return '''"""Generated queue-read regressions; all observations and actions are synthetic."""
from copy import deepcopy

import pytest

import agent_task
import agent_tick
import dispatch
import store
from make_fixtures import schedule11_query_cases

CASE = schedule11_query_cases()


def busy(*_args, **_kwargs):
    raise store.SkillError("ERR_BUSY", "synthetic busy store")


def add(item):
    store.add_item(item["title"], source=agent_task.WORK_SOURCE, state=item["state"],
                   ext=item["ext"], _id=item["id"])


def test_tick_store_failure_stops_before_claim_or_launch(monkeypatch):
    store.init_db()
    add(CASE["found"]["item"])
    monkeypatch.setattr(agent_task.store, "list_items", busy)
    effects = []
    monkeypatch.setattr(agent_task, "claim", lambda *_args, **_kwargs: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args, **_kwargs: effects.append("launch"))
    with pytest.raises(store.SkillError):
        agent_tick.run(post=False)
    assert effects == []


def _dispatch_without_effects(monkeypatch, stream, plan=None):
    effects = []
    monkeypatch.setattr(dispatch, "call_chain", lambda *_args, **_kwargs: effects.append("model"))
    monkeypatch.setattr(dispatch, "execute", lambda *_args, **_kwargs: effects.append("execute"))
    monkeypatch.setattr(dispatch, "_post", lambda *_args, **_kwargs: effects.append("confirm"))
    record = {"plan": deepcopy(plan), "outcomes": {},
              "authorized_ids": [CASE["identity"]], "authorized_work_ids": [CASE["identity"]]}
    if plan is not None:
        record.update(action_identity_version=2, action_keys=[
            dispatch._action_identity(stream, None, index, action)
            for index, action in enumerate(plan["actions"])])
    before = deepcopy(record)
    with pytest.raises((RuntimeError, store.SkillError)):
        dispatch._dispatch(stream, CASE["reply"], None, False, None, None, record,
                           lambda: effects.append("save"))
    assert effects == []
    assert record == before


@pytest.mark.parametrize("stream", ("reminders", CASE["stream"]))
def test_dispatch_census_failure_stops_before_any_planner_or_action(monkeypatch, stream):
    store.init_db()
    monkeypatch.setattr(store, "list_items", busy)
    _dispatch_without_effects(monkeypatch, stream)


@pytest.mark.parametrize("read", sorted(CASE["retry_plans"]))
def test_retry_target_read_failure_stops_before_any_action(monkeypatch, read):
    store.init_db()
    monkeypatch.setattr(store if read == "active_ids" else agent_task, read, busy)
    _dispatch_without_effects(monkeypatch, CASE["stream"], CASE["retry_plans"][read])


def test_confirmed_absence_is_distinct_and_performs_no_mutation():
    store.init_db()
    assert agent_task.get(CASE["identity"]) is None
    assert agent_task.claim(CASE["identity"]) is False
    assert agent_task.finish(CASE["identity"], True, generation=1) is False
    assert store.list_items()["items"] == []


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_empty_and_multipage_reads_remain_available(case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    result = dispatch._active_items()
    assert result == [{"id": item["id"], "title": item["title"]} for item in case["expected"]]


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_work_census_reads_every_page_in_order(case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    result = agent_task.orders()
    assert [(row["id"], row["ext"]["x_agent_exec_state"]) for row in result] == [
        (item["id"], item["ext"]["x_agent_exec_state"]) for item in case["expected"]]


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_census_retains_serial_running_and_stop_guards(monkeypatch, case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    monkeypatch.setattr(agent_tick, "reap", lambda *_args, **_kwargs: [])
    effects = []
    monkeypatch.setattr(agent_task, "claim", lambda *_args, **_kwargs: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args, **_kwargs: effects.append("launch"))
    result = agent_tick.run(post=False)
    assert result["launched"] is None
    assert result["running"] == (0 if case["name"] == "empty" else 1)
    assert effects == []


def test_successful_get_keeps_normal_claim_path():
    store.init_db()
    add(CASE["found"]["item"])
    assert agent_task.get(CASE["identity"])["ext"] == CASE["found"]["item"]["ext"]
    assert agent_task.claim(CASE["identity"]) is True
    assert store.get_item(CASE["identity"])["state"] == "doing"
    assert agent_task.operation(CASE["identity"])["generation"] == 1
'''


def schedule12_review_cases():
    """Generate synthetic inputs for the five final consumer review findings."""
    command = {"trigger": "^synthetic-command", "exec": ["python", "synthetic.py"], "timeout": "3"}
    bad_commands = [
        {**command, "trigger": value} for value in (42, [], "[")
    ] + [{**command, "exec": value} for value in (42, "python", [], [42], [""])] + [
        {**command, "timeout": value} for value in ("invalid", True, 1.5, 0, -1, [])
    ]
    summary = "Synthetic task requires a human observation; no single command can verify it."
    failing = "Write-Output '{'; exit 1"
    nested = {"verify": None, "summary": "Synthetic nested example with } { and escaped \"quotes\"."}
    malformed_outer = json.dumps({"verify": "BROKEN", "summary": "Synthetic malformed outer",
                                 "detail": nested}).replace('"BROKEN"', 'BROKEN')
    final = {"verify": failing, "summary": "Synthetic final contract", "detail": nested}
    return {
        "title": "Synthetic Schedule12 obligation", "request": "Inspect the synthetic result.",
        "bad_progress": [True, False, 1.5, -0.2, 1.0, "1.5", [], {}],
        "good_progress": [0, 42, 100, "0", "42", "100"],
        "blank": ["", " ", "\n" * 2101, "\t\r\n "],
        "text": "Synthetic delivery body.", "stream": "synthetic-stream",
        "registry": {"streams": {"synthetic-stream": {"webhook": "https://discord.example/api/webhooks/synthetic"}}},
        "receipt": {"id": "synthetic-message-121"},
        "bad_commands": bad_commands, "good_command": command,
        "command_name": "synthetic-good", "bad_name": "synthetic-bad",
        "command_text": "synthetic-command payload",
        "failing_verify": failing,
        "tails": [{"verify": value, "summary": "Synthetic quoted text: } { \\\""}
                  for value in (failing, "Write-Output '}'; exit 1", 'echo "{\\\"nested\\\":1}"')] + [final],
        "bad_tails": ["", "Synthetic completion without a contract.", "{", '{"verify":',
                      json.dumps({"summary": summary}),
                      *[json.dumps({"verify": value, "summary": summary})
                        for value in ("", " ", 42, True, [], {})],
                      json.dumps({"verify": None}),
                      json.dumps({"verify": None, "summary": 42}),
                      json.dumps({"verify": "exit 0", "summary": summary}) + '\n{"verify":',
                      malformed_outer, malformed_outer[:-1],
                      json.dumps(nested) + '\n' + malformed_outer],
        "recovered_tail": {"text": malformed_outer + '\n' + json.dumps(final), "expected": final},
        "null_tail": json.dumps({"verify": None, "summary": summary}),
        "unsafe_names": ["stream-host.txt:runtime-data", "record.txt::$DATA", "NUL", "con.txt",
                         "AUX", "PRN.log", "COM1", "LPT9.txt", "COM\u00b9", "CONIN$", "CONOUT$",
                         "record.", "record ", "directory. /record.txt"],
        "ordinary_names": ["record.txt", "pending/record.json", "COM10.txt", "console.txt"],
        "record": "Synthetic versioned runtime observation.\n",
    }


def runtime_defaults_case(root, kit, *, companion=True, guards=True):
    """Generate portable source/companion layouts without reading installed DATA."""
    import shutil
    root = Path(root)
    home, source = root/'home', root/'portable-tools/schedule-reminder'
    home.mkdir(parents=True)
    (source/'.git').mkdir(parents=True)
    (source/'.git/config').write_text(
        '[remote "origin"]\nurl = '+cases()['public_remote']+'\n', encoding='utf-8')
    if guards:
        target = source/'guards/tools/datadir.py'
        target.parent.mkdir(parents=True)
        shutil.copyfile(kit, target)
        (source/'guards/.git').write_text('gitdir: ../.git/modules/guards\n', encoding='utf-8')
    companion_root = source.parent/'schedule-reminder-config'
    def private_repository(path, *, marker=False):
        (path/'.git').mkdir(parents=True)
        (path/'.git/config').write_text(
            '[remote "origin"]\nurl = '+cases()['private_remote']+'\n', encoding='utf-8')
        if marker:
            (path/'.companion').write_text('schedule-reminder\n', encoding='utf-8')
        return path
    if companion:
        private_repository(companion_root, marker=True)
    return {'home': home, 'source': source, 'companion': companion_root,
            'private_repository': private_repository,
            'record': 'Synthetic default-path observation.\n', 'stream': 'synthetic-stream',
            'message': 'synthetic-message-defaults', 'item': {'id': 'synthetic-work-defaults', 'ext': {}},
            'digest': {'contributors': []}}


def runtime_defaults_regression_source():
    return '''"""Generated default-path regressions; all storage and observations are synthetic."""
import importlib
import json
import os
from pathlib import Path
import sys

import pytest
import agent_task
import digest
import dispatch
import inbound
import ingest
import ingest_tick
import private_data
import store
from make_fixtures import runtime_defaults_case

KIT = private_data.SOURCE / "guards/tools/datadir.py"


@pytest.fixture
def defaults(tmp_path, monkeypatch):
    case = runtime_defaults_case(tmp_path / "defaults", KIT)
    for name in list(os.environ):
        if name.startswith(("SCHEDULE_", "AGENT_CENTER_", "AGENT_EXEC_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(case["home"]))
    monkeypatch.setenv("USERPROFILE", str(case["home"]))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: case["home"]))
    monkeypatch.setattr(private_data, "SOURCE", case["source"])
    for owner, attribute in ((ingest, "_STATE_DIR"), (ingest_tick, "_LOG")):
        monkeypatch.setattr(owner, attribute, None)
    return case


def test_initialized_sibling_precedes_legacy_home_companion(defaults):
    defaults["private_repository"](defaults["home"] / ".schedule-reminder-config")
    assert private_data.config_root() == defaults["companion"]
    assert private_data.data_dir() == defaults["companion"] / "data"
    assert private_data.registry_path() == defaults["companion"] / "registry.json"


def test_legacy_home_companion_is_never_discovered(defaults):
    # A private repository at ~/.schedule-reminder-config is not a companion location: with no
    # selector and no sibling, the answer is the uncreated sibling path, never the home dotfile.
    (defaults["companion"] / ".companion").unlink()
    (defaults["companion"] / ".git/config").unlink()
    (defaults["companion"] / ".git").rmdir()
    defaults["companion"].rmdir()
    legacy = defaults["private_repository"](defaults["home"] / ".schedule-reminder-config")
    assert private_data.config_root() == defaults["companion"]
    assert private_data.config_root() != legacy
    assert not defaults["companion"].exists()


def test_unproven_sibling_refuses_when_no_companion_is_initialized(defaults):
    (defaults["companion"] / ".companion").unlink()
    before = set(defaults["companion"].rglob("*"))
    with pytest.raises(ValueError, match="companion|Companion"):
        private_data.config_root()
    assert set(defaults["companion"].rglob("*")) == before


def test_missing_companion_reads_empty_and_writes_fail_before_creation(defaults):
    (defaults["companion"] / ".companion").unlink()
    (defaults["companion"] / ".git/config").unlink()
    (defaults["companion"] / ".git").rmdir()
    defaults["companion"].rmdir()
    missing = defaults["companion"]
    assert private_data.config_root() == missing
    assert store.list_items()["items"] == []
    assert ingest.load_registry() == {}
    with pytest.raises(ValueError):
        private_data.prepare_parent(missing / "state/record.json")
    assert not missing.exists()


@pytest.mark.parametrize("dependency", ("missing", "unsupported"))
def test_missing_guards_fails_default_discovery_explicitly(defaults, dependency):
    path = defaults["source"] / "guards/tools/datadir.py"
    if dependency == "missing":
        path.unlink()
    else:
        path.write_text("# Synthetic unsupported dependency.\\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Guards.*dependency|dependency.*Guards"):
        private_data.config_root()


@pytest.mark.parametrize("variable", ("SCHEDULE_REMINDER_CONFIG", "AGENT_CENTER_CONFIG"))
@pytest.mark.parametrize("json_file", (False, True))
def test_explicit_config_directory_and_registry_selection(defaults, monkeypatch, variable, json_file):
    selected = defaults["private_repository"](defaults["home"] / "selected")
    value = selected / "selected-registry.json" if json_file else selected
    monkeypatch.setenv(variable, str(value))
    assert private_data.config_root() == selected
    expected = value if variable == "AGENT_CENTER_CONFIG" else selected / "registry.json"
    assert private_data.registry_path() == expected


def test_config_and_registry_pointers_keep_independent_precedence(defaults, monkeypatch):
    selected = defaults["private_repository"](defaults["home"] / "selected-config")
    registry = defaults["home"] / "selected-registry.json"
    monkeypatch.setenv("SCHEDULE_REMINDER_CONFIG", str(selected))
    monkeypatch.setenv("AGENT_CENTER_CONFIG", str(registry))
    assert private_data.config_root() == selected
    assert private_data.registry_path() == registry


@pytest.mark.parametrize("override", (False, True))
def test_default_namespaces_and_writers_use_the_selected_roots(defaults, monkeypatch, override):
    root = defaults["companion"]
    data = defaults["private_repository"](defaults["home"] / "selected-data") if override else root / "data"
    if override:
        monkeypatch.setenv("SCHEDULE_REMINDER_DATA_DIR", str(data))
    environment = dict(os.environ)
    assert private_data.config_root() == root
    assert dict(os.environ) == environment
    assert private_data.data_dir() == data
    assert Path(store.default_db_path()) == data / "db.sqlite3"
    assert Path(agent_task.runs_root()) == root / "agent-runs"
    assert Path(digest._path()) == root / "digest.json"
    assert Path(ingest.state_dir()) == root / "state"
    assert inbound.root() == data / "state"
    assert inbound.root(ingest.state_dir()) == root / "state"
    digest._save(defaults["digest"])
    ingest_tick._log(defaults["record"])
    agent_task.append_event(defaults["item"], "synthetic-default")
    with inbound.dispatch_record(defaults["stream"], defaults["message"]) as (record, save):
        save()
    assert json.loads((root / "digest.json").read_text(encoding="utf-8")) == defaults["digest"]
    assert defaults["record"].strip() in (root / "state/ingest_tick.log").read_text(encoding="utf-8")
    assert (root / "agent-runs" / defaults["item"]["id"] / "events.jsonl").is_file()
    assert (data / "state/dispatch" / (inbound.identity(defaults["stream"], defaults["message"]) + ".json")).is_file()
    assert not (data / "digest.json").exists()


def test_cli_dispatches_the_given_reply_without_model_or_send(defaults, monkeypatch):
    calls = []
    monkeypatch.setattr(dispatch, "dispatch", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "--stream", defaults["stream"],
                                      "--reply", defaults["record"], "--no-post"])
    assert dispatch.main() == 0
    assert calls[0][0] == (defaults["stream"], defaults["record"])
    assert calls[0][1]["post"] is False


def test_reload_of_runtime_modules_is_inert(defaults, monkeypatch):
    effects = []
    def blocked(*args, **kwargs):
        effects.append(True)
        raise AssertionError("runtime import attempted storage or transport")
    with monkeypatch.context() as guard:
        guard.setattr(private_data, "config_root", blocked)
        guard.setattr(private_data, "data_dir", blocked)
        guard.setattr(private_data, "prepare_parent", blocked)
        guard.setattr(private_data, "open_for_write", blocked)
        for module in (agent_task, digest, dispatch, inbound, ingest, ingest_tick):
            importlib.reload(module)
    assert effects == []
'''


def model_mapping_node_sender_files():
    """Synthetic Node sender modules for the offline route verifier tests."""
    return {
        "gateway.js": "exports.loadEnv = () => ({ env: {} });\n",
        "model-refresh.js": """const fs = require('node:fs');
exports.settings = () => ({ stream: process.env.FAKE_STREAM });
exports.notify = (text) => {
  if (process.env.FAKE_REFUSE) return false;
  fs.appendFileSync(process.env.FAKE_OUTBOX, text + '\\n', 'utf8');
  return true;
};
""",
    }


if __name__ == '__main__':
    main()


def configuration_registry_cases():
    """Synthetic native registry inputs for configuration-only validation."""
    return {
        "unknown_fields": {"streams": {}, "future_extension": {"keep": True}},
        "bot_destination": {"streams": {"example": {"channel_id": "10001"}},
                            "reader": {"bot_token": "synthetic"}},
        "guild_id": "10002",
        "owner_id": "10003",
    }
