"""Verified process-tree cleanup for a stopped work order (Windows, stdlib only).

A stop ends the runner with `taskkill /T /F`. The parent's exit alone never proves that its
descendants are gone, so a stop used to leave the serial slot reserved until an operator ran
`recover-cleanup`. This module lets the stop prove the stronger fact itself:

  1. observe(): BEFORE the kill, note the time and record the runner's tree. Each recorded member
     keeps an open SYNCHRONIZE handle, so its later exit is read from that handle and a recycled
     PID can never impersonate it. A child is attributed to its parent only when it was created
     inside the parent's lifetime; a bare PPID match is not ancestry.
  2. confirm_gone(): AFTER the kill, every recorded handle must be signalled, and a FRESH snapshot
     must contain (a) no live process whose ancestry runs through a recorded member, and (b) no
     live process created after step 1 whose ancestry is unknown: its parent PID is absent from
     the snapshot or now belongs to a newer process. (b) is what catches a survivor whose own,
     never-recorded parent was started after step 1 and died before the fresh snapshot (a
     `cmd /c start` launcher, or an intermediate taskkill killed while the child it had just
     spawned lived). (b) can hold the slot because of an unrelated orphan born in those few
     seconds; that is the safe direction.

Any failure to observe or confirm raises TreeUncertain, and the caller keeps the reservation.
Residual limit, stated rather than hidden: a descendant whose own parent had already exited
BEFORE step 1 was an orphan at recording time. It has no PPID link to the runner and is invisible
to taskkill /T and to this check. llmcall's kill-on-close Job Object does not close that gap
either: the job allows breakaway (JOB_OBJECT_LIMIT_BREAKAWAY_OK), so a descendant created with
CREATE_BREAKAWAY_FROM_JOB leaves it. Processes this user cannot open (another user's or a
protected process) are treated as not ours: the runner's descendants run under the runner's
token and are always openable for query.
"""
import ctypes
import datetime
import sys
import time

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_INVALID_PARAMETER = 87  # OpenProcess on a PID that does not exist
_ERROR_ACCESS_DENIED = 5
_MARGIN = 10_000_000  # 1 s in FILETIME units: the observation time is taken early, never late
_WAIT_OBJECT_0, _WAIT_TIMEOUT = 0, 258


class TreeUncertain(RuntimeError):
    """The tree could not be recorded or its cleanup could not be confirmed."""


class NativeProcesses:
    """The only OS-facing layer; tests replace it with a synthetic process table."""

    def __init__(self):
        if sys.platform != "win32":
            raise TreeUncertain("process-tree observation is implemented for Windows only")
        from ctypes import wintypes as w

        class _Entry(ctypes.Structure):
            _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD),
                        ("th32ProcessID", w.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", w.DWORD), ("cntThreads", w.DWORD),
                        ("th32ParentProcessID", w.DWORD), ("pcPriClassBase", w.LONG),
                        ("dwFlags", w.DWORD), ("szExeFile", w.WCHAR * 260)]
        self._entry, self._w = _Entry, w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, restype, argtypes in (
                ("CreateToolhelp32Snapshot", w.HANDLE, [w.DWORD, w.DWORD]),
                ("Process32FirstW", w.BOOL, [w.HANDLE, ctypes.POINTER(_Entry)]),
                ("Process32NextW", w.BOOL, [w.HANDLE, ctypes.POINTER(_Entry)]),
                ("OpenProcess", w.HANDLE, [w.DWORD, w.BOOL, w.DWORD]),
                ("GetProcessTimes", w.BOOL, [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4),
                ("WaitForSingleObject", w.DWORD, [w.HANDLE, w.DWORD]),
                ("GetSystemTimePreciseAsFileTime", None, [ctypes.POINTER(w.FILETIME)]),
                ("CloseHandle", w.BOOL, [w.HANDLE])):
            function = getattr(k, name)
            function.restype, function.argtypes = restype, argtypes
        self._k = k

    def _fail(self, operation):
        raise TreeUncertain("process observation failed (%s): %s"
                            % (operation, ctypes.WinError(ctypes.get_last_error())))

    def snapshot(self):
        """-> {pid: parent_pid} for every process in one native snapshot."""
        handle = self._k.CreateToolhelp32Snapshot(2, 0)
        if handle in (None, ctypes.c_void_p(-1).value):
            self._fail("snapshot")
        try:
            entry = self._entry(dwSize=ctypes.sizeof(self._entry))
            found = self._k.Process32FirstW(handle, ctypes.byref(entry))
            table = {}
            while found:
                table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                found = self._k.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES is the only clean end
                self._fail("enumerate")
            return table
        finally:
            self._k.CloseHandle(handle)

    def open(self, pid, foreign_ok=False):
        """-> handle, or None when the PID no longer exists (or, with foreign_ok, when this user may
        not open it). Any other refusal is uncertainty."""
        handle = self._k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if handle:
            return handle
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER or (foreign_ok and error == _ERROR_ACCESS_DENIED):
            return None
        self._fail("open pid %s" % pid)

    def now(self):
        """Current time in the creation-time domain (FILETIME), minus a safety margin."""
        value = self._w.FILETIME()
        self._k.GetSystemTimePreciseAsFileTime(ctypes.byref(value))
        return ((value.dwHighDateTime << 32) | value.dwLowDateTime) - _MARGIN

    def created(self, handle):
        values = [self._w.FILETIME() for _ in range(4)]
        if not self._k.GetProcessTimes(handle, *(ctypes.byref(v) for v in values)):
            self._fail("process times")
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def alive(self, handle):
        state = self._k.WaitForSingleObject(handle, 0)
        if state == _WAIT_TIMEOUT:
            return True
        if state == _WAIT_OBJECT_0:
            return False
        self._fail("wait")

    def close(self, handle):
        self._k.CloseHandle(handle)


class TreeObservation:
    """Recorded members: pid -> (creation time, retained handle). Close it in every path."""

    def __init__(self, backend, roots):
        self.backend, self.roots, self.members, self.observed_at = backend, roots, {}, None

    def close(self):
        for _created, handle in self.members.values():
            try:
                self.backend.close(handle)
            except Exception:
                pass
        self.members.clear()


def observe(roots, backend):
    """Record every live root identity and its descendants before termination.

    roots is [(pid, recorded creation time)]. A root that is gone or has a different creation time
    is uncertainty, not success: the tree it had can no longer be recorded."""
    obs = TreeObservation(backend, [(int(p), str(s)) for p, s in roots])
    try:
        # Taken before anything is read: every process born after it is checked for unknown
        # ancestry after the kill.
        obs.observed_at = backend.now()
        if not obs.roots:
            raise TreeUncertain("no recorded runner identity")
        for pid, pstart in obs.roots:
            if pid in obs.members:
                continue
            handle = backend.open(pid)
            if handle is None:
                raise TreeUncertain("runner identity %s exited before its tree was recorded" % pid)
            obs.members[pid] = (backend.created(handle), handle)
            if str(obs.members[pid][0]) != pstart or not backend.alive(handle):
                raise TreeUncertain("runner identity %s no longer matches the recorded process" % pid)
        table = backend.snapshot()
        pending = list(obs.members)
        while pending:
            parent = pending.pop()
            parent_created = obs.members[parent][0]
            for pid, ppid in table.items():
                if ppid != parent or pid in obs.members or pid == parent:
                    continue
                handle = backend.open(pid)
                if handle is None:
                    if pid in table.values():
                        raise TreeUncertain("intermediate descendant %s exited during observation" % pid)
                    continue
                created = backend.created(handle)
                if created < parent_created:
                    # Its parent PID was recycled by our member; it is not our descendant.
                    backend.close(handle)
                    continue
                obs.members[pid] = (created, handle)
                pending.append(pid)
        return obs
    except BaseException:
        obs.close()
        raise


def _late_orphans(obs, table, attributed):
    """Live processes created after the recording whose parent cannot be identified: the parent PID
    is missing from the fresh snapshot, belongs to a newer process, or cannot be read. Such a
    process may descend from the runner through an intermediate that died unrecorded."""
    backend, found, created_of = obs.backend, [], {}
    if obs.observed_at is None:
        raise TreeUncertain("the recording has no observation time")

    def facts(pid):
        if pid not in created_of:
            handle = backend.open(pid, foreign_ok=True)
            if handle is None:
                created_of[pid] = None
            else:
                try:
                    created_of[pid] = (backend.created(handle), backend.alive(handle))
                finally:
                    backend.close(handle)
        return created_of[pid]

    for pid, ppid in table.items():
        if pid in obs.members or pid in attributed or pid == ppid:
            continue
        own = facts(pid)
        if own is None or own[0] < obs.observed_at or not own[1]:
            continue  # gone, not ours to open, or older than the recording
        parent = facts(ppid) if ppid in table else None
        if parent is None or parent[0] > own[0]:
            found.append(pid)
    return sorted(found)


def confirm_gone(obs, *, timeout=3.0, clock=time.monotonic, sleep=time.sleep):
    """Return a cleanup receipt once every recorded member and every later descendant is gone."""
    backend = obs.backend
    deadline = clock() + timeout
    while True:
        alive = sorted(pid for pid, (_c, handle) in obs.members.items() if backend.alive(handle))
        if not alive:
            break
        if clock() >= deadline:
            raise TreeUncertain("recorded descendants survived termination: %s" % alive[:20])
        sleep(0.05)
    table = backend.snapshot()
    # A dead member's PID may already belong to an unrelated process; children created after that
    # reuse belong to the newcomer. window[pid] = (member creation, reuse creation or infinity).
    window, frontier, seen = {}, [], set()
    for pid, (created, _handle) in obs.members.items():
        upper = float("inf")
        if pid in table:
            reused = backend.open(pid)
            if reused is not None:
                try:
                    reuse_created = backend.created(reused)
                finally:
                    backend.close(reused)
                # Only a strictly newer process is a reuse; the member itself still listed (or a
                # nonsensical older time) leaves the window open, which can only hold the slot.
                if reuse_created > created:
                    upper = reuse_created
        window[pid] = (created, upper)
        frontier.append(pid)
    survivors = []
    while frontier:
        parent = frontier.pop()
        lower, upper = window[parent]
        for pid, ppid in table.items():
            if ppid != parent or pid in seen or pid == parent or pid in obs.members:
                continue
            handle = backend.open(pid)
            if handle is None:
                if pid in table.values():
                    raise TreeUncertain("descendant %s exited before its children were attributed" % pid)
                continue
            try:
                created, live = backend.created(handle), backend.alive(handle)
            finally:
                backend.close(handle)
            if not lower <= created < upper:
                continue
            seen.add(pid)
            if live:
                survivors.append(pid)
            window[pid] = (created, float("inf"))
            frontier.append(pid)
    if survivors:
        raise TreeUncertain("descendants started by the runner tree survived: %s" % sorted(survivors)[:20])
    orphans = _late_orphans(obs, table, seen)
    if orphans:
        raise TreeUncertain("processes of unknown ancestry started after the recording are alive: %s"
                            % orphans[:20])
    return {"authority": "verified-tree-kill",
            "roots": [[pid, pstart] for pid, pstart in obs.roots],
            "members": sorted([pid, str(created)] for pid, (created, _h) in obs.members.items()),
            "fresh_snapshot_processes": len(table),
            "confirmed_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
