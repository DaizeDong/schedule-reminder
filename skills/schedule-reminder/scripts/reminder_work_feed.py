"""Owner-side, versioned work projection. Existing database reads only; no store import.

This is an observation contract, not an execution, liveness or approval authority.
"""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3


SIGNAL_SOURCES = frozenset(("email-monitor", "daily-hotspots", "demand-mining", "task-health"))
ITEM_FIELDS = ("id", "title", "state", "source", "description", "priority", "progress",
               "due_at", "scheduled_at", "project", "created_at", "updated_at", "ext")


def source_role(source):
    if source == "agent-center:work":
        return "agent_work"
    return "signal" if source in SIGNAL_SOURCES else "tracked_item"


def _text(value, limit=1000):
    return value[:limit] if isinstance(value, str) else None


def _columns(conn, table):
    # Table names are fixed in this module, never consumer input.
    return {row[1] for row in conn.execute("PRAGMA table_info(" + table + ")")}


def read_work_feed(*, db_path=None, limit=5000, event_limit=250):
    observed = datetime.now(timezone.utc).isoformat()
    base = {"schemaVersion": 1, "available": False, "observed_at": observed,
            "items": [], "events": [], "sources": [],
            "capabilities": {"decisions": {"available": False, "reason": "decision_contract_not_connected"},
                             "validation": {"available": False, "reason": "summary_only"},
                             "remediation": {"available": False, "reason": "remediation_contract_not_connected"},
                             "liveness": {"available": False, "reason": "persisted_state_only"}}}
    if type(limit) is not int or not 1 <= limit <= 10000 or type(event_limit) is not int or not 0 <= event_limit <= 1000:
        return dict(base, reason="invalid_limit")
    raw = db_path or os.environ.get("SCHEDULE_DB_PATH") or str(Path.home()/".claude"/"schedule-reminder"/"db.sqlite3")
    path = Path(raw)
    if not path.is_absolute() or not path.is_file():
        return dict(base, reason="work_database_unavailable")
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            columns = _columns(conn, "items")
            if not {"id", "title", "state", "source", "ext", "updated_at"} <= columns:
                return dict(base, reason="unsupported_work_schema")
            fields = ",".join(name if name in columns else "NULL AS " + name for name in ITEM_FIELDS)
            totals = Counter()
            sources = []
            for source, state, count in conn.execute("SELECT source,state,count(*) FROM items GROUP BY source,state"):
                role = source_role(source)
                totals[role] += count
                sources.append({"source": source or "unattributed", "role": role, "state": state, "count": count})
            # Work cannot be crowded out by a high volume signal producer. Order stays deterministic.
            signals = ",".join("?" for _ in SIGNAL_SOURCES)
            query = ("SELECT " + fields + " FROM items ORDER BY CASE WHEN source='agent-center:work' THEN 0 "
                     "WHEN source IN (" + signals + ") THEN 2 ELSE 1 END,updated_at DESC,id LIMIT ?")
            rows = conn.execute(query, (*sorted(SIGNAL_SOURCES), limit)).fetchall()
            items, invalid = [], 0
            for row in rows:
                try:
                    ext = json.loads(row["ext"] or "{}")
                    if not isinstance(ext, dict) or not all(isinstance(row[k], str) for k in ("id", "title", "state")):
                        raise ValueError("invalid record")
                except (ValueError, TypeError):
                    invalid += 1
                    continue
                item = {key: row[key] for key in ITEM_FIELDS if key not in ("ext", "description")}
                item["title"] = _text(item["title"], 400)
                item["role"] = source_role(row["source"])
                item["summary"] = _text(ext.get("x_agent_exec_note"), 2000) if item["role"] == "agent_work" else _text(row["description"], 1000) if item["role"] == "tracked_item" else None
                item["execution"] = None
                if item["role"] == "agent_work":
                    item["execution"] = {"state": _text(ext.get("x_agent_exec_state"), 80),
                        "run_id": _text(ext.get("x_agent_exec_run_id"), 200),
                        "attempt_id": _text(ext.get("x_agent_exec_attempt_id"), 200),
                        "evidence": "summary_only"}
                # ext is deliberately not forwarded: it includes message bodies and arbitrary links.
                items.append(item)
            events, event_total = [], None
            events_available = {"seq", "ts", "item_id", "actor", "event_type", "from_state", "to_state"} <= _columns(conn, "events")
            if events_available:
                # SQL join uses only the owner-assigned item ID, never names or close timestamps.
                where = " FROM events e JOIN items i ON i.id=e.item_id WHERE i.source IS NULL OR i.source NOT IN (" + signals + ")"
                event_total = conn.execute("SELECT count(*)" + where, tuple(sorted(SIGNAL_SOURCES))).fetchone()[0]
                query = "SELECT e.seq,e.ts,e.item_id,e.actor,e.event_type,e.from_state,e.to_state,i.title" + where + " ORDER BY e.seq DESC LIMIT ?"
                events = [dict(row) for row in conn.execute(query, (*sorted(SIGNAL_SOURCES), event_limit))]
            total = sum(totals.values())
            return dict(base, available=True, items=items, events=events, sources=sources,
                        coverage={"total": total, "returned": len(items), "omitted": total - len(rows), "invalid": invalid,
                                  "roles": dict(totals), "events_available": events_available,
                                  "event_total": event_total, "events_returned": len(events),
                                  "missing_fields": sorted(set(ITEM_FIELDS) - columns),
                                  "state_basis": "persisted", "task_links": "use_reviewed_linkage_contract"})
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError):
        return dict(base, reason="work_source_read_failed")
