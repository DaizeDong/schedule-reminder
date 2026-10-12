#!/usr/bin/env python3
"""Drain serial work: reap terminated orders, refresh durable state, then claim one.

A running or stop-pending order retains the serial slot. Process identity includes PID and creation time. Detached output goes to the PRIVATE run directory. Native survival and termination semantics require separate platform validation."""
import argparse
import datetime
import json
import os
import subprocess
import sys
from contextvars import ContextVar

_NOTIFICATION = ContextVar("work_notification", default=(None, "reconcile"))

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import agent_task  # noqa: E402
import runner_job  # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

RUNNER = os.path.join(_HERE, "agent_run.py")

_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
# No DETACHED_PROCESS: Windows ignores CREATE_NO_WINDOW when DETACHED_PROCESS is also set, so
# the runner would have NO console and every console program it starts (reminder.py through
# python.exe, gh, git, powershell) would allocate a visible window of its own. With
# CREATE_NO_WINDOW alone the runner owns one hidden console that its children inherit.
# Lifetime is unchanged: neither flag affects job membership or survival after the tick exits.
RUNNER_CREATION_FLAGS = _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW

# A just-claimed order has no pid yet: the claim and the spawn cannot be atomic. Within this window
# a missing pid means "starting", not "dead". Without it a tick could reap the run the previous tick
# had launched microseconds earlier.
CLAIM_GRACE_SECONDS = 180


def _log(msg):
    """Diagnostics go to stderr: stdout carries this module's JSON result (`--stop`, a tick run)
    and the reply of every CLI that calls into it, and a log line in front of that JSON makes
    the reply unparseable. Under pythonw both streams are None and print() drops the line."""
    print(msg, file=sys.stderr, flush=True)


def _post(stream, text, *, run_id=None, condition='reconcile'):
    try:
        import notification_client as client
        if run_id is None:
            run_id, condition = _NOTIFICATION.get()
        receipt = client.submit('schedule-reminder', run_id, 'terminal', condition, stream, text,
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


def _stop_pending_under_lock(item_id, never_started):
    """Finish a stop another process requested, unless that stop has finished it meanwhile."""
    with agent_task.launch_lock():
        it = agent_task.get(item_id)
        if not it or agent_task.exec_state(it) != agent_task.STATE_STOPPING:
            return None
        return _finish_stop(it, (it.get("ext") or {}).get(agent_task.EXT_NOTE) or "",
                            never_started=never_started)


def reap(reservations=None, post=True):
    """Reconcile dead/ambiguous generations without ever replaying an action.

    A receipt written after our liveness probe makes the snapshot CAS fail. A late child must
    win its startup CAS before doing work, so revoking an unstarted generation is safe.
    Parent identity is not descendant cleanup evidence. In-flight/unknown cleanup keeps its
    reservation after reconciliation; only the shared execution receipt can establish quiescence.
    """
    reaped = []
    if reservations is None:
        reservations = agent_task.store.work_reservations()
    for op in reservations:
        it = agent_task.get(op["item_id"])
        if it and agent_task.exec_state(it) == agent_task.STATE_STOPPING:
            result = _stop_pending_under_lock(it["id"], op["checkpoint"] == "claimed")
            if result is not None:
                _deliver(result, post)
                if result["stopped"]:
                    reaped.append(it["id"])
            continue
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
        if not agent_task.reconcile(op["item_id"], op, note):
            continue
        reaped.append(op["item_id"])
        _log("reconcile: %s pid=%s" % (op["item_id"][:8], pid))
        if post:
            _post_owner(((it or {}).get("ext") or {}).get(agent_task.EXT_STREAM) or "infra",
                  "工作单 `%s` 没有完成；执行状态待核对，不会自动重排。" % op["item_id"][:8], run_id=op['run_id'])
    return reaped


RUNNER_KILL_WAIT_SECONDS = 5
NOT_STARTED_ATTEMPTS = 3   # launches of one order whose runner ended unrun before it is blocked


class RunnerNotStarted(RuntimeError):
    """The suspended runner was ended and confirmed exited before it ran: nothing was started."""


def _spawn_runner(argv, item_id, generation, **kw):
    """Start the runner inside its own job (Windows): created suspended, adopted, then resumed, so
    nothing it starts can be born outside the job. -> process.

    The job is mandatory. A runner that cannot be adopted is ended while still suspended and the
    launch raises RunnerNotStarted, which requeues the order (bounded) instead of running it
    without containment."""
    if sys.platform != "win32":
        return subprocess.Popen(argv, **kw)
    kw["creationflags"] = kw.get("creationflags", 0) | runner_job.CREATE_SUSPENDED
    p = subprocess.Popen(argv, **kw)
    try:
        handle = getattr(p, "_handle", None)
        if handle is None:
            raise runner_job.JobUnavailable("no native process handle")
        _alive, start = agent_task.proc_identity(p.pid)
        if start is None:
            raise runner_job.JobUnavailable("runner identity unavailable")
        problem = runner_job.adopt(handle, p.pid, runner_job.name_for(item_id, generation, p.pid, start))
        if problem:
            raise runner_job.JobUnavailable(problem)
        runner_job.resume(p.pid)
    except BaseException as error:
        # Normally still suspended: it has run no instruction and started nothing, so ending it
        # ends the tree. Once its exit is confirmed the launch is known not to have happened,
        # unless a thread was already resumed (ResumeIncomplete): that stays "outcome unknown".
        try:
            p.kill()
            p.wait(timeout=RUNNER_KILL_WAIT_SECONDS)
        except BaseException:
            raise error
        if not isinstance(error, Exception) or isinstance(error, runner_job.ResumeIncomplete):
            raise
        raise RunnerNotStarted("%s: %s" % (type(error).__name__, error)) from error
    return p


def _not_started_count(item):
    try:
        return int(((item or {}).get("ext") or {}).get(agent_task.EXT_NOT_STARTED) or 0)
    except (TypeError, ValueError):
        return NOT_STARTED_ATTEMPTS


def _terminate_unregistered(item_id, generation, pid, pstart):
    """End a runner whose identity could not be recorded, through its job. Raises when unproven."""
    if pid is None or pstart is None:
        raise RuntimeError("termination identity is unavailable")
    try:
        job = runner_job.open_for(item_id, generation, [(pid, pstart)], agent_task.job_backend())
    except runner_job.JobUnavailable as error:
        if not agent_task.is_live(pid, pstart):
            return
        raise RuntimeError("runner job unavailable: %s" % error) from error
    try:
        job.terminate()
        job.confirm_empty()
    except Exception as error:
        raise RuntimeError("runner job termination unproven: %s" % error) from error
    finally:
        job.close()


def launch(item, *, generation, post_reports=True):
    """Spawn the runner detached and record (pid, creation time). Returns True on success.

    `item` is the order as this tick read it; the spawn checkpoint re-checks it inside a store
    transaction, so a stale copy cannot launch anything. The launch/stop lock covers the spawn up
    to the PID receipt, the one window a stop could otherwise fall into."""
    with agent_task.launch_lock():
        return _launch_locked(item, generation, post_reports)


def _launch_locked(item, generation, post_reports):
    if not agent_task.begin_spawn(item["id"], generation):
        return False
    ext = item.get("ext") or {}
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
    logf = agent_task.private_data.open_for_write(os.path.join(d, "run.log"), "ab")
    try:
        kw = {"creationflags": RUNNER_CREATION_FLAGS} if sys.platform == "win32" else {"start_new_session": True}
        argv = [exe, "-B", RUNNER, "--id", item["id"], "--generation", str(generation)]
        if not post_reports:
            argv.append("--no-post")
        p = _spawn_runner(argv, item["id"], generation,
                          stdin=subprocess.DEVNULL, stdout=logf, stderr=logf,
                          cwd=workspace, close_fds=True,
                          env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"), **kw)
    except RunnerNotStarted as e:
        logf.close()
        # The runner was created suspended and is confirmed gone: no instruction of it ran, so the
        # order is not ambiguous. Put it back in the queue (bounded) instead of blocking it.
        op = agent_task.operation(item["id"])
        if op and op["generation"] == generation and not op["started_at"]:
            if not agent_task.not_started(item["id"], op, ("runner not started: %s" % e)[:300],
                                          retry=_not_started_count(item) < NOT_STARTED_ATTEMPTS - 1):
                agent_task.reconcile(item["id"], op, "launch outcome unknown: RunnerNotStarted")
        agent_task.append_event(item, "launch_not_started", generation=generation, error=str(e)[:300])
        _log("launch not started: %s" % e)
        return False
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
    # The receipt is fenced: a lost generation or a stop recorded before this lock was taken makes
    # it return False, and the runner is ended here.
    if not agent_task.record_process(item["id"], p.pid, pstart, generation=generation):
        agent_task.request_stop(item["id"], "process identity persistence failed",
                                expected_generation=generation)
        try:
            _terminate_unregistered(item["id"], generation, p.pid, pstart)
        except (RuntimeError, OSError) as error:
            agent_task.append_event(item, "stop_pending", error=str(error))
            raise RuntimeError("launched process identity is unpersisted and termination is unproven") from error
        agent_task.cancel(item["id"], "process identity persistence failed", expected_generation=generation)
        return False
    agent_task.append_event(item, "launched", pid=p.pid, workspace=workspace, job=sys.platform == "win32")
    _log("launched %s pid=%d in %s" % (item["id"][:8], p.pid, workspace))
    return True


def _deliver(outcome, post):
    """Send the owner report a stop prepared, after the launch/stop lock is released."""
    report = outcome.pop("_report", None)
    if post and report:
        stream, text, run_id, condition = report
        _post_owner(stream, text, run_id=run_id, condition=condition)
    return outcome


def _finish_stop(item, note="", never_started=False, expected_generation=None):
    """Terminate, cancel and release one stop-pending order. The owner report is returned under
    `_report` for _deliver, so it is never sent while the launch/stop lock is held."""
    ext = item.get("ext") or {}
    stream = ext.get(agent_task.EXT_STREAM) or "infra"
    op = agent_task.operation(item["id"])
    if expected_generation is None:
        expected_generation = op["generation"] if op else 0
    if (op['generation'] if op else 0) != expected_generation:
        return {'id': item['id'], 'killed': False, 'stopped': False, 'status': 'stale_generation'}
    run_id = op["run_id"] if op else "work-order:" + item["id"]
    if (op and not never_started and op['checkpoint'] == 'spawning' and op['started_at'] is None
            and op['pid'] is None and op['launch_pid'] is None):
        reason = 'stop revoked an unregistered spawn; process cleanup requires reviewed evidence'
        reconciled = agent_task.store.advance_work(item['id'], op['generation'], 'stop_reconcile',
                                                   expected=op, note=reason, actor=agent_task.ACTOR)
        status = 'reconcile' if reconciled else 'stop_pending'
        return {'id': item['id'], 'killed': False, 'stopped': False, 'status': status, 'error': reason,
                '_report': (stream, reason, run_id, status)}
    job = None
    try:
        try:
            killed = False
            if op and not never_started:
                job = _open_runner_job(item, op)
                if job is not None:
                    # TerminateJobObject ends every member, including llmcall's nested jobs.
                    killed = job.terminate()
            result = agent_task.cancel(item["id"], note or "user asked to stop",
                                       expected_generation=expected_generation)
            if result.get("_err"):
                raise RuntimeError("cancellation persistence failed: " + str(result["_err"]))
        except (RuntimeError, OSError) as error:
            agent_task.append_event(item, "stop_pending", error=str(error))
            _log("stop pending %s: %s" % (item["id"][:8], error))
            return {"id": item["id"], "killed": False, "stopped": False,
                    "status": "stop_pending", "error": str(error),
                    "_report": (stream, "工作单 %s 的停止请求尚未完成：%s" % (item["id"][:8], error),
                                run_id, "stop_pending")}
        if job is not None:
            cleanup = _release_after_job_kill(item, op, job)
        elif op is None:
            cleanup = "not_applicable"
        else:
            # Nothing was terminated, so this stop has no cleanup receipt; the reaper releases the
            # reservation once the store holds a quiescent one.
            cleanup = _hold(item, "no live runner was terminated")
    finally:
        if job is not None:
            job.close()
    status = "terminated" if killed else "already_exited"
    broke_away = list(job.broke_away) if job is not None else []
    breakaway_check = job.breakaway_check if job is not None else None
    if breakaway_check not in (None, "checked"):
        # The kill itself is verified by the job count; only the report of processes outside the
        # job is missing. Released or held, the reply says that nobody looked.
        agent_task.append_event(item, "runner_breakaway_unchecked", reason=breakaway_check[:300])
        _log("stop %s: processes that left the runner job could not be checked: %s"
             % (item["id"][:8], breakaway_check))
    if broke_away:
        # Outside the job by request (CREATE_BREAKAWAY_FROM_JOB), e.g. a shared daemon. Not killed.
        agent_task.append_event(item, "runner_breakaway", pids=broke_away[:50])
        _log("stop %s: processes that left the runner job are still running: %s"
             % (item["id"][:8], broke_away[:20]))
    agent_task.append_event(item, "stopped", killed=killed, status=status, cleanup=cleanup, method="job")
    _log("stopped %s (%s, cleanup %s)" % (item["id"][:8], status, cleanup))
    outcome = {"id": item["id"], "killed": killed, "stopped": True, "status": status, "cleanup": cleanup,
               "_report": (stream, "已停止工作单 %s（%s）。" % (item["id"][:8], status), run_id, "cancelled")}
    if broke_away:
        outcome["broke_away"] = broke_away
    if breakaway_check not in (None, "checked"):
        outcome["breakaway_check"] = breakaway_check
    return outcome


def _open_runner_job(item, op):
    """The runner's own job, verified to hold the recorded runner, or None when every recorded
    runner identity has already exited (its handle no longer keeps the job's name alive). A live
    runner whose job cannot be opened raises: nothing else can end its tree with proof."""
    roots = []
    for pid, pstart in ((op["launch_pid"], op["launch_pstart"]), (op["pid"], op["pstart"])):
        if pid is not None and (pid, pstart) not in roots:
            roots.append((pid, pstart))
    try:
        if not roots or roots[0][1] is None:
            raise runner_job.JobUnavailable("no recorded launch identity")
        return runner_job.open_for(item["id"], op["generation"], roots, agent_task.job_backend())
    except Exception as error:
        if any(pstart is None or agent_task.is_live(pid, pstart) for pid, pstart in roots):
            raise RuntimeError("runner job unavailable: %s: %s" % (type(error).__name__, error)) from error
        agent_task.append_event(item, "runner_job_unavailable", reason=str(error)[:300])
        return None


def _hold(item, reason):
    agent_task.append_event(item, "cleanup_held", reason=reason[:300])
    _log("stop %s keeps its reservation: %s" % (item["id"][:8], reason))
    return "held"


def _release_after_job_kill(item, op, job):
    """Release the slot when the terminated job reports ActiveProcesses == 0: the kernel's count of
    live members, which no unrelated process can enter. Anything else holds the reservation."""
    try:
        receipt = job.confirm_empty()
    except Exception as error:
        return _hold(item, "runner job not empty: %s: %s" % (type(error).__name__, error))
    current = agent_task.operation(item["id"])
    if (current and current["generation"] == op["generation"]
            and agent_task.release_verified_cleanup(item["id"], op["generation"], receipt, current)):
        return "released"
    return _hold(item, "verified cleanup could not be committed to this reservation")


def stop(item_id, note="", post=True, *, expected_generation=None):
    """Persist stop intent before termination; cancel only once the process is gone.

    Without expected_generation, the generation current when the stop runs is the one stopped."""
    out = []
    items = agent_task.orders()
    targets = (agent_task.running(items) if item_id == "*" else
               [it for it in items if it["id"] == item_id or it["id"].startswith(item_id)])
    for item in targets:
        with agent_task.launch_lock():
            outcome = _stop_one(item, note, expected_generation)
        out.append(_deliver(outcome, post))
    return out


def _stop_one(item, note, expected_generation):
    was_queued = agent_task.exec_state(item) in (agent_task.STATE_QUEUED, "preparing")
    operation = agent_task.operation(item["id"])
    generation = expected_generation if expected_generation is not None else (
        operation["generation"] if operation else 0)
    saved = agent_task.request_stop(item["id"], note, expected_generation=generation)
    if saved.get("_err"):
        status = 'stale_generation' if saved['_err'] == 'ERR_STALE_GENERATION' else 'stop_pending'
        return {"id": item["id"], "stopped": False, "status": status,
                "error": "stop intent could not be persisted: " + str(saved["_err"])}
    # The stop-intent transaction returned the current row. A replacement generation can appear
    # after this point; the final cancellation checks the generation again.
    phase = {"claimed": "claimed", "spawning": "starting"}.get(operation["checkpoint"]) if operation else None
    never_started = ((saved.get("ext") or {}).get(agent_task.EXT_PID) is None
                     and phase != "starting" and (was_queued or phase == "claimed"))
    return _finish_stop(saved, note, never_started=never_started, expected_generation=generation)


def run(post=True, reap_only=False):
    run_id = agent_task.store.uuid7()
    # Idle fast path: one read-only query, no census, no lock.
    if not agent_task.store.work_pending():
        return {"run_id": run_id, "reaped": [], "running": 0, "queued": 0, "launched": None}
    items = agent_task.orders()
    reservations = agent_task.store.work_reservations()
    reaped = reap(reservations, post=post)
    if reaped:
        items = agent_task.orders()
        reservations = agent_task.store.work_reservations()
    q = agent_task.queued(items)
    launched = None
    # The claim transaction, not this snapshot, enforces serial workspace writes across ticks.
    if not reap_only and not reservations and not agent_task.running(items) and q:
        target = q[0]
        op = agent_task.claim(target["id"], run_id=run_id, return_operation=True)
        if op and launch(target, generation=op["generation"], post_reports=post):
            launched = target["id"]
    occupied = {op["item_id"] for op in reservations} | {item["id"] for item in agent_task.running(items)}
    return {"run_id": run_id, "reaped": reaped, "running": len(occupied),
            "queued": len(q), "launched": launched}


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
