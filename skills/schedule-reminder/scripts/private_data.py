"""Resolve runtime DATA and prove the nearest governing Git repository is PRIVATE."""
from pathlib import Path
from contextlib import contextmanager
import json
import ntpath
import os
import subprocess
import stat
import time
from types import SimpleNamespace

SOURCE = Path(__file__).resolve().parents[3]
CREATE_NO_WINDOW = 0x08000000


def config_root():
    selected = os.environ.get('SCHEDULE_REMINDER_CONFIG') or os.environ.get('AGENT_CENTER_CONFIG')
    if selected:
        path = assert_writable_path(selected).resolve()
        return path.parent if path.suffix.lower() == '.json' else path
    resolver = _shared_resolver()
    # A DATA override selects output storage, not the registry/inbox companion.
    resolver.os = SimpleNamespace(**{
        **vars(os),
        'environ': {name: value for name, value in os.environ.items()
                    if name != 'SCHEDULE_REMINDER_DATA_DIR'},
    })
    try:
        root = resolver.resolve_companion_root('schedule-reminder')
    except RuntimeError as error:
        raise ValueError('Schedule companion discovery failed: '+str(error)) from error
    # An absent path keeps uninitialized reads inert; writes still require PRIVATE proof.
    return assert_writable_path(root or Path.home()/'.schedule-reminder-config').resolve()


def data_dir():
    selected = os.environ.get('SCHEDULE_REMINDER_DATA_DIR')
    return assert_writable_path(selected).resolve() if selected else config_root()/'data'


def registry_path():
    return assert_writable_path(os.environ.get('AGENT_CENTER_CONFIG') or config_root()/'registry.json').resolve()


def _query(argv):
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    if argv[0] == "gh":
        env["GH_HOST"] = "github.com"
    # Output is captured, so no console is needed: without CREATE_NO_WINDOW a windowless caller
    # (a pythonw tick, a detached runner) gets a new visible console for every proof query.
    flags = {'creationflags': CREATE_NO_WINDOW} if os.name == 'nt' else {}
    result = subprocess.run(argv, capture_output=True, text=True, encoding='utf-8', timeout=20, env=env,
                            **flags)
    if result.returncode:
        raise ValueError('PRIVATE repository proof unavailable')
    return result.stdout.strip()



def _shared_boundary():
    """Load only the reviewed public companion-proof API from this consumer's kit."""
    import importlib.util
    path = SOURCE/"guards/tools/data_boundary.py"
    assert_writable_path(path)
    if not path.is_file():
        raise ValueError("Guards dependency is missing; initialize the reviewed submodule")
    try:
        spec = importlib.util.spec_from_file_location("_schedule_private_boundary", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except (OSError, ImportError, ValueError) as error:
        raise ValueError("Guards PRIVATE proof API is unavailable") from error
    if not all(callable(getattr(module, name, None)) for name in
               ("prove_private_companion", "read_private_companion_git")):
        raise ValueError("Guards dependency lacks the supported PRIVATE proof API")
    return module


def _shared_resolver():
    """Load portable discovery from this consumer's pinned Guards dependency."""
    import importlib.util
    path = SOURCE/'guards/tools/datadir.py'
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


def open_for_write(destination, mode="w", *args, _transient_lock=False, **kwargs):
    """Prove the destination and validate the opened inode before truncation."""
    if not mode or mode[0] not in "wax":
        raise ValueError("runtime writer requires w, a, or x mode")
    options = {"_transient_lock": True} if _transient_lock else {}
    proof = prepare_parent(destination, **options)
    path = assert_writable_path(destination)
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
        assert_writable_path(path)
        if prove_private(path, **options) != proof:
            raise ValueError("PRIVATE proof changed before runtime truncation")
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


def _proof_directory(path):
    """Git discovery starts at the nearest existing directory of a runtime path."""
    directory = path if path.is_dir() else path.parent
    while not directory.exists():
        directory = directory.parent
    return assert_writable_path(directory)


def prove_private(destination, *, _transient_lock=False):
    """Require local route proof, live PRIVATE visibility and versionable DATA storage.

    Only file_lock may select the transient exception. Its lock may be ignored,
    but the owning DATA path (or a named coordination directory) must not be.
    """
    selectors = {"GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_CONFIG",
                 "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}
    if any(name.upper() in selectors or name.upper().startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
           for name in os.environ):
        raise ValueError("inherited Git repository/configuration selector cannot establish physical DATA governance")
    path = assert_writable_path(destination).resolve()
    if path.is_relative_to(SOURCE) or SOURCE.is_relative_to(path) or '.git' in {part.casefold() for part in path.parts}:
        raise ValueError('DATA must be outside public source and Git metadata in a PRIVATE companion')
    eligible = path
    directory_owner = False
    if _transient_lock:
        if not path.name.endswith('.lock'):
            raise ValueError('transient coordination requires a lock filename')
        if path.name in {'.lifecycle.lock', '.enqueue.lock'}:
            eligible = path.parent
            directory_owner = True
        else:
            suffix = '.process.lock' if path.name.endswith('.process.lock') else '.lock'
            eligible = path.with_name(path.name[:-len(suffix)])
    boundary = _shared_boundary()
    try:
        proof = boundary.prove_private_companion(_proof_directory(path))
        root = Path(proof.root).resolve()
        if (not path.is_relative_to(root) or root.is_relative_to(SOURCE)
                or SOURCE.is_relative_to(root)):
            raise ValueError('PRIVATE repository does not govern destination')
        relative = eligible.relative_to(root).as_posix()
        if directory_owner and relative != '.':
            relative += '/'
        head = boundary.read_private_companion_git(proof, 'rev-parse', '--verify', 'HEAD').stdout.strip()
        if not head:
            raise ValueError('PRIVATE companion requires a committed HEAD')
        if boundary.read_private_companion_git(proof, 'check-ignore', '--no-index', '-q', '--', relative).returncode == 0:
            raise ValueError('runtime DATA owner is ignored and cannot be versioned')
        repositories = list(proof.repositories)
        for name in repositories:
            response = json.loads(_query(['gh', 'repo', 'view', name, '--json', 'nameWithOwner,visibility']))
            if (not isinstance(response, dict) or response.get('visibility') != 'PRIVATE'
                    or not isinstance(response.get('nameWithOwner'), str)
                    or response['nameWithOwner'].casefold() != name.casefold()):
                raise ValueError('DATA requires every publication destination to be PRIVATE')
        current = boundary.prove_private_companion(_proof_directory(path))
        if (current.root, current.repositories, current.signature) != (proof.root, proof.repositories, proof.signature):
            raise ValueError('PRIVATE publication configuration changed during visibility verification')
        if boundary.read_private_companion_git(current, 'rev-parse', '--verify', 'HEAD').stdout.strip() != head:
            raise ValueError('PRIVATE companion HEAD changed during visibility verification')
        if boundary.read_private_companion_git(current, 'check-ignore', '--no-index', '-q', '--', relative).returncode == 0:
            raise ValueError('runtime DATA owner became ignored during visibility verification')
    except PermissionError:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, boundary.GitError) as error:
        raise ValueError('DATA requires initialized PRIVATE versioned storage: '+str(error)) from error
    return {'path': str(path), 'repository': repositories[0], 'repositories': repositories,
            'visibility': 'PRIVATE', 'root': str(root), 'signature': proof.signature, 'head': head,
            'eligibility': 'transient-lock' if _transient_lock else 'versionable-data'}


def prepare_parent(destination, *, _transient_lock=False):
    options = {'_transient_lock': True} if _transient_lock else {}
    proof = prove_private(destination, **options)
    assert_writable_path(destination).parent.mkdir(parents=True, exist_ok=True)
    assert_writable_path(destination)
    if prove_private(destination, **options) != proof:
        raise ValueError('PRIVATE proof changed before runtime write')
    return proof


@contextmanager
def file_lock(destination):
    """Serialize a PRIVATE record update across worker processes, including Windows."""
    with open_for_write(destination, 'a+b', _transient_lock=True) as stream:
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
