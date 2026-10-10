"""Each work runner lives in its own named Windows Job Object (stdlib ctypes only).

Why: a stop must decide whether the whole runner tree is gone before it frees the serial slot.
Tracing parent PIDs and creation times (process_tree) cannot tell a descendant whose parent
already died from an unrelated process born in the same second, so that path holds the slot on
any such stranger. Job membership is kernel state instead of inference:

  launch  The tick creates the runner SUSPENDED, creates the job
          ``Local\\schedule-reminder-runner-<item>-g<generation>-<pid>-<creation time>``, assigns
          the runner, duplicates one job handle INTO the runner (that handle keeps the name
          alive for as long as the runner lives; the tick exits right after launching) and only
          then resumes it. Every descendant is born inside the job; nothing can start before the
          assignment.
  stop    Open the job by the name derived from the recorded runner identity, verify that the
          recorded runner (PID and creation time) is a member, list the members and any live
          child that a member started outside the job (a breakaway, reported, not killed),
          TerminateJobObject, then wait until JobObjectBasicAccountingInformation reports
          ActiveProcesses == 0. That count, not a snapshot walk, is the proof of cleanup.

The job sets JOB_OBJECT_LIMIT_BREAKAWAY_OK (never the silent variant) and no kill-on-close:
llmcall nests its own kill-on-close job inside it, and a descendant that deliberately starts a
shared long-lived process (the shared MCP proxy) does so with CREATE_BREAKAWAY_FROM_JOB, which
every job in the chain must allow. Such a process is outside the job by design; the stop reports
the ones whose parent is still a member when the stop runs. One started through a helper that has
already exited cannot be attributed and is not reported.

When the job cannot be created or opened (older runs, a name collision, a runner that already
exited and took the name with it), the caller falls back to the process_tree path and its
fail-closed behaviour.
"""
import ctypes
import datetime
import sys
import time

CREATE_SUSPENDED = 0x00000004
_JOB_QUERY, _JOB_TERMINATE, _SYNCHRONIZE = 0x0004, 0x0008, 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_THREAD_SUSPEND_RESUME = 0x0002
_DUPLICATE_SAME_ACCESS = 0x00000002
_ERROR_ALREADY_EXISTS, _ERROR_FILE_NOT_FOUND, _ERROR_INVALID_PARAMETER = 183, 2, 87
_ERROR_NO_MORE_FILES = 18
_BREAKAWAY_OK = 0x00000800
_CLASS_BASIC_ACCOUNTING, _CLASS_PID_LIST, _CLASS_EXTENDED_LIMITS = 1, 3, 9
_STILL_ACTIVE = 259
_TERMINATED_BY_STOP = 1


class JobUnavailable(RuntimeError):
    """The runner job does not exist, cannot be opened or does not hold the recorded runner."""


def name_for(item_id, generation, pid, pstart):
    return "Local\\schedule-reminder-runner-%s-g%s-%s-%s" % (item_id, generation, int(pid), pstart)


class NativeJobs:
    """The only OS-facing layer; tests may replace it."""

    def __init__(self):
        if sys.platform != "win32":
            raise JobUnavailable("job objects are implemented for Windows only")
        from ctypes import wintypes as w

        class _Accounting(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
                        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                        ("TotalPageFaultCount", w.DWORD), ("TotalProcesses", w.DWORD),
                        ("ActiveProcesses", w.DWORD), ("TotalTerminatedProcesses", w.DWORD)]

        class _Limits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                        ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", w.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD),
                        ("SchedulingClass", w.DWORD)]

        class _Extended(ctypes.Structure):
            # BREAKAWAY_OK is an extended limit: the basic class rejects it (ERROR_INVALID_PARAMETER).
            _fields_ = [("Basic", _Limits), ("IoInfo", ctypes.c_ulonglong * 6),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        class _Thread(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
                        ("th32OwnerProcessID", w.DWORD), ("tpBasePri", w.LONG),
                        ("tpDeltaPri", w.LONG), ("dwFlags", w.DWORD)]
        self._w, self._accounting, self._limits, self._thread = w, _Accounting, _Extended, _Thread
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, restype, argtypes in (
                ("CreateJobObjectW", w.HANDLE, [w.LPVOID, w.LPCWSTR]),
                ("OpenJobObjectW", w.HANDLE, [w.DWORD, w.BOOL, w.LPCWSTR]),
                ("SetInformationJobObject", w.BOOL, [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]),
                ("QueryInformationJobObject", w.BOOL,
                 [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)]),
                ("AssignProcessToJobObject", w.BOOL, [w.HANDLE, w.HANDLE]),
                ("TerminateJobObject", w.BOOL, [w.HANDLE, w.UINT]),
                ("IsProcessInJob", w.BOOL, [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)]),
                ("DuplicateHandle", w.BOOL, [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE),
                                             w.DWORD, w.BOOL, w.DWORD]),
                ("GetCurrentProcess", w.HANDLE, []),
                ("OpenProcess", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
                ("GetProcessTimes", w.BOOL, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4),
                ("GetExitCodeProcess", w.BOOL, [w.HANDLE, ctypes.POINTER(w.DWORD)]),
                ("CreateToolhelp32Snapshot", w.HANDLE, [w.DWORD, w.DWORD]),
                ("Thread32First", w.BOOL, [w.HANDLE, ctypes.POINTER(_Thread)]),
                ("Thread32Next", w.BOOL, [w.HANDLE, ctypes.POINTER(_Thread)]),
                ("OpenThread", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
                ("ResumeThread", w.DWORD, [w.HANDLE]),
                ("CloseHandle", w.BOOL, [w.HANDLE])):
            function = getattr(k, name)
            function.restype, function.argtypes = restype, argtypes
        self._k = k

    def _fail(self, operation):
        raise JobUnavailable("%s failed: %s" % (operation, ctypes.WinError(ctypes.get_last_error())))

    # ---- launch side
    def create(self, name):
        handle = self._k.CreateJobObjectW(None, name)
        if not handle:
            self._fail("create job")
        if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
            self._k.CloseHandle(handle)
            raise JobUnavailable("a job named %s already exists" % name)
        try:
            limits = self._limits()
            limits.Basic.LimitFlags = _BREAKAWAY_OK
            if not self._k.SetInformationJobObject(handle, _CLASS_EXTENDED_LIMITS, ctypes.byref(limits),
                                                   ctypes.sizeof(limits)):
                self._fail("allow breakaway")
            return handle
        except BaseException:
            self._k.CloseHandle(handle)
            raise

    def assign(self, job, process_handle):
        if not self._k.AssignProcessToJobObject(job, int(process_handle)):
            self._fail("assign runner")

    def give(self, job, process_handle):
        """Duplicate a job handle into the runner: it keeps the job's name alive while the runner
        lives. The runner never uses it; it is closed when the runner exits."""
        target = self._w.HANDLE()
        if not self._k.DuplicateHandle(self._k.GetCurrentProcess(), job, int(process_handle),
                                       ctypes.byref(target), 0, False, _DUPLICATE_SAME_ACCESS):
            self._fail("hand the job to the runner")

    def resume(self, pid):
        snapshot = self._k.CreateToolhelp32Snapshot(4, 0)
        if snapshot in (None, ctypes.c_void_p(-1).value):
            self._fail("thread snapshot")
        resumed = 0
        try:
            entry = self._thread(dwSize=ctypes.sizeof(self._thread))
            found = self._k.Thread32First(snapshot, ctypes.byref(entry))
            while found:
                if entry.th32OwnerProcessID == int(pid):
                    thread = self._k.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                    if not thread:
                        self._fail("open runner thread")
                    try:
                        if self._k.ResumeThread(thread) == 0xFFFFFFFF:
                            self._fail("resume runner thread")
                    finally:
                        self._k.CloseHandle(thread)
                    resumed += 1
                found = self._k.Thread32Next(snapshot, ctypes.byref(entry))
            if ctypes.get_last_error() != _ERROR_NO_MORE_FILES:
                self._fail("enumerate threads")
        finally:
            self._k.CloseHandle(snapshot)
        if not resumed:
            raise JobUnavailable("runner %s has no thread to resume" % pid)

    # ---- stop side
    def open(self, name):
        handle = self._k.OpenJobObjectW(_JOB_QUERY | _JOB_TERMINATE | _SYNCHRONIZE, False, name)
        if handle:
            return handle
        if ctypes.get_last_error() == _ERROR_FILE_NOT_FOUND:
            raise JobUnavailable("runner job %s does not exist" % name)
        self._fail("open runner job")

    def identity(self, pid):
        """-> (handle, creation time, alive) or None when the PID does not exist."""
        handle = self._k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, int(pid))
        if not handle:
            if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
                return None
            self._fail("open process %s" % pid)
        values = [self._w.FILETIME() for _ in range(4)]
        code = self._w.DWORD()
        if (not self._k.GetProcessTimes(handle, *(ctypes.byref(v) for v in values))
                or not self._k.GetExitCodeProcess(handle, ctypes.byref(code))):
            self._k.CloseHandle(handle)
            self._fail("read process %s" % pid)
        return handle, (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime, code.value == _STILL_ACTIVE

    def contains(self, job, process_handle):
        result = self._w.BOOL()
        if not self._k.IsProcessInJob(process_handle, job, ctypes.byref(result)):
            self._fail("job membership")
        return bool(result.value)

    def pids(self, job):
        size = 1024
        while True:
            class _List(ctypes.Structure):
                _fields_ = [("Assigned", self._w.DWORD), ("Listed", self._w.DWORD),
                            ("Ids", ctypes.c_size_t * size)]
            buffer = _List()
            if self._k.QueryInformationJobObject(job, _CLASS_PID_LIST, ctypes.byref(buffer),
                                                 ctypes.sizeof(buffer), None):
                return [int(buffer.Ids[i]) for i in range(buffer.Listed)]
            if ctypes.get_last_error() != 234 or size >= 65536:  # ERROR_MORE_DATA
                self._fail("list job members")
            size *= 4

    def active(self, job):
        info = self._accounting()
        if not self._k.QueryInformationJobObject(job, _CLASS_BASIC_ACCOUNTING, ctypes.byref(info),
                                                 ctypes.sizeof(info), None):
            self._fail("job accounting")
        return int(info.ActiveProcesses)

    def terminate(self, job):
        if not self._k.TerminateJobObject(job, _TERMINATED_BY_STOP):
            self._fail("terminate runner job")

    def close(self, handle):
        if handle:
            self._k.CloseHandle(handle)


def adopt(process_handle, pid, name, jobs=None):
    """Put a SUSPENDED runner into its own job and hand it the handle that keeps the name alive.
    -> None on success, else the reason the runner runs without a job. Never resumes."""
    try:
        jobs = jobs or NativeJobs()
        job = jobs.create(name)
    except Exception as error:
        return "%s: %s" % (type(error).__name__, error)
    try:
        jobs.assign(job, process_handle)
        jobs.give(job, process_handle)
        return None
    except Exception as error:
        return "%s: %s" % (type(error).__name__, error)
    finally:
        jobs.close(job)


def resume(pid, jobs=None):
    (jobs or NativeJobs()).resume(pid)


class RunnerJob:
    """An opened runner job whose recorded runner identities are verified members."""

    def __init__(self, jobs, handle, name, roots):
        self.jobs, self.handle, self.name, self.roots = jobs, handle, name, roots
        self.members, self.broke_away = [], []

    def close(self):
        if self.handle:
            self.jobs.close(self.handle)
            self.handle = None

    def _breakaways(self, table):
        """Live non-members whose parent is a live member and that were born after that parent."""
        members, created, found = set(self.members), {}, []
        handles = []
        try:
            for pid in members:
                facts = self.jobs.identity(pid)
                if facts:
                    handles.append(facts[0])
                    created[pid] = facts[1]
            for pid, ppid in table.items():
                if pid in members or ppid not in created or pid == ppid:
                    continue
                facts = self.jobs.identity(pid)
                if not facts:
                    continue
                handles.append(facts[0])
                if facts[2] and facts[1] >= created[ppid] and not self.jobs.contains(self.handle, facts[0]):
                    found.append(pid)
        finally:
            for handle in handles:
                self.jobs.close(handle)
        return sorted(found)

    def terminate(self, snapshot=None):
        """Record members and breakaways, then terminate every member. Raises on failure."""
        self.members = sorted(self.jobs.pids(self.handle))
        if snapshot is not None:
            try:
                self.broke_away = self._breakaways(snapshot())
            except Exception:
                self.broke_away = []   # reporting is advisory; it never blocks the stop
        self.jobs.terminate(self.handle)
        return True

    def confirm_empty(self, *, timeout=5.0, clock=time.monotonic, sleep=time.sleep):
        deadline = clock() + timeout
        while True:
            active = self.jobs.active(self.handle)
            if active == 0:
                break
            if clock() >= deadline:
                raise JobUnavailable("%d process(es) still in the runner job after termination" % active)
            sleep(0.05)
        return {"authority": "verified-job-kill", "job": self.name,
                "roots": [[pid, pstart] for pid, pstart in self.roots],
                "members": [[pid, ""] for pid in self.members] or [[pid, pstart] for pid, pstart in self.roots],
                "broke_away": list(self.broke_away),
                "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}


def open_for(item_id, generation, roots, jobs=None):
    """Open the job of the runner launched as roots[0] = (launch pid, launch creation time) and
    verify that every recorded identity that is still alive is a member, and that the launch
    identity is alive. Raises JobUnavailable otherwise."""
    if not roots or roots[0][0] is None or roots[0][1] is None:
        raise JobUnavailable("no recorded launch identity")
    jobs = jobs or NativeJobs()
    name = name_for(item_id, generation, roots[0][0], roots[0][1])
    handle = jobs.open(name)
    job = RunnerJob(jobs, handle, name, [(int(p), str(s)) for p, s in roots])
    try:
        for index, (pid, pstart) in enumerate(job.roots):
            facts = jobs.identity(pid)
            if facts is None:
                if index == 0:
                    raise JobUnavailable("recorded runner %s has exited" % pid)
                continue
            process, created, alive = facts
            try:
                if str(created) != pstart or not alive:
                    if index == 0:
                        raise JobUnavailable("recorded runner %s is no longer the launched process" % pid)
                    continue
                if not jobs.contains(handle, process):
                    raise JobUnavailable("recorded runner %s is not in its job" % pid)
            finally:
                jobs.close(process)
        return job
    except BaseException:
        job.close()
        raise
