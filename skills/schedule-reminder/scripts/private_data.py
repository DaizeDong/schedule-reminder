"""Resolve runtime DATA locations in the private companion and keep writes out of the public source tree.

There is no runtime visibility proof. Where DATA lives is decided by resolution: the companion
directory (or an explicit override), never an in-repo fallback. A write is refused only by cheap
path checks: the destination must not be inside this repository's source tree or Git metadata,
and must not reach through a filesystem alias. Publication safety is enforced at commit time by
the Guards hooks and CI, not on every runtime write.
"""
from pathlib import Path
from contextlib import contextmanager
import functools
import ntpath
import os
import stat
import time
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[3]


def config_root():
    selected = os.environ.get('SCHEDULE_REMINDER_CONFIG') or os.environ.get('AGENT_CENTER_CONFIG')
    if selected:
        path = assert_writable_path(selected).resolve()
        return path.parent if path.suffix.lower() == '.json' else path
    try:
        root = _shared_resolver(SOURCE/'guards/tools/datadir.py').resolve_companion_root('schedule-reminder')
    except RuntimeError as error:
        raise ValueError('Schedule companion discovery failed: '+str(error)) from error
    # Nothing found: the answer is the sibling convention path, which is never created here.
    # Reads of it stay inert (an uninitialized tool) and check_destination refuses writes below it.
    # This module never names a home-directory path.
    return assert_writable_path(root or SOURCE.parent/'schedule-reminder-config').resolve()


def data_dir():
    selected = os.environ.get('SCHEDULE_REMINDER_DATA_DIR')
    return assert_writable_path(selected).resolve() if selected else config_root()/'data'


def registry_path():
    return assert_writable_path(os.environ.get('AGENT_CENTER_CONFIG') or config_root()/'registry.json').resolve()


@functools.lru_cache(maxsize=None)
def _shared_resolver(path):
    """Load portable discovery from this consumer's pinned Guards dependency, once per process."""
    import importlib.util
    assert_writable_path(path)
    if not path.is_file():
        raise ValueError('Guards dependency is missing; initialize the reviewed submodule')
    try:
        spec = importlib.util.spec_from_file_location('_schedule_companion_resolver', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (OSError, ImportError, ValueError) as error:
        raise ValueError('Guards companion discovery API is unavailable') from error
    if not callable(getattr(module, 'resolve_companion_root', None)):
        raise ValueError('Guards dependency lacks the supported companion discovery API')
    # A DATA override selects output storage, not the registry/inbox companion, so the resolver
    # sees the live environment without SCHEDULE_REMINDER_DATA_DIR.
    module.os = SimpleNamespace(**{**vars(os), 'environ': SimpleNamespace(
        get=lambda name, default=None: (default if name == 'SCHEDULE_REMINDER_DATA_DIR'
                                        else os.environ.get(name, default)))})
    return module


def assert_writable_path(destination):
    """Reject unproven aliases before opening a runtime file for mutation."""
    expanded = Path(destination).expanduser()
    if os.name == 'nt':
        spelling = str(expanded).replace('/', '\\')
        drive, tail = ntpath.splitdrive(spelling)
        if spelling.startswith(('\\\\?\\', '\\\\.\\')) or drive and not tail.startswith('\\'):
            raise ValueError('runtime DATA requires an ordinary absolute Windows path')
        devices = {'CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$'}
        devices.update(prefix + digit for prefix in ('COM', 'LPT') for digit in '123456789\u00b9\u00b2\u00b3')
        for part in tail.split('\\'):
            if part in ('', '.', '..'):
                continue
            if (part.endswith(('.', ' ')) or part.split('.')[0].rstrip(' ').upper() in devices
                    or any(character in '<>:"|?*' or ord(character) < 32 for character in part)):
                raise ValueError('runtime DATA requires ordinary versionable Windows filenames')
    path = Path(os.path.abspath(expanded))
    for node in [*reversed(path.parents), path]:
        for attempt in range(3):
            try:
                info = node.lstat()
            except FileNotFoundError:
                break
            reparse = getattr(info, "st_file_attributes", 0) & 1024
            # A concurrent SQLite sidecar unlink can leave lstat observing zero links.
            # Re-read the pathname; confirmed aliases and persistent zero links refuse.
            if (stat.S_ISREG(info.st_mode) and info.st_nlink == 0
                    and not reparse and attempt < 2):
                continue
            if (stat.S_ISLNK(info.st_mode) or reparse
                    or stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
                raise ValueError("runtime DATA has an unproven filesystem alias")
            break
    return path


def assert_sqlite_paths(destination):
    path = assert_writable_path(destination)
    for suffix in ("-wal", "-shm", "-journal"):
        assert_writable_path(str(path) + suffix)
    return path


def check_destination(destination):
    """Cheap path checks for runtime DATA, no subprocess and no visibility query.

    The destination must be outside the public source tree and Git metadata, and must not lie
    below an uninitialized companion: that companion resolves to its convention path so reads
    stay inert, and a write there must fail instead of creating an unversioned directory."""
    path = assert_writable_path(destination).resolve()
    if (path.is_relative_to(SOURCE) or SOURCE.is_relative_to(path)
            or '.git' in {part.casefold() for part in path.parts}):
        raise ValueError('DATA must be outside the public source tree and Git metadata')
    try:
        root = config_root()
    except ValueError:
        root = None  # discovery failed; a caller that derived its path from it already failed
    if root is not None and path.is_relative_to(root) and not root.is_dir():
        raise ValueError('Schedule companion is not initialized at %s: create the private companion '
                         'repository there or set SCHEDULE_REMINDER_CONFIG to it' % root)
    return path


def prepare_parent(destination):
    """Check the destination and create its parent directory."""
    path = check_destination(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def open_for_write(destination, mode="w", *args, **kwargs):
    """The single write entry point: check the destination, create its parent, and validate the
    opened inode before truncation. Callers do not check the path again beforehand."""
    if not mode or mode[0] not in "wax":
        raise ValueError("runtime writer requires w, a, or x mode")
    path = prepare_parent(destination)
    flags = os.O_RDWR if "+" in mode else os.O_WRONLY
    flags |= os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if mode[0] == "a":
        flags |= os.O_APPEND
    elif mode[0] == "x":
        flags |= os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    stream = None
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or not os.path.samestat(opened, current)):
            raise ValueError("runtime DATA inode changed or has another hardlink")
        stream = os.fdopen(descriptor, mode, *args, **kwargs)
        if mode[0] == "w":
            stream.truncate(0)
        return stream
    except BaseException:
        if stream is None:
            os.close(descriptor)
        else:
            stream.close()
        raise


@contextmanager
def file_lock(destination):
    """Serialize a DATA record update across worker processes, including Windows."""
    with open_for_write(destination, 'a+b') as stream:
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
                    raise TimeoutError('DATA record is locked')
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
