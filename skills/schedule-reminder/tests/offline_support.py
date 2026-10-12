"""Test-only transport adapters. Never used by the shipped runtime."""
import configparser
import json
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
FIX = json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))


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
        if command and Path(command[0]).stem.lower() == 'git' and '-C' in command:
            location = Path(command[command.index('-C')+1]).resolve()
            if location.is_relative_to(TEMP_ROOT):
                for root in (location, *location.parents):
                    if not root.is_relative_to(TEMP_ROOT):
                        break
                    metadata = root/'.git'
                    if not metadata.exists():
                        continue
                    if metadata.is_file():
                        metadata = (root/metadata.read_text(encoding='utf-8').split(':', 1)[1].strip()).resolve()
                        if not metadata.is_relative_to(TEMP_ROOT):
                            return subprocess.CompletedProcess(argv, 128, '', 'metadata outside synthetic storage')
                        common = metadata/'commondir'
                        if common.exists():
                            metadata = (metadata/common.read_text(encoding='utf-8').strip()).resolve()
                    if not metadata.is_relative_to(TEMP_ROOT):
                        return subprocess.CompletedProcess(argv, 128, '', 'metadata outside synthetic storage')
                    parser = configparser.ConfigParser()
                    parser.read(metadata/'config', encoding='utf-8')
                    tail = command[command.index('-C')+2:]
                    if tail == ['rev-parse', '--show-toplevel']:
                        return subprocess.CompletedProcess(argv, 0, str(root), '')
                    if tail == ['remote']:
                        names = [section[8:-1] for section in parser.sections() if section.startswith('remote "') and section.endswith('"')]
                        return subprocess.CompletedProcess(argv, 0, "\n".join(names), '')
                    if tail == ['config', '--null', '--list']:
                        entries = []
                        for section in parser.sections():
                            prefix = section.replace(' "', '.').rstrip('"')
                            for key, value in parser.items(section):
                                entries.append(prefix+'.'+key+'\n'+value+'\0')
                        return subprocess.CompletedProcess(argv, 0, ''.join(entries), '')
                    if tail[:2] == ['remote', 'get-url'] and '--all' in tail:
                        section = 'remote "'+tail[-1]+'"'
                        value = parser.get(section, 'pushurl' if '--push' in tail and parser.has_option(section, 'pushurl') else 'url', fallback='')
                        return subprocess.CompletedProcess(argv, 0 if value else 1, value, '')
                    if tail == ['remote', 'get-url', 'origin']:
                        value = parser.get('remote "origin"', 'url', fallback='')
                        return subprocess.CompletedProcess(argv, 0 if value else 1, value, '')
                return subprocess.CompletedProcess(argv, 128, '', 'no synthetic Git repository')
        if command[:3] == ['gh', 'repo', 'view']:
            name = command[3]
            if name in FIX['visibility']:
                return subprocess.CompletedProcess(argv, 0, json.dumps({'nameWithOwner': name, 'visibility': FIX['visibility'][name]}), '')
            return subprocess.CompletedProcess(argv, 1, '', 'unknown synthetic visibility')
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
