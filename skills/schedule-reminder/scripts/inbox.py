"""Reminder inbox: a secondary machine queues add/update/done/snooze as intents; the primary applies them.

A secondary machine never writes the reminder database. Instead each of those four writes becomes
one intent file in the PRIVATE companion, inbox/<machine id>/<uuid7>.json, committed alone and
pushed. When the push fails (offline) the commit stays local; the next queued change pushes it, and so
does the secondary's nightly companion sync (repos.json secondaryPaths inbox/{machine}/), whichever
comes first.

The primary's tick runs drain() first: it fetches the companion and reads every intent from the
upstream branch with `git show`, so the primary's working tree is never touched. Each applied
intent is recorded in the meta table as inbox:<uuid> and skipped afterwards; meta is part of the
text export, so a handover keeps that record. An add carries an idempotency key (the caller's, or
inbox:<uuid>) and the intent's uuid as the item id, so a crash between applying and recording
replays into the same item. An intent the primary refuses (unknown item, bad field) is recorded
with its error and announced once through the reminders stream, so a queued change never fails silently.
"""
import json
import os
import subprocess

import machine_role
import private_data

OPS = ('add', 'update', 'done', 'snooze')
_PREFIX = 'inbox:'
# The tick runs under pythonw; without CREATE_NO_WINDOW every git call pops a console window.
_NOWINDOW = {'creationflags': 0x08000000} if os.name == 'nt' else {}


def _git(root, *args, timeout=120):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout, **_NOWINDOW)


def submit(op, args):
    """Write one intent on the secondary machine, commit only that file, pull --rebase, push."""
    import store
    if op not in OPS:
        raise store.SkillError('ERR_SECONDARY', machine_role.MESSAGE)
    root = private_data.config_root()
    machine = machine_role.machine_id()
    uid = store.uuid7()
    if op == 'add':
        args = dict(args, _id=uid, idempotency_key=args.get('idempotency_key') or _PREFIX + uid)
    rel = 'inbox/%s/%s.json' % (machine, uid)
    intent = {'id': uid, 'op': op, 'machine': machine,
              'created_at': store.to_rfc3339(store.now_utc()), 'args': args}
    path = root / rel
    with private_data.open_for_write(path, 'x', encoding='utf-8', newline='\n') as fh:
        json.dump(intent, fh, ensure_ascii=False, sort_keys=True, indent=1, default=str)
        fh.write('\n')
    add = _git(root, 'add', '--', rel)
    commit = add if add.returncode else _git(root, 'commit', '-m', 'Queue reminder %s from %s' % (op, machine), '--', rel)
    if commit.returncode:
        _git(root, 'reset', '-q', '--', rel)
        os.remove(path)
        raise store.SkillError('ERR_INBOX', 'could not commit the inbox intent: %s'
                               % (commit.stderr or commit.stdout).strip()[-300:])
    pull = _git(root, 'pull', '--rebase', '--autostash')
    if pull.returncode and os.path.isdir(os.path.join(root, '.git', 'rebase-merge')):
        _git(root, 'rebase', '--abort')
    delivered = pull.returncode == 0 and _git(root, 'push').returncode == 0
    message = ('queued for the primary machine; it applies the change within one tick (5 minutes)'
               if delivered else
               'saved in the inbox on this machine but not pushed (offline?); it reaches the primary '
               'with the next queued change or the nightly companion sync once this machine is online')
    result = {'queued': True, 'inbox_id': uid, 'op': op, 'machine': machine, 'delivered': delivered,
              'path': str(path), 'message': message}
    if op == 'add':
        result['item_id'] = uid
    return result


def _seen(store):
    path = store.default_db_path()
    if not os.path.isfile(path):
        return set()
    conn = store.admitted_connection(path, readonly=True)
    try:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE 'inbox:%'").fetchall()
    finally:
        conn.close()
    return {row[0][len(_PREFIX):] for row in rows}


def _mark(store, uid, record):
    conn = store._connect()
    try:
        with store._Tx(conn):
            conn.execute('INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)',
                         (_PREFIX + uid, json.dumps(record, ensure_ascii=False, sort_keys=True)))
    finally:
        conn.close()


def _apply(store, intent):
    op, args = intent['op'], dict(intent['args'])
    actor = args.pop('actor', None) or 'inbox:' + intent['machine']
    if op == 'add':
        title = args.pop('title')
        return store.add_item(title, actor=actor, **args)
    item_id = args.pop('id')
    if op == 'update':
        return store.update_item(item_id, actor=actor, **args)
    if op == 'done':
        return store.done(item_id, actor=actor, **args)
    if op == 'snooze':
        return store.snooze(item_id, args.pop('until'), actor=actor)
    raise store.SkillError('ERR_BAD_INPUT', 'unknown inbox op %r' % op)


def _announce(intent, uid, error):
    """Tell the owner once (durable receipt keyed by the intent uuid) that a queued change was refused."""
    import notify
    text = '[reminder inbox] a %s queued on %s was not applied: %s %s' % (
        intent.get('op'), intent.get('machine'), error.get('error_code', ''), error.get('message', ''))
    notify.notify(text.strip(), run_id=_PREFIX + uid, phase='inbox', condition='refused')


def drain(post=True):
    """Apply every unseen intent from the companion's upstream branch (primary only)."""
    import store
    machine_role.refuse_on_secondary()
    root = private_data.config_root()
    fetch = _git(root, 'fetch', '--quiet', 'origin')
    if fetch.returncode:
        raise RuntimeError('inbox fetch failed: %s' % fetch.stderr.strip()[-300:])
    upstream = _git(root, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    if upstream.returncode:
        raise RuntimeError('companion branch has no upstream')
    ref = upstream.stdout.strip()
    listing = _git(root, 'ls-tree', '-r', '--name-only', ref, '--', 'inbox/')
    if listing.returncode:
        raise RuntimeError('inbox listing failed: %s' % listing.stderr.strip()[-300:])
    names = [n for n in listing.stdout.splitlines() if n.endswith('.json')]
    seen = _seen(store)
    unseen = sorted((n for n in names if os.path.basename(n)[:-5] not in seen), key=os.path.basename)
    applied, failed = [], []
    if not post:
        return {'ref': ref, 'total': len(names), 'pending': [os.path.basename(n)[:-5] for n in unseen]}
    errors = 0
    for name in unseen:
        uid = os.path.basename(name)[:-5]
        shown = _git(root, 'show', '%s:%s' % (ref, name))
        if shown.returncode:
            errors += 1
            continue
        intent = json.loads(shown.stdout)
        record = {'op': intent.get('op'), 'machine': intent.get('machine'),
                  'applied_at': store.to_rfc3339(store.now_utc())}
        try:
            if intent.get('id') != uid:
                raise store.SkillError('ERR_BAD_INPUT', 'intent id does not match its file name')
            item = _apply(store, intent)
            record['item_id'] = item.get('id') if isinstance(item, dict) else None
            applied.append({'inbox_id': uid, 'op': intent['op'], 'item_id': record['item_id']})
        except store.SkillError as error:
            record['error'] = error.to_dict()
            failed.append({'inbox_id': uid, 'op': intent.get('op'), 'error': error.error_code})
        except Exception:
            errors += 1  # transient: left unseen and retried on the next tick
            continue
        _mark(store, uid, record)
        if 'error' in record:
            _announce(intent, uid, record['error'])
    if errors:
        raise RuntimeError('%d inbox intents could not be read or applied; retried next tick' % errors)
    return {'ref': ref, 'total': len(names), 'applied': applied, 'failed': failed}
