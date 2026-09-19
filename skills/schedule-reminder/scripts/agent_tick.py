#!/usr/bin/env python3
"""schedule-reminder - Agent Center WORK TICK: reap the dead, then launch one.

Scheduled entrypoint (Task Scheduler: AgentCenterWorkTick, every 2 min). Deliberately a SEPARATE
task from the inbound tick: a work order can run for hours, and the two must not be able to starve
each other.

    agent_tick.py                # reap, then launch at most one queued order
    agent_tick.py --reap-only    # reap and report, launch nothing
    agent_tick.py --stop <id>    # cancel one order and kill its process tree ('*' for whichever is running)

REAP BEFORE DISPATCH, always. An order recorded as running is either the same live process that was
launched or it is dead, and a dead one is REPORTED, never silently requeued. Requeueing a run that
died halfway is how a half-finished edit gets a second agent thrown at it.

Liveness is (pid, process creation time), not pid alone: Windows recycles pids, so a pid-only check
reads a recycled number as the live holder. See agent_task.proc_identity.

DETACHED CHILD. The runner is launched with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP |
CREATE_NO_WINDOW and its stdio pointed at a real file. Measured on this platform: such a child keeps
running both when this parent exits normally and when the scheduler terminates this parent at its
execution time limit, which is exactly what lets a 2 minute tick own an hours-long job. Do NOT add
CREATE_BREAKAWAY_FROM_JOB: from inside a scheduled task it raises access denied, and it is not
needed. Redirecting stdio to a file is also what gives the child a real sys.stdout, which it would
not have under a pythonw parent.

Stdlib only (+ sibling agent_task, relay).
"""
import argparse
import datetime
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import agent_task  # noqa: E402
import relay       # noqa: E402
from contextvars import ContextVar

_NOTIFICATION = ContextVar('work_notification', default=(None, 'reconcile'))

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

RUNNER = os.path.join(_HERE, "agent_run.py")

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

# A just-claimed order has no pid yet: the claim and the spawn cannot be atomic. Within this window
# a missing pid means "starting", not "dead". Without it a tick could reap the run the previous tick
# had launched microseconds earlier.
CLAIM_GRACE_SECONDS = 180


def _log(msg):
    print(msg, flush=True)


def _post(stream, text, *, run_id=None, condition='reconcile'):
    try:
        import notification_client as client
        if run_id is None:
            run_id, condition = _NOTIFICATION.get()
        receipt = client.submit('work-order', run_id, 'terminal', condition, stream, text,
                                language='preserve', fallback='big_brother')
        if receipt['state'] != 'sent':
            _log('report: ' + client.detail(receipt))
        return receipt
    except Exception as e:
        _log("relay failed: %s" % type(e).__name__)


def _post_owner(stream, text, *, run_id, condition='reconcile'):
    """Retain the existing two-argument reporting callback seam."""
    token = _NOTIFICATION.set((run_id, condition))
    try:
        return _post(stream, text)
    finally:
        _NOTIFICATION.reset(token)


def _age_seconds(item):
    stamp = item.get("updated_at") or item.get("created_at")
    if not stamp:
        return 1e9
    try:
        s = str(stamp).replace("Z", "+00:00")
        t = datetime.datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds()
    except Exception:
        return 1e9


def log_tail(item, lines=18):
    try:
        p = os.path.join(agent_task.run_dir(item), "run.log")
        with open(p, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""


def reap(items=None, post=True):
    """Reconcile dead/ambiguous generations without ever replaying an action.

    A receipt written after our liveness probe makes the snapshot CAS fail. A late child must
    win its startup CAS before doing work, so revoking an unstarted generation is safe.
    Parent identity is not descendant cleanup evidence. In-flight/unknown cleanup keeps its
    reservation after reconciliation; only the shared execution receipt can establish quiescence.
    """
    reaped = []
    for op in agent_task.store.work_reservations():
        it = agent_task.get(op["item_id"])
        pid = op["pid"] if op["pid"] is not None else op["launch_pid"]
        pstart = op["pstart"] if op["pid"] is not None else op["launch_pstart"]
        if pid is not None:
            alive, actual_start = agent_task.proc_identity(pid)
            if alive and (pstart is None or actual_start is None or str(actual_start) == str(pstart)):
                continue
        if op["outcome"] is not None:
            agent_task.release(op["item_id"], op["generation"])
            continue
        if not op["started_at"] and _age_seconds({"updated_at": op["updated_at"]}) < CLAIM_GRACE_SECONDS:
            continue
        note = "runner identity absent; reconcile without replay (pid=%s)" % pid
        if not agent_task.reconcile(it["id"], op, note):
            continue
        reaped.append(it["id"])
        _log("reconcile: %s pid=%s" % (it["id"][:8], pid))
        if post:
            _post_owner((it.get("ext") or {}).get(agent_task.EXT_STREAM) or "infra",
                  "工作单 `%s` 没有完成；执行状态待核对，不会自动重排。" % it["id"][:8], run_id=op['run_id'])
    return reaped


def launch(item, *, generation=None, post_reports=True):
    """Spawn the runner detached and record (pid, creation time). Returns True on success."""
    ext = item.get("ext") or {}
    generation = generation if generation is not None else ext.get(agent_task.EXT_GENERATION)
    if not agent_task.begin_spawn(item["id"], generation):
        return False
    workspace = ext.get(agent_task.EXT_WORKSPACE) or agent_task.default_workspace()
    if not os.path.isdir(workspace):
        agent_task.finish(item["id"], False, "workspace missing: %s" % workspace, generation=generation)
        agent_task.release(item["id"], generation)
        return False
    d = agent_task.run_dir(item, create=True)
    # A pythonw parent would give the child no usable stdout; a real file handle does. Prefer the
    # console interpreter anyway, since a detached child has no window to show either way.
    exe = sys.executable or "python"
    if os.path.basename(exe).lower().startswith("pythonw"):
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.isfile(cand):
            exe = cand
    logf = open(os.path.join(d, "run.log"), "ab")
    try:
        flags = 0
        if sys.platform == "win32":
            flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
        kw = {"creationflags": flags} if sys.platform == "win32" else {"start_new_session": True}
        argv = [exe, RUNNER, "--id", item["id"], "--generation", str(generation)]
        if not post_reports:
            argv.append("--no-post")
        p = subprocess.Popen(argv,
                             stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                             cwd=workspace, close_fds=True, **kw)
    except Exception as e:
        logf.close()
        # Popen may have crossed the OS spawn boundary before raising. Revoke only an
        # unstarted snapshot; a child that already owns the generation must be reconciled alive.
        op = agent_task.operation(item["id"])
        if op and op["generation"] == generation and not op["started_at"]:
            agent_task.reconcile(item["id"], op, "launch outcome unknown: " + type(e).__name__)
        _log("launch failed: %s" % e)
        return False
    logf.close()
    _alive, pstart = agent_task.proc_identity(p.pid)
    agent_task.record_process(item["id"], p.pid, pstart, generation=generation)
    agent_task.append_event(item, "launched", pid=p.pid, workspace=workspace)
    _log("launched %s pid=%d in %s" % (item["id"][:8], p.pid, workspace))
    return True


def stop(item_id, note="", post=True):
    """Cancel an order and kill its process tree. '*' targets whichever order is running.

    Cancel first, then kill: if the kill lands first the reaper could see a dead process under a
    still-running order and report a crash for something the user asked to stop."""
    items = agent_task.orders()
    if item_id == "*":
        targets = agent_task.running(items)
    else:
        targets = [it for it in items if it["id"] == item_id or it["id"].startswith(item_id)]
    out = []
    for it in targets:
        notification_op = agent_task.operation(it['id'])
        cancelled = agent_task.cancel(it["id"], note or "user asked to stop")
        ext = (cancelled or it).get("ext") or {}
        killed = agent_task.kill_tree(ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART))
        agent_task.append_event(it, "stopped", killed=killed)
        out.append({"id": it["id"], "killed": killed})
        _log("stopped %s (process killed: %s)" % (it["id"][:8], killed))
        if post:
            _post_owner(ext.get(agent_task.EXT_STREAM) or "infra",
                  "🛑 已停止工作单 `%s`%s。%s" % (
                      it["id"][:8],
                      "" if killed else "(它的进程本来就已经不在了)",
                      "标题:%s" % (it.get("title") or "")[:100]),
                  run_id=notification_op['run_id'] if notification_op else 'work-order:' + it['id'],
                  condition='cancelled')
    return out


def run(post=True, reap_only=False):
    agent_task.store.init_db()
    run_id = agent_task.store.uuid7()
    items = agent_task.orders()
    reaped = reap(items, post=post)
    if reaped:
        items = agent_task.orders()
    reservations = agent_task.store.work_reservations()
    q = agent_task.queued(items)
    launched = None
    # The transaction, not this snapshot, enforces serial workspace writes across ticks.
    if not reap_only and not reservations and q:
        target = q[0]
        op = agent_task.claim(target["id"], run_id=run_id, return_operation=True)
        if op and launch(agent_task.get(target["id"]), generation=op["generation"], post_reports=post):
            launched = target["id"]
    return {"run_id": run_id, "reaped": reaped, "running": len(reservations),
            "queued": len(q), "launched": launched}


def main():
    ap = argparse.ArgumentParser(prog="agent_tick.py")
    ap.add_argument("--reap-only", action="store_true")
    ap.add_argument("--stop", default=None, metavar="ID", help="cancel and kill ('*' = the running one)")
    ap.add_argument("--no-post", dest="post", action="store_false")
    a = ap.parse_args()
    if a.stop:
        print(json.dumps({"stopped": stop(a.stop, post=a.post)}, ensure_ascii=False))
        return 0
    print(json.dumps(run(post=a.post, reap_only=a.reap_only), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
