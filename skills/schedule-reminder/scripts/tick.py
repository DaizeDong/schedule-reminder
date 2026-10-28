#!/usr/bin/env python3
"""schedule-reminder: the one scheduled entry point (Task Scheduler: ScheduleReminderTick, every 5 min).

One tick runs three steps in a fixed order, each in this process and each with its own result:

  work    agent_tick.run()     reap finished agent orders, launch the next queued one (fast)
  remind  store.tick()         fire due reminders through relay.py
  ingest  ingest_tick.run()    poll every Agent Center channel and dispatch replies (self-budgeted)

A failing step never stops the next one. The JSON summary names every step; the exit code is the
number of steps that raised, so the Scheduler's last result shows a red tick without hiding which
step it was. Ingest keeps its own time budget (ingest_tick.TASK_LIMIT_S) inside the 5-minute interval.

CLI: tick.py [--no-post] [--only work|remind|ingest ...]
"""
import argparse
import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

STEPS = ("work", "remind", "ingest")


def _work(post):
    import agent_tick
    return agent_tick.run(post=post)


def _remind(post):
    import store
    if not post:
        return store.tick(dry_run=True)
    return store.tick()


def _ingest(post):
    import ingest_tick
    return ingest_tick.run(post=post)


_RUN = {"work": _work, "remind": _remind, "ingest": _ingest}


def run(only=None, post=True):
    results, failed = {}, 0
    for name in STEPS:
        if only and name not in only:
            continue
        try:
            results[name] = {"ok": True, "result": _RUN[name](post)}
        except (Exception, SystemExit) as error:  # SystemExit: a step refused on a secondary machine
            failed += 1
            results[name] = {"ok": False, "error": type(error).__name__}
            traceback.print_exc(file=sys.stderr)
    return results, failed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tick.py")
    ap.add_argument("--no-post", dest="post", action="store_false",
                    help="no Discord delivery: reminders dry-run, agent and ingest skip confirmations")
    ap.add_argument("--only", nargs="+", choices=STEPS, default=None)
    a = ap.parse_args(argv)
    results, failed = run(only=a.only, post=a.post)
    print(json.dumps(results, ensure_ascii=False, default=str))
    return failed


if __name__ == "__main__":
    sys.exit(main())
