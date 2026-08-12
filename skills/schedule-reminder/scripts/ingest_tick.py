#!/usr/bin/env python3
"""schedule-reminder — Agent Center INBOUND tick: poll every stream channel, dispatch new user replies.

Scheduled entrypoint (Task Scheduler: AgentCenterIngestTick, ~every 10 min). The inbound mirror of
the outbound relay:
  1. ingest.poll_all()  advances each stream's cursor and writes <stream>.inbox for streams that got
     a NEW user reply this tick (returns {stream: n_new}).
  2. For each such stream, dispatch.dispatch() judges the reply with the cost-ordered LLM chain
     (codex -> cc -> claude, read-only), executes deterministically via reminder.py, and confirms
     back to that channel via relay.py.

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

_LOG = os.path.join(os.path.expanduser("~"), ".agent-center", "state", "ingest_tick.log")
_PROVIDERS = {"codex": {"model": "gpt-5.6-sol", "reasoning": "max"},
              "cc": {"model": "claude-opus-4-8"}, "claude": {"model": "claude-opus-4-8"}}


def _log(msg):
    try:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        stamp = "?"
    line = "%s %s" % (stamp, msg)
    print(line, file=sys.stderr)
    try:
        os.makedirs(os.path.dirname(_LOG), exist_ok=True)
        with open(_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_inbox(stream):
    fp = ingest._inbox_file(stream)
    return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""


def _read_reactions_inbox(stream):
    fp = ingest._reactions_inbox_file(stream)
    return open(fp, encoding="utf-8").read() if os.path.exists(fp) else ""


def run(only_stream=None, post=True, timeout=180):
    text_result = ingest.poll_all(log=_log)                 # {stream: n_new text replies}
    rx_result = {}                                          # {stream: n_new emoji reactions}
    try:
        rx_result = ingest.poll_all_reactions(log=_log)
    except Exception as e:
        _log("tick: reaction poll crashed: %s" % type(e).__name__)
    active = set(text_result) | set(rx_result)
    streams = [only_stream] if only_stream else sorted(active)
    handled = {}

    # Ack the user's own message with 👀 as soon as it is picked up (swapped to ✅ after dispatch),
    # so the judgment chain's ~60-70s of thinking is visibly "received" instead of silence.
    # Best effort throughout: a failed reaction must never affect whether the reply gets processed.
    acked = {}
    try:
        token = ingest.bot_token(ingest.load_registry())
    except Exception:
        token = None
    if token:
        for stream in streams:
            ch_ids = getattr(ingest, "LAST_POLL_IDS", {}).get(stream)
            if not ch_ids:
                continue
            ch, mids = ch_ids
            for mid in mids:
                if ingest.ack_seen(ch, mid, token):
                    acked.setdefault(stream, (ch, []))[1].append(mid)
    reg = {}
    try:
        reg = ingest.load_registry()
    except Exception as e:
        _log("tick: registry unreadable (%s); command handlers disabled this tick"
             % type(e).__name__)
    handled_cmds = {}

    for stream in streams:
        if only_stream and stream not in active:
            _log("tick: stream %s had no new replies/reactions this poll -> skip" % stream)
            continue
        parts = []
        if stream in text_result:
            # Deterministic command handlers get first refusal, per message. What they claim is
            # already answered in the channel and must not also be judged by the chain; what they
            # do not claim carries on exactly as before. The inbox on disk still holds the whole
            # batch either way, so nothing the bus read is missing from the record.
            ch, msgs = ingest.LAST_POLL_MSGS.get(stream, (None, []))
            t = ""
            if msgs and ch:
                try:
                    claimed, remaining, results = commands.route(msgs, stream, ch, reg,
                                                                 log=_log, post=post)
                    if results:
                        handled_cmds[stream] = results
                    t = ingest.format_messages(remaining) if remaining else ""
                except Exception as e:
                    _log("tick: command routing crashed for %s (%s); falling back to the chain"
                         % (stream, type(e).__name__))
                    t = _read_inbox(stream)
            else:
                t = _read_inbox(stream)
            if t.strip():
                parts.append(t)
        if stream in rx_result:
            r = _read_reactions_inbox(stream)
            if r.strip():
                parts.append(r)
        reply = "\n".join(parts).strip()
        if not reply:
            continue
        try:
            ok = dispatch.dispatch(stream, reply, providers=_PROVIDERS, timeout=timeout,
                                   log=_log, post=post,
                                   channel_id=ingest.CHANNEL_OF.get(stream))
            handled[stream] = "ok" if ok else "passthrough"
        except Exception as e:
            handled[stream] = "error:%s" % type(e).__name__
            _log("tick: dispatch[%s] crashed: %s" % (stream, type(e).__name__))
        finally:
            # Swap 👀 -> ✅ once this stream is done, whatever the verdict: the mark means "processed",
            # not "succeeded" (the channel confirmation message carries the actual outcome).
            if token and stream in acked:
                ch, mids = acked[stream]
                for mid in mids:
                    try:
                        ingest.ack_done(ch, mid, token)
                    except Exception:
                        pass
    return {"polled": text_result, "reactions": rx_result, "handled": handled,
            "commands": handled_cmds}


def main():
    ap = argparse.ArgumentParser(prog="ingest_tick.py")
    ap.add_argument("--stream", default=None, help="only dispatch this stream")
    ap.add_argument("--no-post", dest="post", action="store_false", help="skip channel confirmations")
    ap.add_argument("--timeout", type=int, default=180)
    a = ap.parse_args()
    out = run(only_stream=a.stream, post=a.post, timeout=a.timeout)
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
