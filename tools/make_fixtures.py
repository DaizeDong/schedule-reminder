"""Deterministic synthetic example inputs and verifier cases."""
import argparse
import json
from pathlib import Path


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


def public_example():
    return {"api_version": "1.0.0", "schema_version": 1, "ok": True,
            "item": {"id": "00000000-0000-7000-8000-000000000061", "kind": "task",
                     "title": "Synthetic example obligation", "state": "pending", "progress": 0,
                     "due_at": "2031-06-12T10:00:00.000000+00:00",
                     "source": "synthetic-example", "idempotency_key": "synthetic-example:61"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out')
    args = parser.parse_args()
    example_root = Path(args.out) if args.out else Path(__file__).resolve().parents[1]
    example_root.mkdir(parents=True, exist_ok=True)
    for filename, value in {
        'examples.json': public_example(),
        'db.sqlite3.example': {'format': 'SQLite', 'schema_user_version': 6,
                               'tables': ['items', 'events', 'meta', 'agent_operations',
                                          'notification_receipts', 'work_actions', 'work_action_stops'],
                               'ddl': 'skills/schedule-reminder/scripts/store.py::_DDL',
                               'action_ddl': 'skills/schedule-reminder/scripts/reminder_action_store.py::migrate'},
    }.items():
        (example_root/filename).write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
