"""Owner-side, versioned work projection. Admitted existing database reads only.

This is an observation contract, not an execution, liveness or approval authority.
"""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from creation_guard import SIGNAL_SOURCES, WATCHDOG_KEYS, source_role


ITEM_FIELDS = ("id", "title", "state", "source", "kind", "description", "priority", "progress",
               "due_at", "scheduled_at", "project", "created_at", "updated_at", "ext", "idempotency_key")


def _text(value, limit=1000):
    return value[:limit] if isinstance(value, str) else None


def _columns(conn, table):
    # Table names are fixed in this module, never consumer input.
    return {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}


def _signal_clause(columns, prefix=''):
    """SQL counterpart of source_role for ordering and event coverage."""
    source = "COALESCE(" + prefix + "source,'')"
    kind = "COALESCE(" + prefix + "kind,'')" if 'kind' in columns else "''"
    key = "COALESCE(" + prefix + "idempotency_key,'')" if 'idempotency_key' in columns else "''"
    placeholders = ','.join('?' for _ in SIGNAL_SOURCES)
    watchdogs = ','.join('?' for _ in WATCHDOG_KEYS)
    clause = ("((" + source + " IN (" + placeholders + ") AND NOT (" + source +
              "='email-monitor' AND " + kind + "='task')) OR (" + source +
              "='market-intel' AND " + key + " IN (" + watchdogs + ")))")
    return clause, (*sorted(SIGNAL_SOURCES), *sorted(WATCHDOG_KEYS))


def _queue_projection(conn):
    fields = ('item_id', 'outcome', 'cleanup_state')
    if not {*fields, 'released_at'} <= _columns(conn, 'agent_operations'):
        return {'available': False, 'reason': 'queue_observation_unavailable', 'reservations': []}
    reservations = [dict(row) for row in conn.execute(
        'SELECT item_id,outcome,cleanup_state FROM agent_operations WHERE released_at IS NULL')]
    for row in conn.execute("SELECT id,state,ext FROM items WHERE source='agent-center:work' "
                            "AND id NOT IN (SELECT item_id FROM agent_operations)"):
        ext = json.loads(row['ext'] or '{}')
        if not isinstance(ext, dict):
            return {'available': False, 'reason': 'queue_observation_unavailable', 'reservations': []}
        if row['state'] == 'doing' or (row['state'] in ('pending', 'blocked') and
                ext.get('x_agent_exec_state') in ('running', 'stop_pending', 'reconcile')):
            reservations.append({'item_id': row['id'], 'outcome': None, 'cleanup_state': 'unknown'})
    blocked = any(row['outcome'] is not None and row['cleanup_state'] != 'quiescent' for row in reservations)
    return {'available': True, 'state': 'blocked' if blocked else 'busy' if reservations else 'ready',
            'reservations': reservations}


def read_work_feed(*, db_path=None, limit=5000, event_limit=250):
    import store
    observed = datetime.now(timezone.utc).isoformat()
    base = {"schemaVersion": 1, "available": False, "observed_at": observed,
            "items": [], "events": [], "sources": [],
            "capabilities": {"decisions": {"available": False, "reason": "decision_contract_not_connected"},
                             "validation": {"available": False, "reason": "summary_only"},
                             "remediation": {"available": False, "reason": "remediation_contract_not_connected"},
                             "liveness": {"available": False, "reason": "persisted_state_only"}}}
    if type(limit) is not int or not 1 <= limit <= 10000 or type(event_limit) is not int or not 0 <= event_limit <= 1000:
        return dict(base, reason="invalid_limit")
    raw = db_path or store.default_db_path()
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return dict(base, reason="work_database_unavailable")
    try:
        conn = store.admitted_connection(path, readonly=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            queue = _queue_projection(conn)
            columns = _columns(conn, "items")
            if not {"id", "title", "state", "source", "ext", "updated_at"} <= columns:
                return dict(base, reason="unsupported_work_schema")
            fields = ",".join(name if name in columns else "NULL AS " + name for name in ITEM_FIELDS)
            totals = Counter()
            source_counts = Counter()
            key_field = 'idempotency_key' if 'idempotency_key' in columns else 'NULL'
            kind_field = 'kind' if 'kind' in columns else 'NULL'
            for source, state, key, kind, count in conn.execute(
                    'SELECT source,state,' + key_field + ',' + kind_field + ',count(*) FROM items GROUP BY source,state,' + key_field + ',' + kind_field):
                role = source_role(source, key, kind)
                totals[role] += count
                source_counts[(source or 'unattributed', role, state)] += count
            sources = [{'source': source, 'role': role, 'state': state, 'count': count}
                       for (source, role, state), count in source_counts.items()]
            # Work cannot be crowded out by a high volume signal producer. Order stays deterministic.
            signal_clause, signal_args = _signal_clause(columns)
            query = ("SELECT " + fields + " FROM items ORDER BY CASE WHEN source='agent-center:work' THEN 0 "
                     "WHEN " + signal_clause + " THEN 2 ELSE 1 END,updated_at DESC,id LIMIT ?")
            rows = conn.execute(query, (*signal_args, limit)).fetchall()
            items, invalid = [], 0
            for row in rows:
                try:
                    ext = json.loads(row["ext"] or "{}")
                    if not isinstance(ext, dict) or not all(isinstance(row[k], str) for k in ("id", "title", "state")):
                        raise ValueError("invalid record")
                except (ValueError, TypeError):
                    invalid += 1
                    continue
                item = {key: row[key] for key in ITEM_FIELDS if key not in ("ext", "description", "idempotency_key")}
                item["title"] = _text(item["title"], 400)
                item["role"] = source_role(row["source"], row['idempotency_key'], row['kind'])
                item["summary"] = _text(ext.get("x_agent_exec_note"), 2000) if item["role"] == "agent_work" else _text(row["description"], 1000) if item["role"] == "tracked_item" else None
                item["execution"] = None
                item['latest_mail_summary'] = _text(ext.get('x_email_monitor_latest_summary'), 1000)
                consolidation = ext.get('x_console_consolidation')
                if isinstance(consolidation, dict):
                    parent = consolidation.get('duplicate_of') or consolidation.get('group_under')
                    if isinstance(parent, str) and parent != item['id']:
                        item['group_parent_id'] = _text(parent, 200)
                if item["role"] == "agent_work":
                    item['origin_item_id'] = _text(ext.get('x_console_origin_item'), 200)
                    item["execution"] = {"state": 'stopped' if row['state'] == 'cancelled' else _text(ext.get("x_agent_exec_state"), 80),
                        "run_id": _text(ext.get("x_agent_exec_run_id"), 200),
                        "attempt_id": _text(ext.get("x_agent_exec_attempt_id"), 200),
                        "evidence": "summary_only"}
                    if item['execution']['state'] == 'queued' and queue['reservations']:
                        item['execution']['queue_reason'] = 'cleanup_unconfirmed' if queue.get('state') == 'blocked' else 'writer_busy'
                        item['execution']['blocked_by'] = [op['item_id'] for op in queue['reservations']]
                # ext is deliberately not forwarded: it includes message bodies and arbitrary links.
                if item['role'] == 'tracked_item':
                    from reminder_actions import project_item
                    raw_item = dict(row)
                    raw_item['ext'] = ext
                    item['actions'] = project_item(conn, raw_item, db_path=str(path),
                                                  workspace_root=os.environ.get('SCHEDULE_ACTION_WORKSPACE'))
                items.append(item)
            events, event_total = [], None
            events_available = {"seq", "ts", "item_id", "actor", "event_type", "from_state", "to_state"} <= _columns(conn, "events")
            if events_available:
                # SQL join uses only the owner-assigned item ID, never names or close timestamps.
                signal_clause, signal_args = _signal_clause(columns, 'i.')
                where = " FROM events e JOIN items i ON i.id=e.item_id WHERE NOT " + signal_clause
                event_total = conn.execute("SELECT count(*)" + where, signal_args).fetchone()[0]
                query = "SELECT e.seq,e.ts,e.item_id,e.actor,e.event_type,e.from_state,e.to_state,i.title" + where + " ORDER BY e.seq DESC LIMIT ?"
                events = [dict(row) for row in conn.execute(query, (*signal_args, event_limit))]
            total = sum(totals.values())
            return dict(base, available=True, items=items, events=events, sources=sources, queue=queue,
                        coverage={"total": total, "returned": len(items), "omitted": total - len(rows), "invalid": invalid,
                                  "roles": dict(totals), "events_available": events_available,
                                  "event_total": event_total, "events_returned": len(events),
                                  "missing_fields": sorted(set(ITEM_FIELDS) - columns),
                                  "state_basis": "persisted", "task_links": "use_reviewed_linkage_contract"})
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, store.SkillError):
        return dict(base, reason="work_source_read_failed")
