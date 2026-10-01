"""Resolve runtime DATA and prove the nearest governing Git repository is PRIVATE."""
from pathlib import Path
from contextlib import contextmanager
import json
import os
import re
import subprocess
import time

SOURCE = Path(__file__).resolve().parents[3]


def config_root():
    selected = os.environ.get('SCHEDULE_REMINDER_CONFIG') or os.environ.get('AGENT_CENTER_CONFIG')
    if selected:
        path = Path(selected).expanduser().resolve()
        return path.parent if path.suffix.lower() == '.json' else path
    return Path.home()/'.schedule-reminder-config'


def data_dir():
    selected = os.environ.get('SCHEDULE_REMINDER_DATA_DIR')
    return Path(selected).expanduser().resolve() if selected else config_root()/'data'


def registry_path():
    return Path(os.environ.get('AGENT_CENTER_CONFIG') or config_root()/'registry.json').expanduser().resolve()


def _query(argv):
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=20)
    if result.returncode:
        raise ValueError('PRIVATE repository proof unavailable')
    return result.stdout.strip()


def prove_private(destination):
    path = Path(destination).expanduser().resolve()
    if path.is_relative_to(SOURCE) or SOURCE.is_relative_to(path) or '.git' in {part.casefold() for part in path.parts}:
        raise ValueError('DATA must be outside public source and Git metadata in a PRIVATE companion')
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_file():
        existing = existing.parent
    try:
        root = Path(_query(['git', '-C', str(existing), 'rev-parse', '--show-toplevel'])).resolve()
        if not path.is_relative_to(root):
            raise ValueError('PRIVATE repository does not govern destination')
        remote = _query(['git', '-C', str(root), 'remote', 'get-url', 'origin'])
        match = re.fullmatch(r'(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', remote)
        if not match:
            raise ValueError('PRIVATE origin visibility is unknown')
        name = match.group(1)
        response = json.loads(_query(['gh', 'repo', 'view', name, '--json', 'nameWithOwner,visibility']))
        if (not isinstance(response, dict) or response.get('visibility') != 'PRIVATE'
                or response.get('nameWithOwner', '').casefold() != name.casefold()):
            raise ValueError('DATA requires a governing PRIVATE versioned repository')
    except PermissionError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        raise ValueError('DATA requires initialized PRIVATE versioned storage: '+str(error)) from error
    return {'path': str(path), 'repository': name, 'visibility': 'PRIVATE'}


def prepare_parent(destination):
    proof = prove_private(destination)
    Path(destination).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    return proof


@contextmanager
def file_lock(destination):
    """Serialize a PRIVATE record update across worker processes, including Windows."""
    prepare_parent(destination)
    with open(destination, 'a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic()+10
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('PRIVATE record is locked')
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
