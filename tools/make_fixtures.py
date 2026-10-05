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


def ssh_alias_publication_cases():
    """Synthetic Schedule routes with explicit URL users and expected host proofs."""
    repository = "example-owner/private-data"
    first = "git@synthetic-first:" + repository + ".git"
    second = "ssh://git@synthetic-second/" + repository + ".git"
    canonical = "git@github.com:" + repository + ".git"
    return [
        {"id": name, "fetch": fetch, "push": push, "blocked_hosts": blocked,
         "repositories": (repository, "example-owner/other-private-data"),
         "visibility": visibility, "hosts": hosts, "allowed": allowed}
        for name, fetch, push, blocked, visibility, hosts, allowed in [
            ("alias-private", [first], [first], [], "PRIVATE", ["synthetic-first"], True),
            ("aliases-private", [first], [second], [], "PRIVATE", ["synthetic-first", "synthetic-second"], True),
            ("canonical-alias", [canonical], [first], [], "PRIVATE", ["github.com", "synthetic-first"], True),
            ("alias-canonical", [first], [canonical], [], "PRIVATE", ["synthetic-first", "github.com"], True),
            ("multiple-fetch-urls", [first, second], [canonical], [], "PRIVATE",
             ["synthetic-first", "synthetic-second", "github.com"], True),
            ("second-host-hostile", [first], [second], ["synthetic-second"], "PRIVATE", ["synthetic-first", "synthetic-second"], False),
            ("hostile-first", [second], [first], ["synthetic-second"], "PRIVATE", ["synthetic-second"], False),
            ("canonical-before-hostile", [canonical], [first], ["synthetic-first"], "PRIVATE", ["github.com", "synthetic-first"], False),
            ("alias-public", [first], [second], [], "PUBLIC", ["synthetic-first", "synthetic-second"], False),
            ("alias-unknown", [first], [second], [], "UNKNOWN", ["synthetic-first", "synthetic-second"], False),
            ("wrong-url-user", [first], [second.replace("git@", "other@")], [], "PRIVATE", ["synthetic-first"], False),
            ("missing-url-user", [first], [second.replace("git@", "")], [], "PRIVATE", ["synthetic-first"], False),
            ("url-password", [first], [second.replace("git@", "git:synthetic@")], [], "PRIVATE", ["synthetic-first"], False),
            ("explicit-url-port", [first], [second.replace("synthetic-second/", "synthetic-second:22/")], [],
             "PRIVATE", ["synthetic-first"], False),
            ("url-query", [first], [second + "?synthetic"], [], "PRIVATE", ["synthetic-first"], False),
            ("unknown-transport", [first], ["file:///synthetic/no-repository"], [], "PRIVATE", ["synthetic-first"], False),
        ]
    ]


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
        'changed_config': {'schema_version': 1, 'streams': {}, 'revision': 2},
        'invalid_capability': 'unknown-synthetic',
        'unknown_remote': 'https://github.com/example-owner/unknown-data.git',
        'user': 'SYNTHETIC\\user1',
    }



def publication_environment_case(root, kind='override'):
    """Generate a private SSH repository and command-inventory redirection controls."""
    root = Path(root)
    repository = root/"repository"
    admin = repository/".git"
    (admin/"objects").mkdir(parents=True)
    (admin/"refs/heads").mkdir(parents=True)
    (admin/"HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    if kind not in {'override', 'private', 'linked-private'}:
        raise ValueError('unknown synthetic publication case')
    config = '[core]\nrepositoryformatversion = 0\nbare = false\n'
    if kind == 'override':
        config += 'sshCommand = synthetic-unexecuted-ssh\n'
    remote = ('git@github.com:example-owner/private-data.git' if kind == 'override'
              else 'https://github.com/example-owner/private-data.git')
    config += '[remote "origin"]\nurl = ' + remote + '\n'
    (admin/"config").write_text(config, encoding="utf-8", newline="\n")
    import hashlib
    index_header = b'DIRC' + (2).to_bytes(4, 'big') + bytes(4)
    index = index_header + hashlib.sha1(index_header).digest()
    (admin/"index").write_bytes(index)
    copied_index = root/"copied.index"
    copied_index.write_bytes(index)
    if kind == 'linked-private':
        linked = root/"linked"
        linked.mkdir()
        worktree = admin/"worktrees/synthetic"
        worktree.mkdir(parents=True)
        (worktree/"HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
        (worktree/"commondir").write_text("../..\n", encoding="ascii")
        (worktree/"gitdir").write_text(str(linked/".git")+"\n", encoding="utf-8")
        (linked/".git").write_text("gitdir: "+str(worktree)+"\n", encoding="utf-8")
        repository = linked
    empty = root/"empty.gitconfig"
    empty.write_text("", encoding="ascii")
    return {"repository": repository, "empty_config": empty, "copied_index": copied_index,
            "overrides": {"GIT_CONFIG": str(empty), "GIT_CONFIG_KEY_0": "core.sshCommand",
                          "GIT_CONFIG_VALUE_0": "synthetic-unexecuted-ssh",
                          "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_PARAMETERS": "'core.sshCommand=synthetic-unexecuted-ssh'"}}


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
    example_root = Path(args.out) if args.out else Path(__file__).resolve().parents[1]
    for filename, value in {
        'examples.json': public_example(),
        'readiness.json.example': {'schema_version': 1, 'tasks': {}},
        'db.sqlite3.example': {'format': 'SQLite', 'schema_user_version': 1,
                               'tables': ['items', 'events', 'meta'], 'ddl': 'skills/schedule-reminder/scripts/store.py::_DDL'},
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
        "http_config_overrides": [
            ["http.curloptresolve", "github.com:443:192.0.2.81"],
            ["http.https://github.com/.curloptresolve", "github.com:443:192.0.2.81"],
            ["http.sslverify", "false"], ["http.sslcainfo", "/synthetic/ca.pem"],
            ["http.proxy", "http://proxy.example:8080"],
        ],
        "http_environment_overrides": [
            ["GIT_SSL_NO_VERIFY", "1"], ["GIT_SSL_CAINFO", "/synthetic/ca.pem"],
            ["HTTPS_PROXY", "http://proxy.example:8080"],
        ],
        "existing_record": "synthetic durable record",
        "bad_digest": "{synthetic broken json",
        "preserved_ext": {"x_synthetic_existing": "keep"},
    }



def schedule9_selector_cases(area):
    """Generate publication selectors and inert Git/GitHub answers for author controls."""
    from pathlib import Path
    import json
    area = Path(area)
    private = "https://github.com/example-owner/private-data.git"
    public = "https://github.com/example-owner/public-data.git"
    selectors = ("remote.pushDefault", "branch.main.pushRemote", "branch.main.remote")
    cases = []

    def add(group, label, entries=(), allowed=False, remotes=None, visibility="PRIVATE"):
        index = len(cases)
        root = area / ("%03d-%s" % (index, label))
        root.mkdir(parents=True, exist_ok=True)
        configured = remotes or {"origin": private}
        config = "".join("remote.%s.url\n%s\0" % (name, url)
                         for name, url in configured.items())
        config += "".join(key + ("\n" + value if value is not None else "") + "\0"
                          for key, value in entries)
        cases.append({"id": "%03d-%s" % (index, label), "group": group,
                      "root": root, "destination": root / "pending" / "record.json",
                      "config": config, "remotes": configured, "allowed": allowed,
                      "visibility_response": json.dumps({"nameWithOwner": "example-owner/private-data",
                                                         "visibility": visibility})})

    for key in selectors + ("branch.inactive.pushRemote", "branch.inactive.remote",
                            "ReMoTe.PuShDeFaUlT", "BrAnCh.Inactive.PuShReMoTe"):
        add("raw", "raw-url", [(key, public)])
    for key in selectors:
        for label, value, allowed in (
                ("named", "origin", True), ("unknown", "missing", False),
                ("local-dot", ".", False), ("valueless", None, False),
                ("empty", "", False), ("null-literal", "null", False),
                ("case-mismatch", "ORIGIN", False), ("padded", " origin ", False)):
            add("names", label, [(key, value)], allowed)
        add("names", "exact-case-name", [(key, "Origin")], True,
            {"origin": private, "Origin": private})
        for name in ("null", "unknown"):
            add("names", "configured-literal-name", [(key, name)], True,
                {"origin": private, name: private})
        add("names", "configured-dot", [(key, ".")], False,
            {"origin": private, ".": private})
    add("absent", "unset", allowed=True)
    for entries in (
            [("branch.Main.remote", public), ("branch.main.remote", "origin")],
            [("branch.main.remote", "origin"), ("branch.Main.remote", public)],
            [("remote.pushDefault", public), ("remote.pushDefault", "origin")],
            [("branch.inactive.pushRemote", public), ("branch.main.remote", "origin")]):
        add("all_entries", "all-entries", entries)
    for visibility in ("PUBLIC", "UNKNOWN", None):
        add("visibility", "visibility", [("remote.pushDefault", "origin")],
            visibility=visibility)
    for key in selectors:
        add("writer", "writer-rejected", [(key, public)])
    return cases


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
    from copy import deepcopy
    def item(identity, execution="queued"):
        return {"id": identity, "title": "Synthetic work " + identity,
                "state": "doing" if execution in ("running", "stop_pending") else "pending",
                "ext": {"x_agent_exec_state": execution, "x_agent_exec_stream": "infra"}}
    first = [item("synthetic-%03d" % index) for index in range(100)]
    page = {"items": first, "next_cursor": first[-1]["id"]}
    empty = {"items": [], "next_cursor": None}
    bad_pages = {
        "busy": {"_err": "ERR_BUSY"},
        "error_envelope": {"error_code": "ERR_BUSY"},
        "non_object": [],
        "missing_items": {"next_cursor": None},
        "missing_boundary": {"items": []},
        "non_list_items": {"items": {}, "next_cursor": None},
        "non_object_item": {"items": [None], "next_cursor": None},
        "missing_identity": {"items": [{}], "next_cursor": None},
        "non_text_identity": {"items": [{"id": []}], "next_cursor": None},
        "empty_cursor": {"items": [], "next_cursor": ""},
        "non_text_cursor": {"items": [], "next_cursor": 1},
        "short_nonterminal_page": {"items": [item("synthetic-100")], "next_cursor": "synthetic-100"},
    }
    failures = []
    for name, bad in bad_pages.items():
        for later in (False, True):
            failures.append({"name": name + ("_later" if later else "_first"),
                             "pages": [deepcopy(page), deepcopy(bad)] if later else [deepcopy(bad)]})
    failures.append({"name": "repeated_page", "pages": [deepcopy(page), deepcopy(page)]})
    invalid_work = deepcopy(first[0])
    invalid_work["ext"] = "unreadable execution state"
    work_failures = [
        {"name": "missing_execution_state", "pages": [{"items": [invalid_work], "next_cursor": None}]},
        {"name": "missing_pool_state", "pages": [{"items": [{"id": "synthetic-100", "ext": {"x_agent_exec_state": "running"}}], "next_cursor": None}]},
    ]
    positives = [
        {"name": "empty", "pages": [empty], "expected": []},
        {"name": "multipage_running", "pages": [deepcopy(page), {"items": [item("synthetic-100", "running")], "next_cursor": None}],
         "expected": first + [item("synthetic-100", "running")]},
        {"name": "multipage_stopping", "pages": [deepcopy(page), {"items": [item("synthetic-100", "stop_pending")], "next_cursor": None}],
         "expected": first + [item("synthetic-100", "stop_pending")]},
    ]
    found = item("wo-1")
    wrong = item("synthetic-other")
    get_failures = [
        {"name": "busy", "response": {"_err": "ERR_BUSY"}},
        {"name": "error_envelope", "response": {"error_code": "ERR_BUSY"}},
        {"name": "non_object", "response": []},
        {"name": "missing_item", "response": {}},
        {"name": "null_item", "response": {"item": None}},
        {"name": "wrong_identity", "response": {"item": wrong}},
        {"name": "incomplete_state", "response": {"item": {"id": "wo-1", "state": "pending"}}},
    ]
    boundary = [deepcopy(case) for case in failures
                if case["name"] in ("busy_first", "busy_later", "missing_boundary_first", "repeated_page")]
    dispatch_failures = [{**deepcopy(case), "name": "pool_" + case["name"], "route": "pool"}
                         for case in boundary]
    boundary += deepcopy(work_failures)
    dispatch_failures += [{**deepcopy(case), "name": "work_" + case["name"], "route": "work"}
                          for case in boundary]
    return {"page_failures": failures, "work_failures": work_failures, "positives": positives,
            "boundary_failures": boundary, "dispatch_failures": dispatch_failures,
            "get_failures": get_failures, "found": {"item": found},
            "absent": {"_err": "ERR_NOT_FOUND"}, "identity": "wo-1",
            "reply": "Perform the synthetic maintenance task.",
            "plan": {"actions": []}, "stream": "infra"}


def schedule11_regression_source():
    return "\"\"\"Generated queue-read regressions; all observations and actions are synthetic.\"\"\"\nfrom copy import deepcopy\n\nimport pytest\n\nimport agent_task\nimport agent_tick\nimport dispatch\nfrom make_fixtures import schedule11_query_cases\n\nCASE = schedule11_query_cases()\n\n\ndef pages(monkeypatch, owner, attribute, responses):\n    pending = iter(deepcopy(responses))\n    calls = []\n    def query(*args):\n        calls.append(args)\n        return next(pending)\n    monkeypatch.setattr(owner, attribute, query)\n    return calls\n\n\ndef invoke(role):\n    if role == \"get\":\n        return agent_task.get(CASE[\"identity\"])\n    if role == \"claim\":\n        return agent_task.claim(CASE[\"identity\"])\n    return agent_task.finish(CASE[\"identity\"], True)\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"page_failures\"], ids=lambda case: case[\"name\"])\n@pytest.mark.parametrize(\"consumer\", (\"orders\", \"active_items\"))\ndef test_failed_page_never_returns_a_partial_census(monkeypatch, case, consumer):\n    owner = agent_task if consumer == \"orders\" else dispatch\n    calls = pages(monkeypatch, owner, \"rem\" if consumer == \"orders\" else \"_rem\", case[\"pages\"])\n    with pytest.raises(RuntimeError):\n        (agent_task.orders if consumer == \"orders\" else dispatch._active_items)()\n    assert len(calls) <= len(case[\"pages\"])\n    assert all(call[0] == \"list\" for call in calls)\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"boundary_failures\"], ids=lambda case: case[\"name\"])\n@pytest.mark.parametrize(\"second_census\", (False, True))\ndef test_tick_query_failure_stops_before_claim_or_launch(monkeypatch, case, second_census):\n    responses = deepcopy(case[\"pages\"])\n    if second_census:\n        responses.insert(0, {\"items\": [deepcopy(CASE[\"found\"][\"item\"])], \"next_cursor\": None})\n    pages(monkeypatch, agent_task, \"rem\", responses)\n    effects = []\n    reaped = []\n    monkeypatch.setattr(agent_tick, \"reap\", lambda *_args, **_kwargs: reaped.append(True) or [])\n    monkeypatch.setattr(agent_task, \"claim\", lambda *_args: effects.append(\"claim\"))\n    monkeypatch.setattr(agent_tick, \"launch\", lambda *_args: effects.append(\"launch\"))\n    with pytest.raises(RuntimeError):\n        agent_tick.run(post=False)\n    assert effects == []\n    assert len(reaped) == int(second_census)\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"dispatch_failures\"], ids=lambda case: case[\"name\"])\n@pytest.mark.parametrize(\"replay\", (False, True))\ndef test_dispatch_query_failure_stops_before_any_planner_or_action(monkeypatch, case, replay):\n    owner = dispatch if case[\"route\"] == \"pool\" else agent_task\n    pages(monkeypatch, owner, \"_rem\" if case[\"route\"] == \"pool\" else \"rem\", case[\"pages\"])\n    effects = []\n    monkeypatch.setattr(dispatch, \"call_chain\", lambda *_args, **_kwargs: effects.append(\"model\"))\n    monkeypatch.setattr(dispatch, \"execute\", lambda *_args, **_kwargs: effects.append(\"execute\"))\n    monkeypatch.setattr(dispatch, \"_post\", lambda *_args, **_kwargs: effects.append(\"confirm\"))\n    record = {\"plan\": deepcopy(CASE[\"plan\"]) if replay else None, \"outcomes\": {},\n              \"authorized_ids\": [], \"authorized_work_ids\": []}\n    before = deepcopy(record)\n    with pytest.raises(RuntimeError):\n        dispatch._dispatch(\"reminders\" if case[\"route\"] == \"pool\" else CASE[\"stream\"],\n                           CASE[\"reply\"], None, False, None, None, record,\n                           lambda: effects.append(\"save\"))\n    assert effects == []\n    assert record == before\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"get_failures\"], ids=lambda case: case[\"name\"])\n@pytest.mark.parametrize(\"role\", (\"get\", \"claim\", \"finish\"))\ndef test_failed_get_cannot_transition_patch_or_finalize(monkeypatch, case, role):\n    mutations = []\n    def query(*args):\n        if args[0] == \"get\":\n            return deepcopy(case[\"response\"])\n        mutations.append(args[0])\n        return deepcopy(CASE[\"found\"])\n    monkeypatch.setattr(agent_task, \"rem\", query)\n    monkeypatch.setattr(agent_task, \"patch_ext\", lambda *_args, **_kwargs: mutations.append(\"patch\") or {})\n    with pytest.raises(RuntimeError):\n        invoke(role)\n    assert mutations == []\n\n\n@pytest.mark.parametrize(\"role\", (\"get\", \"claim\", \"finish\"))\ndef test_confirmed_absence_is_distinct_and_performs_no_mutation(monkeypatch, role):\n    calls = pages(monkeypatch, agent_task, \"rem\", [CASE[\"absent\"]])\n    monkeypatch.setattr(agent_task, \"patch_ext\", lambda *_args, **_kwargs: pytest.fail(\"absent item cannot be patched\"))\n    result = invoke(role)\n    if role == \"get\":\n        assert result is None\n    elif role == \"claim\":\n        assert result is False\n    else:\n        assert result[\"_err\"] == \"ERR_NOT_FOUND\"\n    assert calls == [(\"get\", \"--id\", CASE[\"identity\"])]\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"positives\"], ids=lambda case: case[\"name\"])\n@pytest.mark.parametrize(\"consumer\", (\"orders\", \"active_items\"))\ndef test_complete_empty_and_multipage_reads_remain_available(monkeypatch, case, consumer):\n    owner = agent_task if consumer == \"orders\" else dispatch\n    calls = pages(monkeypatch, owner, \"rem\" if consumer == \"orders\" else \"_rem\", case[\"pages\"])\n    result = (agent_task.orders if consumer == \"orders\" else dispatch._active_items)()\n    expected = case[\"expected\"] if consumer == \"orders\" else [\n        {\"id\": item[\"id\"], \"title\": item[\"title\"]} for item in case[\"expected\"]]\n    assert result == expected\n    assert len(calls) == len(case[\"pages\"])\n\n\n@pytest.mark.parametrize(\"case\", CASE[\"positives\"], ids=lambda case: case[\"name\"])\ndef test_complete_census_retains_serial_running_and_stop_guards(monkeypatch, case):\n    pages(monkeypatch, agent_task, \"rem\", case[\"pages\"] + case[\"pages\"])\n    monkeypatch.setattr(agent_tick, \"reap\", lambda *_args, **_kwargs: [])\n    effects = []\n    monkeypatch.setattr(agent_task, \"claim\", lambda *_args: effects.append(\"claim\"))\n    monkeypatch.setattr(agent_tick, \"launch\", lambda *_args: effects.append(\"launch\"))\n    result = agent_tick.run(post=False)\n    assert result[\"launched\"] is None\n    assert result[\"running\"] == (0 if case[\"name\"] == \"empty\" else 1)\n    assert effects == []\n\n\n@pytest.mark.parametrize(\"role\", (\"get\", \"claim\", \"finish\"))\ndef test_successful_get_keeps_normal_claim_and_finish_paths(monkeypatch, role):\n    effects = []\n    def query(*args):\n        if args[0] != \"get\":\n            effects.append(args[0])\n        return deepcopy(CASE[\"found\"])\n    monkeypatch.setattr(agent_task, \"rem\", query)\n    monkeypatch.setattr(agent_task, \"patch_ext\", lambda *_args, **_kwargs: effects.append(\"patch\") or {})\n    result = invoke(role)\n    if role == \"get\":\n        assert result == CASE[\"found\"][\"item\"]\n        assert effects == []\n    elif role == \"claim\":\n        assert result is True\n        assert effects == [\"transition\", \"patch\"]\n    else:\n        assert result == CASE[\"found\"]\n        assert effects == [\"done\"]\n"


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
