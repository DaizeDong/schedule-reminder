"""Deterministic synthetic inputs for capability and DATA-boundary tests."""
import argparse
import json
from pathlib import Path

FIXTURE = 'skills/schedule-reminder/tests/capability_cases.json'


def cases():
    return {
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out')
    args = parser.parse_args()
    destination = Path(args.out)/Path(FIXTURE).name if args.out else Path(__file__).resolve().parents[1]/FIXTURE
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(cases(), indent=2)+'\n', encoding='utf-8', newline='\n')
    example_root = Path(args.out) if args.out else Path(__file__).resolve().parents[1]
    for filename, value in {
        'readiness.json.example': {'schema_version': 1, 'tasks': {}},
        'db.sqlite3.example': {'format': 'SQLite', 'schema_user_version': 1,
                               'tables': ['items', 'events', 'meta'], 'ddl': 'skills/schedule-reminder/scripts/store.py::_DDL'},
    }.items():
        (example_root/filename).write_text(json.dumps(value, indent=2)+'\n', encoding='utf-8', newline='\n')


if __name__ == '__main__':
    main()
