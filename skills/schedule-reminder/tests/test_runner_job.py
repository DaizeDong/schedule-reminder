"""Real Windows processes: each runner lives in its own Job Object and a stop is verified by it.

Every process here is started by this file (a synthetic runner script, its synthetic children and
a synthetic stranger) and every survivor is terminated through a handle opened on it, so a
recycled PID can never be hit. Only taskkill aimed at this file's own runner is let through the
offline harness, so the same file also runs against code without runner jobs: there the stranger
born during the stop holds the slot (the negative control).
"""
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes as w

import pytest

import agent_task
import agent_tick
import offline_support
import runner_job
import store
from test_agent_lifecycle import pool  # noqa: F401  (fixture reuse)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects")

SLEEP = "import time; time.sleep(60)"
RUNNER_SCRIPT = r'''
import ctypes, json, os, subprocess, sys, time
from ctypes import wintypes as w
py, mode, out = sys.executable, os.environ["SYNTHETIC_RUNNER_MODE"], os.path.abspath("pids.json")
sleep = [py, "-c", "import time; time.sleep(60)"]
pids = {}
child = subprocess.Popen([py, "-c", "import subprocess, sys, time\n"
                          "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                          "print(g.pid, flush=True)\ntime.sleep(60)"], stdout=subprocess.PIPE)
pids["child"], pids["grandchild"] = child.pid, int(child.stdout.readline())
if mode == "nested":
    # The shape llmcall uses: a private kill-on-close job that allows breakaway, nested inside.
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = w.HANDLE
    k.CreateJobObjectW.argtypes = [w.LPVOID, w.LPCWSTR]
    k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    job = k.CreateJobObjectW(None, None)
    info = (ctypes.c_byte * 144)()
    ctypes.c_uint32.from_buffer(info, 16).value = 0x2000 | 0x0800   # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    assert k.SetInformationJobObject(job, 9, info, ctypes.sizeof(info)), ctypes.get_last_error()
    nested = subprocess.Popen(sleep, creationflags=0x00000004)
    assert k.AssignProcessToJobObject(job, int(nested._handle)), ctypes.get_last_error()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    assert ntdll.NtResumeProcess(int(nested._handle)) == 0
    pids["nested"] = nested.pid
if mode == "breakaway":
    try:
        pids["breakaway"] = subprocess.Popen(sleep, creationflags=0x01000000).pid
    except OSError as error:
        pids["breakaway_error"] = str(error)
with open(out + ".tmp", "w") as stream:
    json.dump(pids, stream)
os.replace(out + ".tmp", out)
time.sleep(60)
'''


class Held:
    """Handles on processes this file started: liveness and termination without PID reuse risk."""

    def __init__(self):
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k.OpenProcess.restype = w.HANDLE
        self.k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self.k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self.k.CloseHandle.argtypes = [w.HANDLE]
        self.handles = {}

    def hold(self, label, pid):
        handle = self.k.OpenProcess(0x00100000 | 0x0001 | 0x1000, False, int(pid))
        assert handle, "synthetic process %s (%s) is not openable" % (label, pid)
        self.handles[label] = (int(pid), handle)
        return int(pid)

    def alive(self, label):
        return self.k.WaitForSingleObject(self.handles[label][1], 0) == 258

    def wait_gone(self, label, seconds=5.0):
        deadline = time.monotonic() + seconds
        while self.alive(label) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not self.alive(label)

    def cleanup(self):
        for _pid, handle in self.handles.values():
            if self.k.WaitForSingleObject(handle, 0) == 258:
                self.k.TerminateProcess(handle, 1)
            self.k.CloseHandle(handle)


@pytest.fixture
def held():
    processes = Held()
    yield processes
    processes.cleanup()


@pytest.fixture
def runner(pool, tmp_path, monkeypatch, held):  # noqa: F811
    script = tmp_path / "synthetic_runner.py"
    script.write_text(RUNNER_SCRIPT, encoding="utf-8")
    monkeypatch.setattr(agent_tick, "RUNNER", str(script))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original_run = subprocess.run

    def run(argv, *args, **kwargs):
        # Real taskkill only for this file's own runner (the process-tree fallback and old code).
        command = [str(value) for value in argv] if isinstance(argv, (list, tuple)) else []
        if command[:1] == ["taskkill"] and command[-1] == str(held.handles.get("runner", (None,))[0]):
            return offline_support.ORIGINAL_RUN(argv, *args, **kwargs)
        return original_run(argv, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)

    def start(mode="tree"):
        monkeypatch.setenv("SYNTHETIC_RUNNER_MODE", mode)
        item = store.add_item("Synthetic runner job order", source=agent_task.WORK_SOURCE,
                              ext={agent_task.EXT_STATE: "queued", agent_task.EXT_WORKSPACE: str(workspace)})
        assert agent_task.claim(item["id"])
        assert agent_tick.launch(agent_task.get(item["id"]), generation=1, post_reports=False)
        op = agent_task.operation(item["id"])
        held.hold("runner", op["launch_pid"])
        # The synthetic runner does not run agent_run.py; register its start on its behalf.
        assert agent_task.start_runner(item["id"], 1, op["launch_pid"], int(op["launch_pstart"]))
        assert agent_task.child_started(item["id"], 1, {"phase": "actor"})   # in_flight: what blocks
        out = workspace / "pids.json"
        deadline = time.monotonic() + 30
        while not out.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = json.loads(out.read_text())
        for label, pid in pids.items():
            if isinstance(pid, int):
                held.hold(label, pid)
        return item["id"], pids
    return start


def stranger_during_stop(monkeypatch, held):
    """An unrelated orphan born during the stop: its parent exits at once, so its ancestry is
    unknown, which is exactly the process the tree fallback cannot tell from a runner survivor."""
    original_cancel = agent_task.cancel

    def cancel(*args, **kwargs):
        launcher = subprocess.Popen(
            [sys.executable, "-c", "import subprocess, sys\n"
             "print(subprocess.Popen([sys.executable, '-c', %r], creationflags=0x08000000).pid)" % SLEEP],
            stdout=subprocess.PIPE, text=True)
        held.hold("stranger", int(launcher.communicate(timeout=20)[0]))
        return original_cancel(*args, **kwargs)
    monkeypatch.setattr(agent_task, "cancel", cancel)


def test_stop_ends_the_tree_and_a_stranger_born_meanwhile_does_not_hold_the_slot(
        runner, held, monkeypatch, pool):  # noqa: F811
    iid, _pids = runner()
    stranger_during_stop(monkeypatch, held)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert result["stopped"] is True and result["killed"] is True
    for label in ("runner", "child", "grandchild"):
        assert held.wait_gone(label), label + " survived the stop"
    assert held.alive("stranger"), "the stop must not touch an unrelated process"
    assert result["cleanup"] == "released"
    op = agent_task.operation(iid)
    assert op["released_at"] and '"authority": "verified-job-kill"' in op["cleanup_receipt"]
    queued = store.add_item("Synthetic next order", source=agent_task.WORK_SOURCE,
                            ext={agent_task.EXT_STATE: "queued"})
    assert agent_task.claim(queued["id"])


def test_llmcall_style_nested_job_is_ended_by_the_runner_job(runner, held, pool):  # noqa: F811
    iid, pids = runner("nested")
    assert "nested" in pids
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    for label in ("runner", "child", "grandchild", "nested"):
        assert held.wait_gone(label), label + " survived the stop"
    assert result["cleanup"] == "released"


def test_a_process_that_broke_away_is_reported_and_left_running(runner, held, pool):  # noqa: F811
    iid, pids = runner("breakaway")
    if "breakaway_error" in pids:
        pytest.skip("this test process sits in a job that forbids breakaway: " + pids["breakaway_error"])
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert result.get("broke_away") == [pids["breakaway"]]
    assert held.alive("breakaway")
    assert held.wait_gone("child") and held.wait_gone("grandchild")
    assert result["cleanup"] == "released"
    receipt = json.loads(agent_task.operation(iid)["cleanup_receipt"])
    assert receipt["broke_away"] == [pids["breakaway"]]


def test_without_a_job_the_tree_fallback_still_fails_closed(runner, held, monkeypatch, pool):  # noqa: F811
    monkeypatch.setattr(runner_job, "adopt", lambda *args: "synthetic job refusal")
    iid, _pids = runner()
    stranger_during_stop(monkeypatch, held)
    result = agent_tick.stop(iid, post=False, expected_generation=1)[0]
    assert result["stopped"] is True
    assert held.wait_gone("runner") and held.wait_gone("grandchild")
    assert result["cleanup"] == "held"
    assert agent_task.operation(iid)["released_at"] is None


def test_a_job_that_does_not_hold_the_recorded_runner_is_refused(held, tmp_path):
    stray = subprocess.Popen([sys.executable, "-c", SLEEP], creationflags=0x08000000)
    held.hold("stray", stray.pid)
    _alive, start = agent_task.proc_identity(stray.pid)
    jobs = runner_job.NativeJobs()
    impostor = jobs.create(runner_job.name_for("synthetic-item", 1, stray.pid, start))
    try:
        with pytest.raises(runner_job.JobUnavailable, match="not in its job"):
            runner_job.open_for("synthetic-item", 1, [(stray.pid, str(start))], jobs)
        with pytest.raises(runner_job.JobUnavailable, match="does not exist"):
            runner_job.open_for("synthetic-item", 2, [(stray.pid, str(start))], jobs)
    finally:
        jobs.close(impostor)
