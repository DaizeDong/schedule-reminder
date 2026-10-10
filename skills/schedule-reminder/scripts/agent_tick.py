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
import relay       # noqa: E402
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


def log_tail(item, lines=18):
    try:
        p = os.path.join(agent_task.run_dir(item), "run.log")
        with open(p, encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        return ""


@agent_task.serialized_lifecycle
def _reap_legacy(items=None, post=True):
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
            _post_owner(ext.get(agent_task.EXT_STREAM) or "infra", "\n".join([
                "⛔ 工作单 `%s` 的执行进程没了(pid=%s),**任务没有完成**,不会自动重排。"
                % (it["id"][:8], pid),
                "标题:%s" % (it.get("title") or "")[:120],
                ("日志末尾:\n```\n%s\n```" % tail[:800]) if tail else "(没有日志可读)",
            ]), run_id='work-order:' + it['id'], condition='reconcile')
    return reaped


@agent_task.serialized_lifecycle
def reap(items=None, post=True):
    """Reconcile dead/ambiguous generations without ever replaying an action.

    A receipt written after our liveness probe makes the snapshot CAS fail. A late child must
    win its startup CAS before doing work, so revoking an unstarted generation is safe.
    Parent identity is not descendant cleanup evidence. In-flight/unknown cleanup keeps its
    reservation after reconciliation; only the shared execution receipt can establish quiescence.
    """
    reaped = []
    reservations = agent_task.store.work_reservations()
    known = {op["item_id"] for op in reservations}
    legacy = [it for it in (agent_task.orders() if items is None else items) if it["id"] not in known]
    reaped.extend(_reap_legacy(legacy, post=post))
    for op in reservations:
        it = agent_task.get(op["item_id"])
        if it and agent_task.exec_state(it) == agent_task.STATE_STOPPING:
            result = _finish_stop(it, (it.get("ext") or {}).get(agent_task.EXT_NOTE) or "", post=post,
                                  never_started=op["checkpoint"] == "claimed")
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
        if not agent_task.reconcile(it["id"], op, note):
            continue
        reaped.append(it["id"])
        _log("reconcile: %s pid=%s" % (it["id"][:8], pid))
        if post:
            _post_owner((it.get("ext") or {}).get(agent_task.EXT_STREAM) or "infra",
                  "工作单 `%s` 没有完成；执行状态待核对，不会自动重排。" % it["id"][:8], run_id=op['run_id'])
    return reaped


RUNNER_KILL_WAIT_SECONDS = 5
NOT_STARTED_ATTEMPTS = 3   # launches of one order whose runner ended unrun before it is blocked


class RunnerNotStarted(RuntimeError):
    """The suspended runner was ended and confirmed exited before it ran: nothing was started."""


def _spawn_runner(argv, item_id, generation, **kw):
    """Start the runner inside its own job (Windows): created suspended, adopted, then resumed, so
    nothing it starts can be born outside the job. -> (process, None | why there is no job).
    A job failure never blocks the launch; the stop then uses the process-tree fallback."""
    if sys.platform != "win32":
        return subprocess.Popen(argv, **kw), "not Windows"
    kw["creationflags"] = kw.get("creationflags", 0) | runner_job.CREATE_SUSPENDED
    p = subprocess.Popen(argv, **kw)
    handle = getattr(p, "_handle", None)
    if handle is None:
        return p, "no native process handle"
    try:
        _alive, start = agent_task.proc_identity(p.pid)
        problem = ("runner identity unavailable" if start is None else
                   runner_job.adopt(handle, p.pid, runner_job.name_for(item_id, generation, p.pid, start)))
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
    return p, problem


def _not_started_count(item):
    try:
        return int(((item or {}).get("ext") or {}).get(agent_task.EXT_NOT_STARTED) or 0)
    except (TypeError, ValueError):
        return NOT_STARTED_ATTEMPTS


@agent_task.serialized_lifecycle
def launch(item, *, generation=None, post_reports=True):
    """Spawn the runner detached and record (pid, creation time). Returns True on success."""
    item = agent_task.get(item["id"])
    if not item or agent_task.exec_state(item) != agent_task.STATE_RUNNING:
        return False
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
    logf = agent_task.private_data.open_for_write(os.path.join(d, "run.log"), "ab")
    try:
        flags = 0
        if sys.platform == "win32":
            flags = RUNNER_CREATION_FLAGS
        kw = {"creationflags": flags} if sys.platform == "win32" else {"start_new_session": True}
        argv = [exe, "-B", RUNNER, "--id", item["id"], "--generation", str(generation)]
        if not post_reports:
            argv.append("--no-post")
        p, job_problem = _spawn_runner(argv, item["id"], generation,
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
    registered = agent_task.record_process(item["id"], p.pid, pstart, generation=generation)
    if not registered or registered.get("_err"):
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
    if job_problem:
        agent_task.append_event(item, "runner_job_unavailable", reason=job_problem[:300])
        _log("runner %s runs without its own job: %s" % (item["id"][:8], job_problem))
    agent_task.append_event(item, "launched", pid=p.pid, workspace=workspace, job=not job_problem)
    _log("launched %s pid=%d in %s" % (item["id"][:8], p.pid, workspace))
    return True


def _finish_stop(item, note="", post=True, never_started=False, expected_generation=None):
    ext = item.get("ext") or {}
    op = agent_task.operation(item["id"])
    if expected_generation is None and op:
        expected_generation = ext.get(agent_task.EXT_GENERATION)
    if expected_generation is not None and (op['generation'] if op else 0) != expected_generation:
        return {'id': item['id'], 'killed': False, 'stopped': False, 'status': 'stale_generation'}
    run_id = op["run_id"] if op else "work-order:" + item["id"]
    if (op and not never_started and op['checkpoint'] == 'spawning' and op['started_at'] is None
            and op['pid'] is None and op['launch_pid'] is None):
        reason = 'stop revoked an unregistered spawn; process cleanup requires reviewed evidence'
        reconciled = agent_task.store.advance_work(item['id'], op['generation'], 'stop_reconcile',
                                                   expected=op, note=reason, actor=agent_task.ACTOR)
        status = 'reconcile' if reconciled else 'stop_pending'
        if post:
            _post_owner(ext.get(agent_task.EXT_STREAM) or 'infra', reason,
                        run_id=run_id, condition=status)
        return {'id': item['id'], 'killed': False, 'stopped': False, 'status': status, 'error': reason}
    observation, observe_error, job = None, None, None
    if op and not never_started:
        job, job_error = _open_runner_job(item, op)
        if job is None:
            if job_error:
                agent_task.append_event(item, "runner_job_unavailable", reason=job_error[:300])
            observation, observe_error = _observe_runner_tree(op)
    try:
        try:
            if never_started:
                killed = False
            elif job is not None:
                # TerminateJobObject ends every member, including llmcall's nested jobs.
                killed = job.terminate(snapshot=lambda: agent_task.process_backend().snapshot())
            else:
                killed = agent_task.kill_tree(ext.get(agent_task.EXT_PID), ext.get(agent_task.EXT_PSTART))
            result = (agent_task.cancel(item["id"], note or "user asked to stop") if expected_generation is None else
                      agent_task.cancel(item["id"], note or "user asked to stop", expected_generation=expected_generation))
            if result.get("_err"):
                raise RuntimeError("cancellation persistence failed: " + str(result["_err"]))
        except (RuntimeError, OSError) as error:
            agent_task.append_event(item, "stop_pending", error=str(error))
            outcome = {"id": item["id"], "killed": False, "stopped": False,
                       "status": "stop_pending", "error": str(error)}
            _log("stop pending %s: %s" % (item["id"][:8], error))
            if post:
                _post_owner(ext.get(agent_task.EXT_STREAM) or "infra",
                      "工作单 %s 的停止请求尚未完成：%s" % (item["id"][:8], error), run_id=run_id, condition="stop_pending")
            return outcome
        if job is not None:
            cleanup = _release_after_job_kill(item, op, job)
        else:
            cleanup = _release_after_verified_kill(item, op, observation, observe_error, killed)
    finally:
        if observation is not None:
            observation.close()
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
    agent_task.append_event(item, "stopped", killed=killed, status=status, cleanup=cleanup,
                            method="job" if job is not None else "tree")
    _log("stopped %s (%s, cleanup %s)" % (item["id"][:8], status, cleanup))
    if post:
        _post_owner(ext.get(agent_task.EXT_STREAM) or "infra",
              "已停止工作单 %s（%s）。" % (item["id"][:8], status), run_id=run_id, condition="cancelled")
    outcome = {"id": item["id"], "killed": killed, "stopped": True, "status": status, "cleanup": cleanup}
    if broke_away:
        outcome["broke_away"] = broke_away
    if breakaway_check not in (None, "checked"):
        outcome["breakaway_check"] = breakaway_check
    return outcome


def _open_runner_job(item, op):
    """The runner's own job, verified to hold the recorded runner. -> (job|None, reason|None).
    A missing job is the normal state for runs launched before jobs existed and for a runner that
    already exited (its handle kept the job's name alive); the caller falls back."""
    if op["launch_pid"] is None or op["launch_pstart"] is None:
        return None, None
    roots = [(op["launch_pid"], op["launch_pstart"])]
    if op["pid"] is not None and (op["pid"], op["pstart"]) not in roots:
        roots.append((op["pid"], op["pstart"]))
    try:
        return runner_job.open_for(item["id"], op["generation"], roots, agent_task.job_backend()), None
    except Exception as error:
        return None, "%s: %s" % (type(error).__name__, error)


def _release_after_job_kill(item, op, job):
    """Release the slot when the terminated job reports ActiveProcesses == 0: the kernel's count of
    live members, which no unrelated process can enter. Anything else holds the reservation."""
    try:
        receipt = job.confirm_empty()
    except Exception as error:
        reason = "runner job not empty: %s: %s" % (type(error).__name__, error)
    else:
        current = agent_task.operation(item["id"])
        if (current and current["generation"] == op["generation"]
                and agent_task.release_verified_cleanup(item["id"], op["generation"], receipt, current)):
            return "released"
        reason = "verified cleanup could not be committed to this reservation"
    agent_task.append_event(item, "cleanup_held", reason=reason[:300])
    _log("stop %s keeps its reservation: %s" % (item["id"][:8], reason))
    return "held"


def _observe_runner_tree(op):
    """Record the runner tree before termination. Failure never blocks the stop itself; it only
    means the reservation cannot be released automatically. -> (observation|None, error|None)"""
    roots = []
    for pid, pstart in ((op["launch_pid"], op["launch_pstart"]), (op["pid"], op["pstart"])):
        if pid is not None and (pid, pstart) not in roots:
            if pstart is None:
                return None, "recorded runner identity has no creation time"
            roots.append((pid, pstart))
    if not roots:
        return None, "no recorded runner identity"
    try:
        return agent_task.process_tree.observe(roots, agent_task.process_backend()), None
    except Exception as error:  # observation is advisory to the kill; any failure holds the slot
        return None, "%s: %s" % (type(error).__name__, error)


def _release_after_verified_kill(item, op, observation, observe_error, killed):
    """Release the serial slot only when the kill was verified AND a fresh snapshot confirms that
    every recorded member and every later descendant is gone. The parent's exit alone, a failed
    snapshot or any survivor keeps the reservation for reconcile / recover-cleanup.
    -> "released" | "held" | "not_applicable"."""
    if op is None:
        return "not_applicable"
    reason = None
    if not killed:
        reason = "runner had already exited; its tree was never recorded"
    elif observation is None:
        reason = "runner tree was not recorded before termination: %s" % observe_error
    else:
        try:
            receipt = agent_task.process_tree.confirm_gone(observation)
        except Exception as error:
            reason = "cleanup unconfirmed: %s: %s" % (type(error).__name__, error)
        else:
            current = agent_task.operation(item["id"])
            if (current and current["generation"] == op["generation"]
                    and agent_task.release_verified_cleanup(item["id"], op["generation"], receipt, current)):
                return "released"
            reason = "verified cleanup could not be committed to this reservation"
    agent_task.append_event(item, "cleanup_held", reason=reason[:300])
    _log("stop %s keeps its reservation: %s" % (item["id"][:8], reason))
    return "held"


@agent_task.serialized_lifecycle
def stop(item_id, note="", post=True, *, expected_generation=None):
    """Persist stop intent before termination; cancel only once the process is gone."""
    items = agent_task.orders()
    targets = (agent_task.running(items) if item_id == "*" else
               [it for it in items if it["id"] == item_id or it["id"].startswith(item_id)])
    out = []
    for item in targets:
        was_queued = agent_task.exec_state(item) in (agent_task.STATE_QUEUED, "preparing")
        saved = (agent_task.request_stop(item["id"], note) if expected_generation is None else
                 agent_task.request_stop(item["id"], note, expected_generation=expected_generation))
        if saved.get("_err"):
            status = 'stale_generation' if saved['_err'] == 'ERR_STALE_GENERATION' else 'stop_pending'
            out.append({"id": item["id"], "stopped": False, "status": status,
                        "error": "stop intent could not be persisted: " + str(saved["_err"])})
            continue
        # Bound requests keep the exact PID/start-time snapshot from the stop-intent transaction.
        # A replacement generation can appear after this point; final cancellation checks again.
        current = (saved if 'id' in saved else saved.get('item')) if expected_generation is not None else next(
            (row for row in agent_task.orders() if row["id"] == item["id"]), None)
        if current is None:
            out.append({"id": item["id"], "stopped": False, "status": "stop_pending",
                        "error": "current order could not be read after stop intent"})
            continue
        current_ext = current.get("ext") or {}
        operation = agent_task.operation(item["id"])
        phase = current_ext.get(agent_task.EXT_LAUNCH)
        if operation:
            phase = {"claimed": "claimed", "spawning": "starting"}.get(operation["checkpoint"], phase)
        never_started = (current_ext.get(agent_task.EXT_PID) is None
                         and phase != "starting" and (was_queued or phase == "claimed"))
        out.append(_finish_stop(current, note, post=post, never_started=never_started,
                                expected_generation=expected_generation))
    return out


@agent_task.serialized_lifecycle
def run(post=True, reap_only=False):
    run_id = agent_task.store.uuid7()
    items = agent_task.orders()
    reaped = reap(items, post=post)
    items = agent_task.orders()
    reservations = agent_task.store.work_reservations()
    q = agent_task.queued(items)
    launched = None
    # The transaction, not this snapshot, enforces serial workspace writes across ticks.
    if not reap_only and not reservations and not agent_task.running(items) and q:
        target = q[0]
        op = agent_task.claim(target["id"], run_id=run_id, return_operation=True)
        if op and launch(agent_task.get(target["id"]), generation=op["generation"], post_reports=post):
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
