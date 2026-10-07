#!/usr/bin/env python3
"""schedule-reminder — Agent Center INBOUND tick: poll every stream channel, dispatch new user replies.

Scheduled entrypoint (Task Scheduler: AgentCenterIngestTick, ~every 10 min). The inbound mirror of
the outbound relay:
  1. Poll text and reactions, staging identified work before advancing consumption state.
  2. Drain durable pending work, including retries from earlier polls. Successful processing
     is recorded separately from receipt; model plans and action identities survive retries.

CLI: ingest_tick.py                 # poll + dispatch all, JSON summary
     ingest_tick.py --stream mail   # only process this stream (still polls all to advance cursors)
     ingest_tick.py --no-post       # forward to dispatch: skip channel confirmations (pool still writes)
Stdlib only (+ sibling modules ingest, dispatch).
"""
import argparse
import datetime
import json
import os
import sys
import private_data
import inbound

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import ingest    # noqa: E402
import dispatch  # noqa: E402
import commands  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_LOG = None


def _log(msg):
    try:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        stamp = "?"
    line = "%s %s" % (stamp, msg)
    print(line, file=sys.stderr)
    path = _LOG or str(private_data.config_root()/"state"/"ingest_tick.log")
    private_data.prepare_parent(path)
    with private_data.open_for_write(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")



def _read_inbox(stream):
    fp = ingest._inbox_file(stream)
    return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""


def _read_reactions_inbox(stream):
    fp = ingest._reactions_inbox_file(stream)
    return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""


def run(only_stream=None, post=True):
    # New polling may fail while previously staged work remains available.
    text_result, rx_result = {}, {}
    for poll, result, label in ((ingest.poll_all, text_result, "text"),
                                (ingest.poll_all_reactions, rx_result, "reaction")):
        try:
            result.update(poll(log=_log))
        except Exception as error:
            _log("tick: %s poll failed: %s" % (label, type(error).__name__))
    reg = ingest.load_registry()
    token = ingest.bot_token(reg)
    handled, handled_cmds = {}, {}
    for record in ingest.pending_work():
        stream = record["stream"]
        if only_stream and stream != only_stream:
            continue
        try:
            with inbound.processing(record, ingest.state_dir()) as current:
                if not current or current["status"] != "pending":
                    continue
                channel = current["channel_id"]
                message_id = current["message_id"]
                if token and current["kind"] == "text":
                    try:
                        ingest.ack_seen(channel, message_id, token)
                    except Exception as error:
                        _log("tick: acknowledgement failed: " + type(error).__name__)
                done, command_claimed = False, False
                if current["kind"] == "text":
                    message = current["payload"]
                    claimed, remaining, results = commands.route(
                        [message], stream, channel, reg, log=_log, post=post,
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
                    done = dispatch.dispatch(stream, reply, log=_log, post=post,
                                             channel_id=channel, msg_id=origin_id,
                                             inbound_id=current["id"])
                error = None if done else ("command failed; explicit retry required"
                                            if command_claimed else "dispatch rejected or failed")
                inbound.attempted(current, done, error, ingest.state_dir(), retryable=not command_claimed)
                prior = handled.get(stream)
                handled[stream] = ("failed" if (command_claimed and not done) or prior == "failed"
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
    return {"polled": text_result, "reactions": rx_result, "handled": handled, "commands": handled_cmds}


def main():
    ap = argparse.ArgumentParser(prog="ingest_tick.py")
    ap.add_argument("--stream", default=None, help="only dispatch this stream")
    ap.add_argument("--no-post", dest="post", action="store_false", help="skip channel confirmations")
    ap.add_argument("--retry-command", metavar="INBOUND_ID",
                    help="explicitly retry one failed or uncertain command after checking its effects")
    a = ap.parse_args()
    if a.retry_command:
        inbound.retry_command(a.retry_command, ingest.state_dir())
    out = run(only_stream=a.stream, post=a.post)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
