#!/usr/bin/env python3
"""Drain serial work: reap terminated orders, refresh durable state, then claim one.

A running or stop-pending order retains the serial slot. Process identity includes PID and creation time. Detached output goes to the PRIVATE run directory. Native survival and termination semantics require separate platform validation."""
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


@agent_task.serialized_lifecycle
def reap(items=None, post=True):
    """Report every running order whose process is gone. Returns the list of reaped ids."""
    reaped = []
    for it in agent_task.running(items):
        ext = it.get("ext") or {}
        if agent_task.exec_state(it) == agent_task.STATE_STOPPING:
            outcome = _finish_stop(it, ext.get(agent_task.EXT_NOTE) or "", post=post)
            if outcome["stopped"]:
                reaped.append(it["id"])
            continue
        pid, pstart = ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART)
        if agent_task.is_live(pid, pstart):
            continue
        if pid is None and ext.get(agent_task.EXT_LAUNCH) == "starting":
            agent_task.request_stop(it["id"], "launch began but process identity is unavailable")
            _log("reap: %s has unproven launch completion; keeping stop pending" % it["id"][:8])
            continue
        if pid is None and _age_seconds(it) < CLAIM_GRACE_SECONDS:
            _log("reap: %s claimed %.0fs ago and has no pid yet; still starting"
                 % (it["id"][:8], _age_seconds(it)))
            continue
        tail = log_tail(it)
        agent_task.append_event(it, "reaped", pid=pid)
        finalized = agent_task.finish(it["id"], False, "runner process died (pid=%s)" % pid)
        if finalized.get("_err"):
            _log("reap: %s finalization failed; order remains recoverable" % it["id"][:8])
            continue
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


@agent_task.serialized_lifecycle
def launch(item):
    """Spawn only while this order still owns launch; publish its PID under the same lock."""
    item = agent_task.get(item["id"])
    if (not item or agent_task.exec_state(item) != agent_task.STATE_RUNNING
            or item.get("state") == "cancelled"):
        return False
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
    logf = agent_task.private_data.open_for_write(os.path.join(d, "run.log"), "ab")
    try:
        prepared = agent_task.patch_ext(item["id"], **{agent_task.EXT_LAUNCH: "starting"})
        if prepared.get("_err"):
            raise RuntimeError("launch intent could not be persisted")
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
    registered = agent_task.record_process(item["id"], p.pid, pstart)
    if registered.get("_err"):
        agent_task.request_stop(item["id"], "process identity persistence failed")
        try:
            agent_task.kill_tree(p.pid, pstart)
        except (RuntimeError, OSError) as error:
            agent_task.append_event(item, "stop_pending", error=str(error))
            raise RuntimeError("launched process identity is unpersisted and termination is unproven") from error
        agent_task.cancel(item["id"], "process identity persistence failed")
        return False
    current = agent_task.get(item["id"])
    if agent_task.exec_state(current) == agent_task.STATE_STOPPING:
        _finish_stop(current, "stop requested during launch", post=False)
        return False
    agent_task.append_event(item, "launched", pid=p.pid, workspace=workspace)
    _log("launched %s pid=%d in %s" % (item["id"][:8], p.pid, workspace))
    return True


def _finish_stop(item, note="", post=True, never_started=False):
    ext = item.get("ext") or {}
    try:
        killed = False if never_started else agent_task.kill_tree(
            ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART))
        result = agent_task.cancel(item["id"], note or "user asked to stop")
        if result.get("_err"):
            raise RuntimeError("cancellation persistence failed: " + str(result["_err"]))
    except (RuntimeError, OSError) as error:
        agent_task.append_event(item, "stop_pending", error=str(error))
        outcome = {"id": item["id"], "killed": False, "stopped": False,
                   "status": "stop_pending", "error": str(error)}
        _log("stop pending %s: %s" % (item["id"][:8], error))
        if post:
            _post(ext.get(agent_task.EXT_STREAM) or "infra",
                  "工作单 %s 的停止请求尚未完成：%s" % (item["id"][:8], error))
        return outcome
    status = "terminated" if killed else "already_exited"
    agent_task.append_event(item, "stopped", killed=killed, status=status)
    _log("stopped %s (%s)" % (item["id"][:8], status))
    if post:
        _post(ext.get(agent_task.EXT_STREAM) or "infra",
              "已停止工作单 %s（%s）。" % (item["id"][:8], status))
    return {"id": item["id"], "killed": killed, "stopped": True, "status": status}


@agent_task.serialized_lifecycle
def stop(item_id, note="", post=True):
    """Persist stop intent before termination; cancel only once the process is gone."""
    items = agent_task.orders()
    targets = (agent_task.running(items) if item_id == "*" else
               [it for it in items if it["id"] == item_id or it["id"].startswith(item_id)])
    out = []
    for item in targets:
        was_queued = agent_task.exec_state(item) == agent_task.STATE_QUEUED
        saved = agent_task.request_stop(item["id"], note)
        if saved.get("_err"):
            out.append({"id": item["id"], "stopped": False, "status": "stop_pending",
                        "error": "stop intent could not be persisted: " + str(saved["_err"])})
            continue
        current = next((row for row in agent_task.orders() if row["id"] == item["id"]), None)
        if current is None:
            out.append({"id": item["id"], "stopped": False, "status": "stop_pending",
                        "error": "current order could not be read after stop intent"})
            continue
        current_ext = current.get("ext") or {}
        phase = current_ext.get(agent_task.EXT_LAUNCH)
        never_started = (current_ext.get(agent_task.EXT_PID) is None
                         and phase != "starting" and (was_queued or phase == "claimed"))
        out.append(_finish_stop(current, note, post=post, never_started=never_started))
    return out


@agent_task.serialized_lifecycle
def run(post=True, reap_only=False):
    items = agent_task.orders()
    reaped = reap(items, post=post)
    # Reaping can persist stop intent without completing a terminal transition.
    items = agent_task.orders()
    live = agent_task.running(items)
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
        outcomes = stop(a.stop, post=a.post)
        print(json.dumps({"stopped": outcomes}, ensure_ascii=False))
        return 0 if all(row["stopped"] for row in outcomes) else 1
    print(json.dumps(run(post=a.post, reap_only=a.reap_only), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
