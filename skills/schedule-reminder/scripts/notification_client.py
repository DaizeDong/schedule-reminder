"""Thin owner consumer API. All claims, reconciliation and delivery live in the producer.

Install beside notification_receipts.py. Consumers may bind this file explicitly through
SCHEDULE_NOTIFICATION_CLIENT; missing binding never selects another transport.
"""
from pathlib import Path
import os
import sys


def submit(owner, run_id, phase, condition, stream, content, *, dry_run=False,
           command=None, retry_failed=False, **options):
    """Return a receipt (or an explicitly unpersisted refusal/dry-run result)."""
    if dry_run or os.environ.get('AGENT_CENTER_RELAY_DRYRUN'):
        return {'state': 'dry-run', 'persisted': False}
    run_id = run_id or os.environ.get('TASK_RUN_ID') or os.environ.get('SCHEDULE_RUN_ID')
    if not run_id:
        return {'state': 'refused', 'persisted': False, 'error': 'stable_run_id_required'}
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from notification_receipts import Event, deliver
    try:
        return deliver(Event(owner, str(run_id), phase, condition, stream), content,
                       command=command, retry_failed=retry_failed, **options)
    except Exception as exc:
        # No fabricated pending/failed state: a missing DB is a consumer refusal, not a receipt.
        return {'state': 'refused', 'persisted': False, 'error': type(exc).__name__}


def detail(receipt):
    return 'notification %s%s%s' % (receipt['state'],
        ': ' + receipt['error'] if receipt.get('error') else '',
        ' [' + receipt['event_id'] + ']' if receipt.get('event_id') else '')


def command_policy(argv, *, payload='text', timeout=30, cwd=None):
    """Explicit standalone policy; deliberately does not infer a stream from argv."""
    result = {'argv': [os.fspath(a) for a in argv], 'payload': payload, 'timeout': timeout}
    if cwd is not None:
        result['cwd'] = os.fspath(cwd)
    return result


def transport_options(argv, stream, *, payload='text', timeout=30):
    """Recognize only this installed relay; explicit other scripts stay explicit targets."""
    expected = [sys.executable, str(Path(__file__).with_name('relay.py')),
                'send', '--stream', stream, '--text-b64' if payload == 'base64' else '--text']
    if len(argv) == len(expected) and all(
            (Path(a).resolve() == Path(b).resolve()) if i < 2 else a == b
            for i, (a, b) in enumerate(zip(argv, expected))):
        return {'fallback': 'big_brother'}
    return {'command': command_policy(argv, payload=payload, timeout=timeout)}
