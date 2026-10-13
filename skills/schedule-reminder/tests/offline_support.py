"""Test-only transport adapters. Never used by the shipped runtime."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import types

ACTIVE = True
INSTALLED = False
REAL_RUN = subprocess.run
ROOT = Path(__file__).resolve().parents[3]
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()


ORIGINAL_RUN = None  # the interpreter's subprocess.run, kept for tests that need a real child


def install():
    global INSTALLED, ORIGINAL_RUN
    if INSTALLED:
        return
    INSTALLED = True
    sys.path.insert(0, str(ROOT/'skills/schedule-reminder/scripts'))
    if os.environ.get('SCHEDULE_TEST_TRACE'):
        import faulthandler
        faulthandler.dump_traceback_later(20)
    module = types.ModuleType('llmcall')
    def blocked(*args, **kwargs):
        raise AssertionError('external transport blocked in offline tests')
    module.call = module.call_chain = blocked
    module.active_chain = lambda: ['synthetic-route']
    sys.modules['llmcall'] = module
    socket.socket.connect = socket.socket.connect_ex = blocked
    original_run = ORIGINAL_RUN = subprocess.run

    def run(argv, *args, **kwargs):
        command = [str(value) for value in argv] if isinstance(argv, (list, tuple)) else []
        if command and Path(command[0]).stem.lower() in ('schtasks', 'taskkill', 'gh'):
            return subprocess.CompletedProcess(argv, 1, '', 'platform action blocked in offline tests')
        return original_run(argv, *args, **kwargs)
    subprocess.run = run

    class Popen(subprocess.Popen):
        """A detached work tick would outlive the test that woke it; tests drive ticks directly."""
        def __init__(self, argv, *args, **kwargs):
            command = [str(value) for value in argv] if isinstance(argv, (list, tuple)) else []
            if command and Path(command[-1]).name == 'agent_tick.py':
                raise OSError('detached work tick blocked in offline tests')
            super().__init__(argv, *args, **kwargs)
    subprocess.Popen = Popen

    def audit(event, args):
        if not ACTIVE:
            return
        if event == 'open' and not isinstance(args[0], int):
            path = Path(args[0])
            if path.name.lower() == 'agent-center.md':
                raise AssertionError('excluded documentation must not be read')
            if str(path).lower() in ('nul', os.devnull.lower(), '\\\\.\\nul'):
                return
            mode = args[1] or ''
            flags = args[2] or 0
            if any(value in mode for value in 'wax+') or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
                if not path.resolve().is_relative_to(TEMP_ROOT):
                    raise AssertionError('offline write outside temporary storage: '+str(path))
    sys.addaudithook(audit)
