"""Notification routing and delivery receipts.

Explicit SCHEDULE_RELAY_CMD takes precedence. Otherwise the configured relay
sends to SCHEDULE_RELAY_STREAM (default reminders). A missing relay script uses
the standalone Big Brother sender. Legacy boolean delivery never proves external
readiness; a downstream receipt is required for that capability.

The transport modules resolve their own credentials. This module does not log them.
"""
from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_relay_path():
    return os.environ.get("SCHEDULE_RELAY_PY", os.path.join(_HERE, "relay.py"))


def _default_stream():
    return os.environ.get("SCHEDULE_RELAY_STREAM", "reminders")


def _run(argv):
    # encoding="utf-8": text=True otherwise decodes the child's stdout with the locale codepage
    # (GBK on a zh-CN box); harmless here since only the return code is used, but keep it consistent.
    r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    return r.returncode == 0


def notify_event(event, text, **delivery_options):
    """Return the durable business-event receipt for an explicitly selected owner event."""
    from notification_receipts import deliver
    return deliver(event, text, **delivery_options)


def notify_occurrence(run_id, text, *, phase='reminder', condition='due',
                      db_path=None, retry_failed=False):
    """Opt-in occurrence bridge preserving command overrides and standalone routing."""
    import notification_client as client
    stream = _default_stream()
    command = os.environ.get('SCHEDULE_RELAY_CMD')
    if command:
        options = {'command': client.command_policy(shlex.split(command, posix=(os.name != 'nt')))}
    elif os.path.isfile(_default_relay_path()):
        options = client.transport_options([sys.executable, _default_relay_path(), 'send',
                                            '--stream', stream, '--text'], stream)
    else:
        options = {'command': client.command_policy([sys.executable, os.path.join(_HERE, 'bigbrother.py')])}
    return client.submit('schedule-reminder', run_id, phase, condition, stream, text,
                         language='preserve', db_path=db_path, retry_failed=retry_failed, **options)


def notify(text, *, run_id=None, phase='reminder', condition='due', retry_failed=False):
    """Return boolean delivery, using durable receipts when an occurrence ID is supplied.

    Text-only callers retain their established transport until their owner supplies a stable
    occurrence identity. Business-event receipts do not establish external readiness.
    """
    try:
        if run_id is not None:
            import notification_client as client
            receipt = notify_occurrence(run_id, text, phase=phase, condition=condition,
                                        retry_failed=retry_failed)
            if receipt['state'] != 'sent':
                sys.stderr.write('notify: ' + client.detail(receipt) + '\n')
            return receipt['state'] == 'sent'
        if retry_failed:
            sys.stderr.write('notify: retry requires a stable occurrence ID\n')
            return False
        cmd_env = os.environ.get("SCHEDULE_RELAY_CMD")
        if cmd_env:  # explicit override / test seam, always wins
            return _run(shlex.split(cmd_env, posix=(os.name != "nt")) + [text])

        relay_py = _default_relay_path()
        if os.path.isfile(relay_py):  # Agent Center channel (the 2026-07-01 decision)
            return _run([sys.executable, relay_py, "send",
                         "--stream", _default_stream(), "--text", text])

        # Standalone install without relay.py: deliver via the native Big Brother DM so a reminder
        # is never dropped. (Replaces the old shell-out to the legacy DM notifier script.)
        return _standalone(text)
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: %s\n" % e)
        return False


def _standalone(text):
    """Legacy fallback confirms delivery only; it produces no readiness receipt."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)
    import bigbrother
    return bool(bigbrother.send_dm(text))


def deliver(text):
    """Return a downstream receipt when available, retaining legacy bool delivery.

    A successful legacy process has no receipt and cannot establish readiness.
    """
    command = os.environ.get('SCHEDULE_RELAY_CMD')
    if not command:
        relay_path = _default_relay_path()
        if not os.path.isfile(relay_path):
            return _standalone(text)
        if os.path.abspath(relay_path) == os.path.join(_HERE, 'relay.py'):
            import relay
            return relay.deliver(_default_stream(), text)
        argv = [sys.executable, _default_relay_path(), 'send', '--stream', _default_stream(), '--text', text]
    else:
        argv = shlex.split(command, posix=(os.name != 'nt')) + [text]
    result = subprocess.run(argv,
                            capture_output=True, text=True, encoding='utf-8', timeout=30)
    if result.returncode:
        return False
    try:
        receipt = json.loads(result.stdout.strip())
    except (ValueError, TypeError):
        return True
    return receipt if isinstance(receipt, dict) else False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Deliver a reminder or stable owner notification')
    parser.add_argument('text')
    parser.add_argument('--run-id')
    parser.add_argument('--phase', default='reminder')
    parser.add_argument('--condition', default='due')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    sys.exit(0 if notify(args.text, run_id=args.run_id, phase=args.phase,
                         condition=args.condition, retry_failed=args.retry_failed) else 1)
