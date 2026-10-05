"""Owner-side creation review and transactional reuse; no model or external effects."""
from difflib import SequenceMatcher
import hashlib
import json
import re
import unicodedata

import store


SIGNAL_SOURCES = frozenset(('email-monitor', 'daily-hotspots', 'demand-mining', 'task-health'))
WATCHDOG_KEYS = frozenset(('market-intel:weekly-poll-watchdog', 'market-intel:monthly-refresh-watchdog'))


def source_role(source, key=None, kind=None):
    """Classify the producer record without loading any console or agent runtime."""
    if source == 'agent-center:work':
        return 'agent_work'
    if source == 'email-monitor' and kind == 'task':
        return 'tracked_item'
    signal = source in SIGNAL_SOURCES or source == 'market-intel' and key in WATCHDOG_KEYS
    return 'signal' if signal else 'tracked_item'


SCOPE_FIELDS = ('kind', 'due_at', 'scheduled_at', 'start_at', 'end_at', 'wait_until',
                'tz', 'recurrence', 'rdate', 'exdate', 'project', 'alarms', 'relations', 'ext')
TIME_FIELDS = ('due_at', 'scheduled_at', 'start_at', 'end_at', 'wait_until')


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def _text(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def _title(value):
    return re.sub(r'^(?:待办|提醒)\s*:\s*', '', _text(value))


def revision(item):
    return _digest(item)


def candidate(title, **fields):
    if not isinstance(title, str) or not title.strip():
        raise store.SkillError('ERR_BAD_INPUT', 'title is required')
    item = dict(fields, title=title, kind=fields.get('kind', 'task'))
    store._validate_kind(item['kind'])
    for field in TIME_FIELDS:
        if item.get(field):
            item[field] = store.to_rfc3339(store.parse_dt(item[field]))
    return item


def _scope(item):
    # Work uses the full request digest, workspace and origin, never its truncated title.
    ext = item.get('ext') or {}
    if not isinstance(ext, dict):
        raise store.SkillError('ERR_BAD_INPUT', 'creation ext must be an object')
    if item.get('source') == 'agent-center:work' and ext.get('x_agent_exec_request_identity'):
        return {'work_request': ext['x_agent_exec_request_identity']}
    return {**{field: item.get(field) or None for field in SCOPE_FIELDS},
            'record_role': source_role(item.get('source'), item.get('idempotency_key'), item.get('kind'))}


def _equivalent(left, right):
    if _scope(left) != _scope(right):
        return False
    if 'work_request' in _scope(left):
        return True
    return (_title(left.get('title')) == _title(right.get('title'))
            and _text(left.get('description')) == _text(right.get('description')))


def _occurrence(item):
    return {key: value for key, value in _scope(item).items() if key != 'ext'}


def inspect(conn, proposed):
    rows = conn.execute('SELECT * FROM items ORDER BY created_at,id').fetchall()
    matches = []
    for row in rows:
        item = store._row_to_item(row)
        active = item['state'] in store.ACTIVE_STATES
        if not active and not proposed.get('due_at'):
            continue
        if (item.get('source') == 'agent-center:work') != (proposed.get('source') == 'agent-center:work'):
            continue
        compared = dict(item)
        if not active and item['kind'] == 'task' and not proposed.get('end_at'):
            compared['end_at'] = None  # completion time is not an occurrence boundary
        if _occurrence(compared) != _occurrence(proposed):
            continue
        exact = active and _equivalent(item, proposed)
        score = SequenceMatcher(None, _title(item['title']), _title(proposed['title'])).ratio()
        if exact or score >= 0.55:
            matches.append({'item': item, 'revision': revision(item),
                            'match': 'equivalent' if exact else 'review', 'similarity': round(score, 3)})
    matches.sort(key=lambda row: (row['match'] != 'equivalent', -row['similarity'],
                                  row['item']['created_at'], row['item']['id']))
    exact = [row for row in matches if row['match'] == 'equivalent']
    return {'decision': 'reuse' if exact else 'review' if matches else 'create',
            'matches': matches, 'scanned': len(rows), 'complete': True}


def _request(proposed, options):
    source, key = proposed.get('source'), proposed.get('idempotency_key')
    if not isinstance(source, str) or not source.strip() or not isinstance(key, str) or not key.strip():
        raise store.SkillError('ERR_BAD_INPUT', 'ensure requires source and idempotency_key')
    meaningful = {field: value for field, value in proposed.items()
                  if field not in ('id', 'created_at', 'updated_at', 'schema_version', 'notified_at',
                                   'next_retry_at', 'retry_count', 'claimed_at', 'block_reason')}
    return 'creation:' + _digest([source, key]), _digest([meaningful, options])


def _receipt(conn, key):
    row = conn.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(row['value']) if row else None


def complete(conn, item, proposed, options, decision):
    key, request_digest = _request(proposed, options)
    conn.execute('INSERT INTO meta(key,value) VALUES (?,?)',
                 (key, json.dumps({'item_id': item['id'], 'request_digest': request_digest})))
    store._append_event(conn, item['id'], proposed['source'], 'creation_' + decision,
                        payload={'request_key': key, 'source': proposed['source'],
                                 'proposal': {field: proposed.get(field) for field in
                                     ('title', 'description', 'source', 'idempotency_key', 'ext',
                                      'priority', 'tags', 'project', 'due_at', 'recurrence', 'alarms')},
                                 'distinct_reason': options.get('distinct_reason')})
    return {'item': item, 'decision': decision}


def prepare(conn, proposed, options):
    key, request_digest = _request(proposed, options)
    seen = _receipt(conn, key)
    if seen:
        if seen.get('request_digest') != request_digest:
            raise store.SkillError('ERR_CONFLICT', 'creation request changed; reuse the original request')
        item = store._row_to_item(store._get_raw(conn, seen['item_id']))
        if item is None:
            raise store.SkillError('ERR_CONFLICT', 'original creation result is unavailable')
        return {'item': item, 'decision': 'replayed'}
    keyed = conn.execute('SELECT * FROM items WHERE idempotency_key=?',
                         (proposed['idempotency_key'],)).fetchone()
    if keyed is not None:
        if keyed['source'] != proposed['source']:
            raise store.SkillError('ERR_CONFLICT', 'idempotency key belongs to another source')
        if any(options.get(name) for name in ('reuse_id', 'expected_revision', 'note', 'distinct_reason')):
            raise store.SkillError('ERR_CONFLICT', 'explicit creation review requires a new request identity')
        return complete(conn, store._row_to_item(keyed), proposed, options, 'reused')
    if proposed['state'] not in store.ACTIVE_STATES:
        raise store.SkillError('ERR_BAD_INPUT', 'ensure creates active items only')
    reuse_id = options.get('reuse_id')
    if reuse_id:
        target = store._row_to_item(store._get_raw(conn, reuse_id))
        if target is None or revision(target) != options.get('expected_revision'):
            raise store.SkillError('ERR_CONFLICT', 'creation target changed; repeat the preflight')
        if target['state'] not in store.ACTIVE_STATES or _occurrence(target) != _occurrence(proposed):
            raise store.SkillError('ERR_CONFLICT', 'creation target has a different occurrence or scope')
        note = options.get('note')
        if proposed.get('description') and _text(proposed['description']) != _text(target.get('description')) and not note:
            raise store.SkillError('ERR_BAD_INPUT', 'new description content requires an append note')
        if note:
            description = ((target.get('description') or '').rstrip() + '\n\n' + note.strip()).strip()
            conn.execute('UPDATE items SET description=?,updated_at=? WHERE id=?',
                         (description, store.to_rfc3339(store.now_utc()), reuse_id))
            target = store._row_to_item(store._get_raw(conn, reuse_id))
        return complete(conn, target, proposed, options, 'merged' if note else 'reused')
    if options.get('note') or options.get('expected_revision'):
        raise store.SkillError('ERR_BAD_INPUT', 'note and expected_revision require reuse_id')
    result = inspect(conn, proposed)
    exact = [row for row in result['matches'] if row['match'] == 'equivalent']
    if exact and not options.get('distinct_reason'):
        return complete(conn, exact[0]['item'], proposed, options, 'reused')
    if result['matches'] and not options.get('distinct_reason'):
        raise store.SkillError('ERR_CREATION_REVIEW', 'creation review required before adding a similar item',
                               candidates=[{'id': row['item']['id'], 'revision': row['revision']}
                                           for row in result['matches']])
    return None
