#!/usr/bin/env python3
"""schedule-reminder — Agent Center INBOUND tick: poll every stream channel, dispatch new user replies.

The ingest step of tick.py (Task Scheduler: ScheduleReminderTick). The inbound mirror of the outbound
relay:
  1. Poll text and reactions (one read per channel), staging identified work before advancing
     consumption state.
  2. Drain durable pending work, including retries from earlier polls. Successful processing
     is recorded separately from receipt; model plans and action identities survive retries.

ONE TIME BUDGET
    A tick plans for a self-imposed TASK_LIMIT_S that fits inside the 5-minute task interval, so it
    finishes before the next tick is due (the registration sets no kill limit). The poll starts no new
    channel after INTAKE_S, and the poll's start channel rotates every five minutes so the same tail
    channels are not always the ones skipped. Intake of pending records has its own INTAKE_S window
    measured from when dispatch starts, so a slow poll never starves staged work, but no record
    starts with less than MIN_DISPATCH_S left of the whole tick; the planner
    call gets at most PLANNER_S and never more than what is left before RESERVE_S; a command
    handler is clamped the same way. A tick that overran the interval would make Task Scheduler
    skip the next one (MultipleInstancesPolicy IgnoreNew).

RETRIES
    A dispatch that did not finish (a failed action, an undelivered confirmation, no usable plan)
    stays pending for the next tick, up to inbound.MAX_FAILURES attempts; then the record becomes
    `failed` and waits for --retry-command. A rejected action is final and completes the record.

CLI: ingest_tick.py                 # poll + dispatch all, JSON summary
     ingest_tick.py --stream mail   # only process this stream (still polls all to advance cursors)
     ingest_tick.py --no-post       # forward to dispatch: skip channel confirmations (pool still writes)
     ingest_tick.py --retry-command ID   # rearm one failed record after checking its effects
Stdlib only (+ sibling modules ingest, inbound; dispatch and commands load only when work is pending).
"""
import argparse
import datetime
import json
import os
import sys
import time
import private_data
import inbound

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ingest    # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_LOG = None
TASK_LIMIT_S = 240   # self-imposed budget, inside the 5-minute task interval
INTAKE_S = 60        # no new channel poll or pending record is started after this
PLANNER_S = 120      # the most one planner call may take
RESERVE_S = 30       # kept back for executing a plan, confirming and recording the outcome
MIN_DISPATCH_S = 30  # no pending record is started with less than this left before RESERVE_S


def _log(msg):
    try:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        stamp = "?"
    line = "%s %s" % (stamp, msg)
    print(line, file=sys.stderr)
    path = _LOG or str(private_data.config_root()/"state"/"ingest_tick.log")
    with private_data.open_for_write(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _handlers():
    """The command and dispatch layers, imported only when there is pending work to hand them."""
    import commands
    import dispatch
    return commands, dispatch


def run(only_stream=None, post=True):
    start = time.monotonic()
    # New polling may fail while previously staged work remains available.
    polled = {}
    try:
        polled = ingest.poll(log=_log, deadline=start + INTAKE_S)
    except Exception as error:
        _log("tick: poll failed: %s" % type(error).__name__)
    handled, handled_cmds = {}, {}
    work = [record for record in ingest.pending_work()
            if not only_stream or record["stream"] == only_stream]
    if work:
        commands, dispatch = _handlers()
        reg = ingest.load_registry()
        try:
            token = ingest.bot_token(reg)
        except Exception as error:  # BotTokenUnavailable; its message names the cause, never the value
            # Staged work still dispatches; only the Discord acknowledgement needs the bot.
            _log("tick: bot token unavailable: %s: %s" % (type(error).__name__, error))
            token = None
    # Dispatch gets its own intake window measured from when it starts, so a slow poll can never
    # starve records that are already staged.
    dispatch_start = time.monotonic()
    for index, record in enumerate(work):
        if (time.monotonic() - dispatch_start > INTAKE_S
                or TASK_LIMIT_S - RESERVE_S - (time.monotonic() - start) < MIN_DISPATCH_S):
            _log("tick: time budget spent; %d pending record(s) wait for the next tick"
                 % (len(work) - index))
            break
        stream = record["stream"]
        try:
            with inbound.processing(record, ingest.state_dir()) as current:
                if not current or current["status"] != "pending":
                    continue
                budget = max(1, TASK_LIMIT_S - RESERVE_S - (time.monotonic() - start))
                channel = current["channel_id"]
                message_id = current["message_id"]
                if token and current["kind"] == "text" and not current["attempts"]:
                    try:
                        ingest.ack_seen(channel, message_id, token)
                    except Exception as error:
                        _log("tick: acknowledgement failed: " + type(error).__name__)
                done, command_claimed = False, False
                if current["kind"] == "text":
                    message = current["payload"]
                    claimed, remaining, results = commands.route(
                        [message], stream, channel, reg, log=_log, post=post, timeout_cap=budget,
                        before_run=lambda name: inbound.command_started(current, name, ingest.state_dir()))
                    if results:
                        handled_cmds.setdefault(stream, []).extend(results)
                    command_claimed = bool(claimed)
                    done = (command_claimed and not remaining and bool(results)
                            and all(result.get("ok") is True for result in results))
                    reply = ingest.format_messages([message])
                else:
                    reply = ingest.format_reaction(current["payload"])
                if not command_claimed:
                    origin_id = (message_id if current["kind"] == "text"
                                 else current["payload"]["message_id"])
                    done = dispatch.dispatch(
                        stream, reply, log=_log, post=post, channel_id=channel, msg_id=origin_id,
                        inbound_id=current["id"], timeout=max(1, min(PLANNER_S, budget)),
                        final=current.get("failures", 0) + 1 >= inbound.MAX_FAILURES)
                error = None if done else ("command failed; explicit retry required"
                                            if command_claimed else "dispatch did not finish")
                status = inbound.attempted(current, done, error, ingest.state_dir(),
                                           retryable=not command_claimed)
                if status == "failed":
                    _log("tick: dispatch[%s] gave up after %d attempts; rearm with --retry-command %s"
                         % (stream, inbound.MAX_FAILURES, current["id"]))
                prior = handled.get(stream)
                handled[stream] = ("failed" if status in ("failed", "command_failed") or prior == "failed"
                                   else "ok" if done and prior in (None, "ok") else "pending")
                if done and token and current["kind"] == "text":
                    try:
                        ingest.ack_done(channel, message_id, token)
                    except Exception as error:
                        _log("tick: completion acknowledgement failed: " + type(error).__name__)
        except Exception as error:
            handled[stream] = "error:" + type(error).__name__
            _log("tick: dispatch[%s] failed; inspect its durable inbound status: %s" %
                 (stream, type(error).__name__))
    return {"polled": polled.get("text", {}), "reactions": polled.get("reactions", {}),
            "handled": handled, "commands": handled_cmds}


def main():
    ap = argparse.ArgumentParser(prog="ingest_tick.py")
    ap.add_argument("--stream", default=None, help="only dispatch this stream")
    ap.add_argument("--no-post", dest="post", action="store_false", help="skip channel confirmations")
    ap.add_argument("--retry-command", metavar="INBOUND_ID",
                    help="explicitly rearm one failed command or exhausted dispatch after checking its effects")
    a = ap.parse_args()
    if a.retry_command:
        inbound.retry_command(a.retry_command, ingest.state_dir())
    out = run(only_stream=a.stream, post=a.post)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
