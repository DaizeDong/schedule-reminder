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
import private_data
import runner_job
import store
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
TICK = os.path.join(_HERE, "agent_tick.py")

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

EXT_REQUEST_SHA = "x_agent_exec_request_sha256"

EXT_NOT_STARTED = "x_agent_exec_not_started"  # launches whose suspended runner ended unrun
EXT_GENERATION = "x_agent_exec_generation"
EXT_RUN_ID = "x_agent_exec_run_id"
EXT_ATTEMPT_ID = "x_agent_exec_attempt_id"

EXT_VERSION = 2
# ext is argv-bound (see the module docstring). This ceiling is asserted in the tests so a future
# field that carries free text gets caught here rather than by a truncated command line in the field.
EXT_MAX_CHARS = 2000

STATE_QUEUED, STATE_RUNNING, STATE_STALLED = "queued", "running", "stalled"
STATE_DONE, STATE_FAILED = "done", "failed"
STATE_STOPPING = "stop_pending"
# Every execution state an order can carry; anything else is a damaged record.
EXEC_STATES = (STATE_QUEUED, STATE_RUNNING, STATE_STOPPING, STATE_STALLED, STATE_DONE, STATE_FAILED,
               "preparing", "reconcile", "review_unavailable", "cancelled", "interrupted")


def launch_lock():
    """Exclude launch and stop from each other across processes.

    Every state change is already a generation-fenced store transaction. This lock covers only
    the gap the store cannot see: a stop that arrives between the spawn and the PID receipt would
    find neither a process to kill nor a reason to cancel. Hold it for that window and nothing
    slower (no notification, no model call)."""
    return private_data.file_lock(os.path.join(runs_root(), ".lifecycle.lock"))


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wake_tick():
    """Start a detached tick now instead of waiting for the scheduled poll. Never raises: the
    scheduled AgentCenterWorkTick remains the backstop when this cannot start."""
    exe = sys.executable or "python"
    windowless = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.isfile(windowless):
        exe = windowless
    # NO_WINDOW | DETACHED, first also leaving the caller's job (a runner waking the next order
    # is inside its own runner job); a job that forbids breakaway refuses that, so retry without.
    attempts = ((0x08000000 | 0x00000008 | 0x01000000), (0x08000000 | 0x00000008)) \
        if sys.platform == "win32" else (0,)
    for flags in attempts:
        try:
            subprocess.Popen([exe, "-B", TICK], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags,
                             **({} if sys.platform == "win32" else {"start_new_session": True}))
            return True
        except Exception:
            continue
    return False


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
    return os.environ.get("AGENT_CENTER_RUNS") or str(private_data.config_root()/"agent-runs")


def run_dir(item):
    """An order's run directory. Writers create it through private_data.open_for_write."""
    return os.path.join(runs_root(), _ext(item).get(EXT_DIR) or item["id"])


def append_event(item, kind, **fields):
    """Persist an event; a missing or unwritable PRIVATE record is an explicit failure."""
    d = run_dir(item)
    rec = {"ts": _utcnow(), "event": kind, **fields}
    with private_data.open_for_write(os.path.join(d, "events.jsonl"), "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- workspace
def default_workspace():
    return os.environ.get("AGENT_EXEC_WORKSPACE") or os.path.expanduser("~")


def is_git_workspace(path):
    """True when ``path`` or a parent holds a .git entry."""
    if not path:
        return False
    from pathlib import Path
    return any((root/".git").exists() for root in (Path(path), *Path(path).parents))


def order_workspace(token):
    """A dedicated artifact workspace for one order, stable for one action identity.

    It lives beside the run records in the private companion and is created here."""
    path = os.path.join(runs_root(), "workspaces", hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:32])
    private_data.check_destination(path)
    os.makedirs(path, exist_ok=True)
    return path


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
# Windows recycles PIDs, so process creation time must match the recorded identity.
# os.kill(pid, 0) is unsuitable for this check on Windows:
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
        error = k.GetLastError()
        if error not in (0, 87, 1168):
            raise RuntimeError("process identity is unavailable (Windows error %s)" % error)
        return (False, None)
    try:
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(h, *map(ctypes.byref, (c, e, kt, ut))):
            raise RuntimeError("process creation time is unavailable")
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            raise RuntimeError("process exit status is unavailable")
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


# --------------------------------------------------------------------------- queue operations

def _persist_request(item, request):
    """Save the complete request atomically before publishing runnable work.

    No lock: one item has one request sha (checked before this), so two racing writers write
    identical bytes, and each writes its own temporary file before the atomic replace."""
    directory = run_dir(item)
    path = os.path.join(directory, "request.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as stream:
            if stream.read() != (request or ""):
                raise RuntimeError("action identity already has a different durable request")
        return directory
    temporary = "%s.%s.tmp" % (path, uuid.uuid4().hex)
    with private_data.open_for_write(temporary, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(request or "")
        stream.flush()
        os.fsync(stream.fileno())
    private_data.assert_writable_path(path)
    os.replace(temporary, path)
    return directory


def enqueue(stream, request, workspace=None, msg_id=None, title=None, *,
            idempotency_key=None, origin_item_id=None, evidence=None, work_id=None, db_path=None, action_id=None):
    """Create a queued work order. Returns the item dict, or {"_err": ...}."""
    private_data.check_destination(runs_root())
    ws, note = resolve_workspace(workspace)
    ext = {
        EXT_V: EXT_VERSION,
        EXT_STATE: "preparing",
        EXT_STREAM: stream,
        EXT_MSG: msg_id,
        EXT_WORKSPACE: ws,
        EXT_ROUND: 0,
        EXT_APPROACH: 0,
        EXT_REQUEST_SHA: hashlib.sha256((request or "").encode("utf-8")).hexdigest(),
    }
    if note:
        ext[EXT_NOTE] = note
    if origin_item_id:
        ext['x_console_origin_item'] = origin_item_id
    if evidence is not None:
        if evidence not in ('git', 'artifacts'):
            return {"_err": "invalid evidence mode"}
        ext['x_agent_exec_evidence'] = evidence
    if work_id is None:
        identity = [request or '', os.path.normcase(os.path.abspath(ws)), origin_item_id, evidence]
        ext['x_agent_exec_request_identity'] = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()
    t = (title or request or "").strip().replace("\n", " ")
    if not idempotency_key:
        idempotency_key = ('agent-message:' + hashlib.sha256(
            json.dumps([stream, msg_id]).encode('utf-8')).hexdigest()) if msg_id else 'agent-request:' + store.uuid7()
    if work_id is None:
        r = store.ensure_item(("执行:" + t)[:200], kind='task', source=WORK_SOURCE,
            ext=ext, actor=ACTOR, db_path=db_path, idempotency_key=idempotency_key)
    else:
        r = {'item': store.add_item(("执行:" + t)[:200], kind='task', source=WORK_SOURCE,
            ext=ext, actor=ACTOR, db_path=db_path, _id=work_id,
            idempotency_key=idempotency_key, if_exists="return")}
    item = r.get("item")
    if not item:
        return r
    if r.get('decision') in ('reused', 'replayed') and item.get('idempotency_key') != idempotency_key:
        return item
    if idempotency_key and ((work_id is not None and item['id'] != work_id) or _ext(item).get('x_console_origin_item') != origin_item_id
                           or _ext(item).get(EXT_WORKSPACE) != ws):
        return {'_err': 'work identity conflict'}
    if _ext(item).get(EXT_REQUEST_SHA) != ext[EXT_REQUEST_SHA]:
        return {'_err': 'work request identity conflict'}
    if idempotency_key and exec_state(item) != 'preparing':
        read_request(item)
        return item
    # The request is written to the run directory, never into ext: it is user text of unbounded
    # length and ext is argv-bound.
    _persist_request(item, request)
    item = store.publish_work(item["id"], actor=ACTOR, db_path=db_path, action_id=action_id)
    if not item:
        return {"_err": "work preparation cancelled or changed"}
    append_event(item, "enqueued", stream=stream, workspace=ws, note=note)
    wake_tick()
    return item



def read_request(item):
    """A missing or damaged request cannot authorize work from a shortened title."""
    with open(os.path.join(run_dir(item), "request.txt"), encoding="utf-8") as stream:
        request = stream.read()
    expected = _ext(item).get(EXT_REQUEST_SHA)
    if expected and hashlib.sha256(request.encode("utf-8")).hexdigest() != expected:
        raise RuntimeError("durable work request failed its integrity check")
    return request


def orders(active_only=True):
    """Every work order, oldest first, read in process; a store failure raises."""
    out, cursor = [], None
    while True:
        page = store.list_items(source=WORK_SOURCE, active_only=active_only, limit=100, cursor=cursor)
        out.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return out


def get(item_id):
    """The work order, or None when it does not exist."""
    return store.get_item(item_id)


def running(items=None):
    """Orders whose ext says running. Says nothing about whether the process is alive; that is the
    reaper's job, and keeping the two apart is what lets the reaper report a death instead of
    quietly hiding it."""
    return [it for it in (orders() if items is None else items) if exec_state(it) in (STATE_RUNNING, STATE_STOPPING)]


def queued(items=None):
    return [it for it in (orders() if items is None else items) if exec_state(it) == STATE_QUEUED]


def claim(item_id, *, run_id=None, return_operation=False):
    """Atomically reserve the serial writer and publish state/ext/generation.

    The bool result remains available. The dispatcher requests the winning receipt,
    rather than rereading a possibly newer generation after cancellation/reopen.
    """
    op = store.claim_work(item_id, run_id=run_id, actor=ACTOR)
    return op if return_operation else bool(op)


def record_process(item_id, pid, pstart, *, generation):
    return _advance(item_id, generation, "receipt", pid=pid, pstart=pstart)


def finish(item_id, ok, note="", exec_state_value=None, *, generation):
    """Commit pool state and execution metadata together; a lost generation returns False."""
    return _advance(item_id, generation, "finish", note=note,
                    outcome=exec_state_value or (STATE_DONE if ok else STATE_FAILED))


def current_generation(item_id):
    """The generation a stop applies to when the caller saw none: 0 names a never-claimed order."""
    op = operation(item_id)
    return op["generation"] if op else 0


def request_stop(item_id, note="", *, expected_generation=None):
    """Keep the work order active while termination is being established."""
    if expected_generation is None:
        expected_generation = current_generation(item_id)
    return store.request_work_stop(item_id, expected_generation,
        note=note or 'user asked to stop', actor=ACTOR) or {'_err': 'ERR_STALE_GENERATION'}


def cancel(item_id, note="", *, expected_generation=None):
    """Commit cancellation and execution metadata in the same pool transaction."""
    if expected_generation is None:
        expected_generation = current_generation(item_id)
    return store.cancel_work(item_id, expected_generation, note=note, actor=ACTOR) or {
        '_err': 'ERR_STALE_GENERATION'}


def operation(item_id):
    return store.work_operation(item_id)


def _advance(item_id, generation, action, **kwargs):
    if generation is None:
        return False
    return store.advance_work(item_id, generation, action, actor=ACTOR, **kwargs)


def begin_spawn(item_id, generation):
    return _advance(item_id, generation, "spawn")


def start_runner(item_id, generation, pid, pstart):
    return _advance(item_id, generation, "start", pid=pid, pstart=pstart)


def checkpoint(item_id, generation, name, *, fields=None, progress=None):
    return _advance(item_id, generation, "checkpoint", checkpoint=name,
                    fields=fields, progress=progress)


def owns(item_id, generation):
    op = operation(item_id)
    item = get(item_id)
    return bool(op and op["generation"] == generation and op["outcome"] is None
                and op["started_at"] and item and item["state"] == "doing"
                and exec_state(item) == STATE_RUNNING)


def reconcile(item_id, snapshot, note):
    return _advance(item_id, snapshot["generation"], "reconcile", expected=snapshot, note=note)


def not_started(item_id, snapshot, note, *, retry):
    """The suspended runner was ended before it ran: release the slot and requeue (retry) or block."""
    return _advance(item_id, snapshot["generation"], "not_started", expected=snapshot, note=note,
                    checkpoint="requeue" if retry else "block")


def release(item_id, generation):
    """Release only with a quiescent receipt; parent exit cannot clear uncertain cleanup."""
    return _advance(item_id, generation, "release")


def child_started(item_id, generation, receipt):
    return _advance(item_id, generation, "child_start", receipt=receipt)


def child_finished(item_id, generation, receipt, *, quiescent):
    return _advance(item_id, generation, "child_finish", receipt=receipt,
                    outcome="quiescent" if quiescent else "unknown")


def recover_cleanup(item_id, generation, evidence):
    """Apply an operator's reviewed cleanup evidence after both launcher identities are gone.

    This never infers cleanup from an absent parent or replays an interrupted task. The evidence
    digest binds a separate audit of containment/descendants; current liveness is an extra guard.
    """
    if (not isinstance(evidence, dict) or not isinstance(evidence.get('note'), str)
            or not evidence['note'].strip() or len(evidence['note']) > 1000
            or not re.fullmatch(r'[a-f0-9]{64}', str(evidence.get('evidence_sha256', '')))):
        raise ValueError('reviewed cleanup evidence with a note and SHA256 is required')
    op = operation(item_id)
    if (not op or op['generation'] != generation or not op['outcome'] or op['released_at']
            or op['cleanup_state'] not in ('unknown', 'in_flight')):
        raise ValueError('no matching terminal cleanup reservation')
    for pid, start in ((op['pid'], op['pstart']), (op['launch_pid'], op['launch_pstart'])):
        if pid is None:
            continue
        alive, actual = proc_identity(pid)
        if alive and (start is None or actual is None or str(actual) == str(start)):
            raise ValueError('runner identity is still live or unavailable')
    receipt = {'authority': 'operator-reviewed', **evidence,
               'previous_receipt': json.loads(op['cleanup_receipt'] or 'null')}
    return _advance(item_id, generation, 'recover_cleanup', expected=op, receipt=receipt)


def job_backend():
    """The OS job layer used by a stop to terminate and verify the runner's job."""
    return runner_job.NativeJobs()


def release_verified_cleanup(item_id, generation, receipt, expected):
    """Release a cancelled generation whose runner job was terminated and confirmed empty.

    The receipt comes from runner_job.RunnerJob.confirm_empty; store.advance_work binds it to every
    recorded runner identity and to the caller's exact reservation snapshot."""
    return _advance(item_id, generation, "verified_cleanup", expected=expected, receipt=receipt)


def save_baseline(item_id, generation, baseline, digest, workspace):
    return _advance(item_id, generation, "baseline", baseline=baseline,
                    baseline_sha256=digest, workspace=workspace)


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


def signature(verify_rc, verify_output, changed_files, workspace, evidence=None):
    """A round's fingerprint: what the check said, and what the tree actually looks like.

    File CONTENT is hashed, not just the name: an agent that rewrites the same file with the same
    bytes every round has not moved, and a name-only signature would read that as progress.
    Artifact evidence already carries every file's sha256, so it is used as is."""
    h = hashlib.sha256()
    h.update(("rc=%s\n" % verify_rc).encode("utf-8"))
    h.update((normalize_output(verify_output)[:4000] + "\n").encode("utf-8"))
    if evidence is not None:
        h.update(evidence.encode("utf-8"))
        return h.hexdigest()
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
    ap.add_argument("verb", choices=["list", "runs-root", "status", "recover-cleanup"])
    ap.add_argument("--all", action="store_true", help="include terminal orders")
    ap.add_argument('--id')
    ap.add_argument('--generation', type=int)
    ap.add_argument('--evidence-file')
    a = ap.parse_args()
    if a.verb == "runs-root":
        print(runs_root())
        return 0
    if a.verb == 'status':
        keys = ('item_id', 'generation', 'checkpoint', 'outcome', 'cleanup_state', 'cleanup_receipt',
                'pid', 'pstart', 'launch_pid', 'launch_pstart', 'created_at', 'updated_at', 'released_at')
        print(json.dumps({'schemaVersion': 1, 'reservations': [
            {key: op[key] for key in keys} for op in store.work_reservations()],
            'queued_ids': [item['id'] for item in queued()]}, ensure_ascii=False))
        return 0
    if a.verb == 'recover-cleanup':
        if not a.id or a.generation is None or not a.evidence_file:
            ap.error('--id, --generation and --evidence-file are required')
        with open(a.evidence_file, encoding='utf-8') as stream:
            evidence = json.load(stream)
        ok = recover_cleanup(a.id, a.generation, evidence)
        print(json.dumps({'schemaVersion': 1, 'recovered': ok, 'item_id': a.id}))
        return 0 if ok else 1
    items = orders(active_only=not a.all)
    print(json.dumps([{"id": it["id"], "state": it.get("state"),
                       "exec": exec_state(it), "title": it.get("title"),
                       "stream": _ext(it).get(EXT_STREAM)} for it in items], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
