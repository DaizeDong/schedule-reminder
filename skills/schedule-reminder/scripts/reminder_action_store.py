"""Durable console request receipts inside the reminder owner's database."""
import json
from pathlib import Path
import re

SCHEMA_VERSION = 5
ACTIVE = frozenset(('preparing', 'queued', 'running', 'dispatching', 'reconcile'))


class ActionError(ValueError):
    def __init__(self, code, message=None):
        self.code = code
        super().__init__(message or code)


def migrate(connection):
    connection.execute('''
        CREATE TABLE IF NOT EXISTS work_actions(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL,
            item_id TEXT NOT NULL,
            action_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            target_id TEXT,
            work_item_id TEXT,
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            result TEXT
        )
    ''')
    connection.execute('CREATE INDEX IF NOT EXISTS work_actions_item ON work_actions(item_id,seq)')
    connection.execute('''CREATE TABLE IF NOT EXISTS work_action_stops(
        request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, action_id TEXT NOT NULL)''')


def connect(db_path, *, write=False):
    import store
    path = Path(db_path)
    if not path.is_absolute() or not path.is_file():
        raise ActionError('action_database_unavailable')
    try:
        connection = store.admitted_connection(path, readonly=not write)
    except store.SkillError as exc:
        raise ActionError('action_database_unavailable') from exc
    if not write:
        connection.execute('PRAGMA query_only=ON')
    if not ready(connection):
        connection.close()
        raise ActionError('action_schema_upgrade_required')
    return connection


def ready(connection):
    return (connection.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA_VERSION
            and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='work_actions'").fetchone() is not None)


def latest(connection, item_id):
    row = connection.execute('SELECT * FROM work_actions WHERE item_id=? ORDER BY seq DESC LIMIT 1', (item_id,)).fetchone()
    return dict(row) if row else None


def by_id(connection, action_id):
    row = connection.execute('SELECT * FROM work_actions WHERE id=?', (action_id,)).fetchone()
    return dict(row) if row else None


def task_acknowledged(result, expected_task, target_id):
    """Validate the controller handoff, including receipts written by older owners."""
    if (not isinstance(target_id, str) or not target_id or not isinstance(expected_task, dict)
            or expected_task.get('task_id') != target_id
            or not isinstance(expected_task.get('name'), str)
            or result.get('name') != expected_task['name']
            or not isinstance(expected_task.get('review_revision'), str)
            or re.fullmatch(r'sha256:[0-9a-f]{64}', expected_task['review_revision']) is None):
        return False
    controller_reply = (type(result.get('schemaVersion')) is int and result['schemaVersion'] == 1
                        and result.get('verb') == 'run'
                        and isinstance(result.get('name'), str) and bool(result['name'].strip())
                        and result.get('before') in ('Ready', 'Running', 'Disabled')
                        and result.get('after') in ('Ready', 'Running', 'Disabled')
                        and 'payload_success' in result and result['payload_success'] is None)
    return (result.get('ok') is True and controller_reply and
            ((result.get('status') == 'run_requested' and result.get('acknowledged') is True)
             or (result.get('status') == 'already_running' and result.get('acknowledged') is False
                 and result['before'] == result['after'] == 'Running')))


def public(connection, row):
    if row is None:
        return None
    state, summary, generation = row['state'], None, None
    result = json.loads(row['result']) if row.get('result') else {}
    if (row['kind'] == 'task' and state == 'task_requested'
            and not task_acknowledged(result, result.get('expected_task'), row['target_id'])):
        state, summary = 'reconcile', '关联任务的执行状态尚未确认'
    if row['kind'] == 'agent' and row.get('work_item_id'):
        work = connection.execute('SELECT state,ext FROM items WHERE id=?', (row['work_item_id'],)).fetchone()
        if work and state == 'stopped' and work['state'] in ('pending', 'doing', 'blocked'):
            execution = json.loads(work['ext'] or '{}').get('x_agent_exec_state')
            if execution in ('queued', 'running', 'stop_pending', 'reconcile'):
                state, summary = 'reconcile', '停止请求已记录，执行状态尚未确认'
        if work and state not in ('failed', 'stopped', 'reconcile'):
            ext = json.loads(work['ext'] or '{}')
            state = 'stopped' if work['state'] == 'cancelled' else ext.get('x_agent_exec_state', state)
            summary = ext.get('x_agent_exec_note')
        operation = connection.execute('SELECT generation,cleanup_state,released_at FROM agent_operations WHERE item_id=? '
                                       'ORDER BY generation DESC LIMIT 1',
                                       (row['work_item_id'],)).fetchone()
        generation = operation['generation'] if operation else 0
        if (state == 'reconcile' and work and work['state'] == 'cancelled'
                and (not operation or operation['released_at'] is not None)):
            state, summary = 'stopped', '已停止'
        if operation and operation['released_at'] is None and state not in ('preparing', 'queued', 'running'):
            state = 'reconcile'
            summary = '处理已结束，正在确认执行进程是否退出'
    return {'id': row['id'], 'kind': row['kind'], 'state': state,
            'work_item_id': row.get('work_item_id'), 'task_id': row.get('target_id'),
            'generation': generation,
            'summary': str(summary or result.get('message') or '')[:1500],
            'created_at': row['created_at'], 'updated_at': row['updated_at']}


def update(db_path, action_id, state, result=None, *, expected=None):
    import store
    connection = connect(db_path, write=True)
    try:
        with store._Tx(connection):
            row = by_id(connection, action_id)
            if row is None:
                raise ActionError('action_not_found')
            if expected is not None and row['state'] not in expected:
                raise ActionError('action_state_changed')
            connection.execute('UPDATE work_actions SET state=?,updated_at=?,result=? WHERE id=?',
                               (state, store.to_rfc3339(store.now_utc()),
                                json.dumps(result or {}, ensure_ascii=False), action_id))
            store._append_event(connection, row['item_id'], 'task-console', 'action_update',
                                payload={'action_id': action_id, 'state': state})
            return public(connection, by_id(connection, action_id))
    finally:
        connection.close()
