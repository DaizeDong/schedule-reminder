#!/usr/bin/env python3
"""schedule-reminder - Agent Center WORK ORDERS: the queue behind the inbound bus.

The bus could judge a reply and mutate the pool, but nothing could make anything HAPPEN. This module
is the state layer for the half that acts: a work order is an ordinary pool item, so it inherits
durability across reboot, the audit event stream, and the pool's optimistic state machine, whose
compare-and-swap is exactly the lock a queue needs.

Split of concerns:
  agent_task.py  (here)  the work order: enqueue, claim, liveness, terminal states, run directory
  agent_run.py           one order's execution: act -> verify -> review -> decide, stall, rotation
  agent_tick.py          the scheduled drainer: reap the dead, launch one

WHAT LIVES WHERE. `ext` carries only small FIXED fields (see EXT_* below). Everything that grows
(the verbatim request, prompts, transcripts, verification output, diffs) is a file in the order's
run directory, outside this repo. Two reasons, both load bearing: `--ext` reaches reminder.py as a
process argument and Windows caps a command line near 32767 chars, so a growing transcript in `ext`
fails eventually and does so at the worst possible moment; and the pool is a state store, not a log
store.

`due_at` is deliberately left NULL. An item carrying a due date in pending/doing/blocked is a live
candidate for the reminder tick, which would fire Discord notifications for an order that is merely
running.

Stdlib only.
"""
import ctypes
import ctypes.wintypes as wintypes
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REMINDER = os.path.join(_HERE, "reminder.py")

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

# One source for every work order, not agent-center:<stream>. `list --source` is exact equality with
# no prefix match, so a per-stream source would need one query per stream to answer "what is running";
# the origin stream lives in ext instead. It also keeps work orders from being confused with the
# generic follow-up to-dos dispatch creates, which DO use agent-center:<stream>.
WORK_SOURCE = "agent-center:work"
ACTOR = "agent-center-work"

EXT_V = "x_agent_exec_v"                  # ext schema version
EXT_STATE = "x_agent_exec_state"          # queued|running|stalled|done|failed
EXT_STREAM = "x_agent_exec_stream"        # origin channel key
EXT_MSG = "x_agent_exec_msg_id"           # origin Discord message id (may be None)
EXT_DIR = "x_agent_exec_dir"              # run directory NAME, relative to runs_root()
EXT_WORKSPACE = "x_agent_exec_workspace"  # cwd the runner works in (== codex write sandbox)
EXT_PID = "x_agent_exec_pid"
EXT_PSTART = "x_agent_exec_pstart"        # process creation time, the pid-reuse discriminator
EXT_ROUND = "x_agent_exec_round"
EXT_APPROACH = "x_agent_exec_approach"
EXT_NOTE = "x_agent_exec_note"            # short terminal reason, for the pool view

EXT_VERSION = 1
# ext is argv-bound (see the module docstring). This ceiling is asserted in the tests so a future
# field that carries free text gets caught here rather than by a truncated command line in the field.
EXT_MAX_CHARS = 2000

STATE_QUEUED, STATE_RUNNING, STATE_STALLED = "queued", "running", "stalled"
STATE_DONE, STATE_FAILED = "done", "failed"


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- reminder.py bridge
def rem(*args):
    """Call reminder.py and return its parsed JSON, or {"_err": ...}. Never raises.

    --actor is a TOP-LEVEL flag and must precede the verb; passing it after would be an argparse
    usage error (exit 2, plain text, not JSON)."""
    p = subprocess.run([sys.executable, REMINDER, "--actor", ACTOR, *args],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        raw = (p.stderr or p.stdout).strip()
        try:
            return {"_err": json.loads(raw.splitlines()[-1]).get("error_code") or raw[:200],
                    "_raw": raw[:400]}
        except Exception:
            return {"_err": raw[:200] or "exit %d" % p.returncode}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"_err": "unparseable: %s" % (p.stdout or "")[:200]}


def _ext(item):
    e = (item or {}).get("ext")
    return e if isinstance(e, dict) else {}


def exec_state(item):
    return _ext(item).get(EXT_STATE)


# --------------------------------------------------------------------------- run directory (DATA)
def runs_root():
    """Where per-order run records live. Real runtime output, so it is OUTSIDE this repo, in the
    private companion that already holds every other piece of bus state. Assembled with os.path.join
    rather than written as a literal path, matching the rest of the bus. There is no in-repo
    fallback: if this cannot be created the runner fails loudly instead of writing into the repo."""
    return os.environ.get("AGENT_CENTER_RUNS") or os.path.join(
        os.path.expanduser("~"), ".agent-center", "agent-runs")


def run_dir(item, create=False):
    name = _ext(item).get(EXT_DIR) or item["id"]
    p = os.path.join(runs_root(), name)
    if create:
        os.makedirs(p, exist_ok=True)
    return p


def append_event(item, kind, **fields):
    """Append one line to the order's event log. Best effort: a log write must never be able to fail
    a run that actually happened."""
    try:
        d = run_dir(item, create=True)
        rec = {"ts": _utcnow(), "event": kind}
        rec.update(fields)
        with open(os.path.join(d, "events.jsonl"), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- workspace
def default_workspace():
    return os.environ.get("AGENT_EXEC_WORKSPACE") or os.path.expanduser("~")


def resolve_workspace(candidate):
    """A model-proposed workspace is only honoured when it exists and is a directory. Anything else
    falls back to the default, which is reported rather than silently substituted: an agent that
    believes it is working in repo X while its write sandbox is elsewhere produces edits that go
    nowhere. Returns (path, note|None)."""
    if candidate:
        p = os.path.expanduser(str(candidate).strip().strip('"'))
        if os.path.isdir(p):
            return os.path.abspath(p), None
        return default_workspace(), "requested workspace not a directory: %s" % str(candidate)[:120]
    return default_workspace(), None


# --------------------------------------------------------------------------- process identity
# A pid alone cannot answer "is my child still alive": Windows recycles pids, and the fleet's two
# existing pid locks both read a recycled pid as the live holder. The discriminator is the process
# CREATION TIME, which is unique per (pid, process). os.kill(pid, 0) is unusable here: on Windows
# signal 0 is CTRL_C_EVENT, so the call routes through GenerateConsoleCtrlEvent and can send a real
# ctrl+c to a live child sharing the console.
_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def proc_identity(pid):
    """-> (alive: bool, creation_time: int|None). A never-existed pid gives (False, None).

    OpenProcess still succeeds on an exited-but-not-reaped process, so the STILL_ACTIVE check is
    load bearing, not belt and braces."""
    if not pid or sys.platform != "win32":
        return (False, None)
    k = ctypes.windll.kernel32
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return (False, None)
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(h, *map(ctypes.byref, (c, e, kt, ut))):
            return (True, None)
        code = ctypes.c_ulong()
        k.GetExitCodeProcess(h, ctypes.byref(code))
        return (code.value == _STILL_ACTIVE, (c.dwHighDateTime << 32) | c.dwLowDateTime)
    finally:
        k.CloseHandle(h)


def is_live(pid, pstart):
    """True only when pid names the SAME process that was recorded. An unrecorded creation time is
    not treated as a match: without it there is nothing to tell a recycled pid from the original."""
    if not pid or pstart is None:
        return False
    alive, start = proc_identity(pid)
    return bool(alive and start is not None and int(start) == int(pstart))


def kill_tree(pid, pstart):
    """Kill the recorded process AND its descendants, and only if it is still the same process.

    /T is not optional: a launcher invoked through a command wrapper spawns grandchildren that
    survive a direct kill and keep pipe handles open. The identity check is what stops a recycled
    pid from getting an unrelated process killed."""
    if not is_live(pid, pstart):
        return False
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(int(pid))], capture_output=True,
                   **({"creationflags": 0x08000000} if sys.platform == "win32" else {}))
    return True


# --------------------------------------------------------------------------- queue operations
def enqueue(stream, request, workspace=None, msg_id=None, title=None):
    """Create a queued work order. Returns the item dict, or {"_err": ...}."""
    ws, note = resolve_workspace(workspace)
    ext = {
        EXT_V: EXT_VERSION,
        EXT_STATE: STATE_QUEUED,
        EXT_STREAM: stream,
        EXT_MSG: msg_id,
        EXT_WORKSPACE: ws,
        EXT_ROUND: 0,
        EXT_APPROACH: 0,
    }
    if note:
        ext[EXT_NOTE] = note
    t = (title or request or "").strip().replace("\n", " ")
    r = rem("add", "--title", ("执行:" + t)[:200], "--kind", "task",
            "--source", WORK_SOURCE, "--ext", json.dumps(ext, ensure_ascii=False))
    item = r.get("item")
    if not item:
        return r
    # The request is written to the run directory, never into ext: it is user text of unbounded
    # length and ext is argv-bound.
    d = run_dir(item, create=True)
    with open(os.path.join(d, "request.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(request or "")
    append_event(item, "enqueued", stream=stream, workspace=ws, note=note)
    return item


def read_request(item):
    try:
        with open(os.path.join(run_dir(item), "request.txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return item.get("title") or ""


def orders(active_only=True):
    """Every work order, newest last. Paginates, because `list` caps at 100 per page."""
    out, cursor = [], None
    while True:
        args = ["list", "--source", WORK_SOURCE, "--limit", "100"]
        if active_only:
            args.append("--active")
        if cursor:
            args += ["--cursor", cursor]
        r = rem(*args)
        out += r.get("items", [])
        cursor = r.get("next_cursor")
        if not cursor:
            break
    return out


def get(item_id):
    return rem("get", "--id", item_id).get("item")


def running(items=None):
    """Orders whose ext says running. Says nothing about whether the process is alive; that is the
    reaper's job, and keeping the two apart is what lets the reaper report a death instead of
    quietly hiding it."""
    return [it for it in (orders() if items is None else items) if exec_state(it) == STATE_RUNNING]


def queued(items=None):
    return [it for it in (orders() if items is None else items) if exec_state(it) == STATE_QUEUED]


def patch_ext(item_id, **fields):
    """Merge fields into ext. reminder.py merges ext shallowly at the top level, which is all these
    flat keys need."""
    return rem("update", "--id", item_id, "--ext", json.dumps(fields, ensure_ascii=False))


def set_progress(item_id, pct):
    """Progress on an already-doing item must go through `update`. A same-state `transition` is an
    idempotent no-op that returns BEFORE applying --progress, so it would silently do nothing."""
    return rem("update", "--id", item_id, "--set", "progress=%d" % max(0, min(100, int(pct))))


def claim(item_id):
    """Move a queued order to running. Returns True only for the caller that won.

    --expect pending is the whole safety story for overlapping ticks: the loser gets
    ERR_STATE_CONFLICT and must not launch anything."""
    r = rem("transition", "--id", item_id, "--to", "doing", "--expect", "pending")
    if r.get("_err"):
        return False
    patch_ext(item_id, **{EXT_STATE: STATE_RUNNING})
    return True


def record_process(item_id, pid, pstart):
    return patch_ext(item_id, **{EXT_PID: pid, EXT_PSTART: pstart})


def finish(item_id, ok, note="", exec_state_value=None):
    """Terminal state. ok -> pool done; not ok -> pool blocked, which keeps the order visible in the
    active list instead of quietly disappearing into done."""
    st = exec_state_value or (STATE_DONE if ok else STATE_FAILED)
    patch_ext(item_id, **{EXT_STATE: st, EXT_NOTE: (note or "")[:300]})
    if ok:
        return rem("done", "--id", item_id)
    return rem("transition", "--id", item_id, "--to", "blocked",
               "--reason", (note or st)[:300])


def cancel(item_id, note=""):
    patch_ext(item_id, **{EXT_STATE: STATE_FAILED, EXT_NOTE: ("cancelled: " + (note or ""))[:300]})
    return rem("transition", "--id", item_id, "--to", "cancelled", "--reason", (note or "stopped")[:300])


# --------------------------------------------------------------------------- stall signature
_DIGITS = re.compile(r"\d{2,}")


def normalize_output(text):
    """Collapse the parts of a command's output that change on every run without meaning anything:
    timestamps, durations, pids, byte counts. Runs of two or more digits become a marker; single
    digits survive, so "1 failed" and "3 failed" remain different.

    Erring toward "not stalled" is the safe direction here (it costs another round), but output that
    embeds a fresh identifier every run WILL defeat stall detection, and the terminal report says so
    when that happens."""
    return re.sub(r"\s+", " ", _DIGITS.sub("#", text or "")).strip()


def signature(verify_rc, verify_output, changed_files, workspace):
    """A round's fingerprint: what the check said, and what the tree actually looks like.

    File CONTENT is hashed, not just the name: an agent that rewrites the same file with the same
    bytes every round has not moved, and a name-only signature would read that as progress."""
    h = hashlib.sha256()
    h.update(("rc=%s\n" % verify_rc).encode("utf-8"))
    h.update((normalize_output(verify_output)[:4000] + "\n").encode("utf-8"))
    for rel in sorted(set(changed_files or [])):
        p = rel if os.path.isabs(rel) else os.path.join(workspace or "", rel)
        try:
            with open(p, "rb") as f:
                digest = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            digest = "missing"
        h.update(("%s:%s\n" % (rel, digest)).encode("utf-8"))
    return h.hexdigest()


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="agent_task.py", description="inspect the work order queue")
    ap.add_argument("verb", choices=["list", "runs-root"])
    ap.add_argument("--all", action="store_true", help="include terminal orders")
    a = ap.parse_args()
    if a.verb == "runs-root":
        print(runs_root())
        return 0
    items = orders(active_only=not a.all)
    print(json.dumps([{"id": it["id"], "state": it.get("state"),
                       "exec": exec_state(it), "title": it.get("title"),
                       "stream": _ext(it).get(EXT_STREAM)} for it in items], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
