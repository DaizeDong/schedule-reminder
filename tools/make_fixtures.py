"""Deterministic synthetic example inputs."""
import argparse
import json
from pathlib import Path


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
