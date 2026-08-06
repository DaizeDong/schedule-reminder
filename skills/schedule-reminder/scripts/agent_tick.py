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


def _post(stream, text):
    try:
        relay.relay(stream, text[:1900])
    except Exception as e:
        _log("relay failed: %s" % type(e).__name__)


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
    """Report every running order whose process is gone. Returns the list of reaped ids."""
    reaped = []
    for it in agent_task.running(items):
        ext = it.get("ext") or {}
        pid, pstart = ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART)
        if agent_task.is_live(pid, pstart):
            continue
        if pid is None and _age_seconds(it) < CLAIM_GRACE_SECONDS:
            _log("reap: %s claimed %.0fs ago and has no pid yet; still starting"
                 % (it["id"][:8], _age_seconds(it)))
            continue
        tail = log_tail(it)
        agent_task.append_event(it, "reaped", pid=pid)
        agent_task.finish(it["id"], False, "runner process died (pid=%s)" % pid)
        reaped.append(it["id"])
        _log("reap: %s dead (pid=%s)" % (it["id"][:8], pid))
        if post:
            _post(ext.get(agent_task.EXT_STREAM) or "infra", "\n".join([
                "⛔ 工作单 `%s` 的执行进程没了(pid=%s),**任务没有完成**,不会自动重排。"
                % (it["id"][:8], pid),
                "标题:%s" % (it.get("title") or "")[:120],
                ("日志末尾:\n```\n%s\n```" % tail[:800]) if tail else "(没有日志可读)",
            ]))
    return reaped


def launch(item):
    """Spawn the runner detached and record (pid, creation time). Returns True on success."""
    ext = item.get("ext") or {}
    workspace = ext.get(agent_task.EXT_WORKSPACE) or agent_task.default_workspace()
    if not os.path.isdir(workspace):
        agent_task.finish(item["id"], False, "workspace missing: %s" % workspace)
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
        p = subprocess.Popen([exe, RUNNER, "--id", item["id"]],
                             stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                             cwd=workspace, close_fds=True, **kw)
    except Exception as e:
        logf.close()
        agent_task.finish(item["id"], False, "could not launch runner: %s" % type(e).__name__)
        _log("launch failed: %s" % e)
        return False
    logf.close()
    _alive, pstart = agent_task.proc_identity(p.pid)
    agent_task.record_process(item["id"], p.pid, pstart)
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
        ext = it.get("ext") or {}
        agent_task.cancel(it["id"], note or "user asked to stop")
        killed = agent_task.kill_tree(ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART))
        agent_task.append_event(it, "stopped", killed=killed)
        out.append({"id": it["id"], "killed": killed})
        _log("stopped %s (process killed: %s)" % (it["id"][:8], killed))
        if post:
            _post(ext.get(agent_task.EXT_STREAM) or "infra",
                  "🛑 已停止工作单 `%s`%s。%s" % (
                      it["id"][:8],
                      "" if killed else "(它的进程本来就已经不在了)",
                      "标题:%s" % (it.get("title") or "")[:100]))
    return out


def run(post=True, reap_only=False):
    items = agent_task.orders()
    reaped = reap(items, post=post)
    if reaped:
        items = agent_task.orders()
    live = [it for it in agent_task.running(items)
            if agent_task.is_live((it.get("ext") or {}).get(agent_task.EXT_PID),
                                  (it.get("ext") or {}).get(agent_task.EXT_PSTART))
            or _age_seconds(it) < CLAIM_GRACE_SECONDS]
    q = agent_task.queued(items)
    if reap_only:
        return {"reaped": reaped, "running": len(live), "queued": len(q), "launched": None}
    launched = None
    # Serial on purpose. Two agents editing one working tree concurrently is a corruption source,
    # not throughput.
    if not live and q:
        target = q[0]                       # ids are time ordered, so this is the oldest
        if agent_task.claim(target["id"]):  # the compare and swap is what makes overlapping ticks safe
            if launch(agent_task.get(target["id"])):
                launched = target["id"]
        else:
            _log("claim lost for %s (another tick took it)" % target["id"][:8])
    return {"reaped": reaped, "running": len(live), "queued": len(q), "launched": launched}


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
