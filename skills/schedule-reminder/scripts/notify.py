#!/usr/bin/env python3
"""schedule-reminder — pluggable notification channel.

Default channel = the **Agent Center `#reminders` channel**, via this repo's own `relay.py`
(`relay.py send --stream reminders`). That is the standing decision (2026-07-01): every skill
notifies into its own Agent Center channel; the Big Brother DM is no longer a notification target.

  NOTE for whoever owns the Discord: a channel post does NOT push to your phone unless that
  channel's notifications are set to All Messages. A DM always pushes. Routing reminders to a
  channel is only safe if #reminders is actually configured to notify you.

Resolution order (first one that exists wins):
  1. SCHEDULE_RELAY_CMD  — explicit override; text appended as final argv. Also the **test seam**
     (tests point it at a stub, so no real Discord push happens).
  2. relay.py            — `send --stream <SCHEDULE_RELAY_STREAM|reminders>` (the Agent Center
     egress; relay.py itself falls back to the DM if that stream is unconfigured, so a reminder is
     never silently lost).
  3. bigbrother DM       — the native Big Brother DM sender (`bigbrother.send_dm`), only if relay.py
     is missing (standalone install). Replaces the old shell-out to the legacy DM notifier script.

Contract: notify(text, run_id=...) -> bool. Only a sent receipt returns True.
Text-only compatibility callers must bind TASK_RUN_ID/SCHEDULE_RUN_ID or pass run_id;
absence is an observable refusal, with no one-shot fallback send.

Env:
  SCHEDULE_RELAY_CMD     full command to run; reminder text appended as last arg (overrides all)
  SCHEDULE_RELAY_PY      path to relay.py       (default: alongside this file)
  SCHEDULE_RELAY_STREAM  Agent Center stream     (default: "reminders")

Secrets: the relay/bigbrother read their token/webhook from the registry; this module never reads,
logs, or echoes any of them.
"""
from __future__ import annotations

import os
import shlex
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_relay_path():
    return os.environ.get("SCHEDULE_RELAY_PY", os.path.join(_HERE, "relay.py"))


def _default_stream():
    return os.environ.get("SCHEDULE_RELAY_STREAM", "reminders")


def notify_event(event, text, **delivery_options):
    """Receipt-aware owner API; requires an explicit Event/stream, with no default routing.

    This returns a receipt, not a bool. The boolean notify shell uses the same producer and
    refuses calls that cannot supply a stable owner occurrence identity.
    """
    from notification_receipts import deliver
    return deliver(event, text, **delivery_options)


def notify_occurrence(run_id, text, *, phase='reminder', condition='due',
                      db_path=None, retry_failed=False):
    """Reminder-owner bridge preserving the explicit override and standalone DM policy."""
    import notification_client as client
    stream = _default_stream()
    cmd = os.environ.get('SCHEDULE_RELAY_CMD')
    if cmd:
        options = {'command': client.command_policy(shlex.split(cmd, posix=(os.name != 'nt')))}
    elif os.path.isfile(_default_relay_path()):
        options = client.transport_options([sys.executable, _default_relay_path(), 'send',
                                            '--stream', stream, '--text'], stream)
    else:
        # The legacy standalone DM is an explicit target, never a stream guessed by the client.
        options = {'command': client.command_policy([sys.executable, os.path.join(_HERE, 'bigbrother.py')])}
    return client.submit('schedule-reminder', run_id, phase, condition, stream, text,
                         language='preserve', db_path=db_path, retry_failed=retry_failed, **options)


def notify(text, *, run_id=None, phase='reminder', condition='due', retry_failed=False):
    """Boolean compatibility shell. A stable owner identity is required for delivery."""
    try:
        import notification_client as client
        receipt = notify_occurrence(run_id, text, phase=phase, condition=condition,
                                    retry_failed=retry_failed)
        if receipt['state'] != 'sent':
            sys.stderr.write('notify: ' + client.detail(receipt) + '\n')
        return receipt['state'] == 'sent'
    except Exception as e:  # delivery failures are signalled by return value, not exceptions
        sys.stderr.write("notify: delivery failed (%s)\n" % type(e).__name__)
        return False


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description='Deliver one stable owner notification')
    parser.add_argument('text')
    parser.add_argument('--run-id')
    parser.add_argument('--phase', default='reminder')
    parser.add_argument('--condition', default='due')
    parser.add_argument('--retry-failed', action='store_true')
    args = parser.parse_args()
    sys.exit(0 if notify(args.text, run_id=args.run_id, phase=args.phase,
                         condition=args.condition, retry_failed=args.retry_failed) else 1)
