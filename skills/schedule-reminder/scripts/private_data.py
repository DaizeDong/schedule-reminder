"""Resolve runtime DATA and prove the nearest governing Git repository is PRIVATE."""
from pathlib import Path
from contextlib import contextmanager
import json
import ntpath
import os
import subprocess
import hashlib
import stat
import threading
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


_VISIBILITY_ARGV = ('gh', 'repo', 'view')
_VISIBILITY_FIELDS = ('--json', 'nameWithOwner,visibility')


def _query(argv):
    if len(argv) == 6 and tuple(argv[:3]) == _VISIBILITY_ARGV and tuple(argv[4:]) == _VISIBILITY_FIELDS:
        return _github_visibility(argv[3])
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



def _github_visibility(name):
    """Live visibility of one publication destination, independent of the ACTIVE gh account.

    A plain `gh repo view` asks only with whichever account `gh auth switch` last selected; with
    an active account that cannot see the private companion every proof failed closed. The pinned
    Guards kit asks with the owner's stored account, then
    every other stored account, then gh's default, and refuses only when none can see it. The
    answer keeps the `gh repo view` JSON shape so the caller's PRIVATE check is unchanged."""
    boundary = _shared_boundary()
    ask = getattr(boundary, 'query_github_visibility', None)
    if not callable(ask):
        raise ValueError('Guards dependency lacks the account-independent visibility API')
    try:
        visibility = ask(name)
    except boundary.GitError as error:
        raise ValueError('PRIVATE repository proof unavailable') from error
    return json.dumps({'nameWithOwner': name, 'visibility': visibility})


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


# A full PRIVATE proof runs ~55 git queries and one live `gh repo view` per publication
# destination (~3 s). One CLI command proves the same companion many times (database, action
# workspace, run directory, locks, records): work-action proved it 22 times (~64 s) against the
# console's 30 s budget. A successful proof is therefore reused within this process while the
# companion state it depends on is unchanged: the companion root and Git administration, its own
# Git configuration (remote URLs), HEAD and the ref it names, the global and system Git
# configuration (including the files GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM name and literal
# core.excludesFile targets in the hashed files), the SSH client configuration files the proof
# attests, the local visibility receipt, the process environment and the proof implementation.
# Any change is a miss and proves in full. A refusal is never stored, so it is proved again next
# time. Entries expire after PROOF_TTL_SECONDS so a long-lived tick cannot keep a stale answer for
# long. Bounded only by that TTL: configuration reached only through include directives,
# server-side visibility changes, the visibility receipt ageing past its maximum age, and changes
# inside the Git installation itself (exec-path). Ignore status is checked per destination through
# the proof's own read-only Git query and re-checked whenever a .gitignore that can decide it
# changes. The memo is not used under GIT_CEILING_DIRECTORIES or below a directory Git would take
# for a bare repository: there Git discovery stops somewhere other than the nearest .git.
PROOF_TTL_SECONDS = 60.0
_PROOF_LOCK = threading.RLock()
_PROOF_MEMO = {}
_UNREADABLE = object()


def clear_proof_memo():
    """Forget every reused PRIVATE proof in this process."""
    with _PROOF_LOCK:
        _PROOF_MEMO.clear()


def _clock():
    return time.monotonic()


def _looks_bare(candidate):
    """Git discovery also stops at a directory that is itself a repository (HEAD, objects, refs)."""
    try:
        return ((candidate/'HEAD').is_file() and (candidate/'objects').is_dir()
                and (candidate/'refs').is_dir())
    except OSError:
        return True


def _governing_root(directory):
    """The nearest ancestor holding a .git entry, or None where Git discovery could stop elsewhere.

    None (no memo) when GIT_CEILING_DIRECTORIES is set, or when a directory on the way up looks
    like a bare repository, because Git stops there and refuses rather than reaching the .git.
    """
    if os.environ.get('GIT_CEILING_DIRECTORIES'):
        return None
    for candidate in (directory, *directory.parents):
        try:
            os.lstat(candidate/'.git')
        except FileNotFoundError:
            if _looks_bare(candidate):
                return None
            continue
        except OSError:
            return None
        return candidate
    return None


def _excludes_targets(path, home):
    """Literal core.excludesFile values in one config file (include chains are TTL-bounded)."""
    try:
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    targets = []
    for line in text.splitlines():
        name, separator, value = line.strip().partition('=')
        if separator and name.strip().casefold() == 'excludesfile':
            value = value.strip().strip('"')
            if value.startswith('~/') or value == '~':
                value = str(home) + value[1:]
            if value:
                targets.append(Path(value))
    return targets


def _git_system_files(home):
    """System Git and SSH configuration files the proof may read; a superset is fine."""
    import shutil
    paths = set()
    for name in ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM'):
        if os.environ.get(name):
            paths.add(Path(os.environ[name]))
    profiles = {str(home), os.environ.get('HOME'), os.environ.get('USERPROFILE')}
    paths.update(Path(profile)/'.ssh'/'config' for profile in profiles if profile)
    if os.name == 'nt':
        program_data = os.environ.get('ProgramData')
        if program_data:
            paths.add(Path(program_data)/'ssh'/'ssh_config')
            paths.add(Path(program_data)/'Git'/'config')
        git = shutil.which('git')
        if git:
            for installation in list(Path(git).resolve().parents)[:4]:
                paths.update({installation/'etc'/'gitconfig', installation/'etc'/'ssh'/'ssh_config',
                              installation/'mingw64'/'etc'/'gitconfig'})
    else:
        paths.update({Path('/etc/gitconfig'), Path('/etc/ssh/ssh_config')})
    return paths


def _file_state(path, digest=True):
    try:
        if digest:
            with open(path, 'rb') as stream:
                return hashlib.sha256(stream.read()).hexdigest()
        info = os.stat(path)
        return (info.st_mtime_ns, info.st_size)
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE


def _admin_dirs(root):
    marker = root/'.git'
    if marker.is_dir():
        admin = marker
    elif marker.is_file():
        text = marker.read_text(encoding='utf-8').strip()
        if not text.startswith('gitdir:'):
            return None
        admin = Path(text[7:].strip())
        admin = (admin if admin.is_absolute() else root/admin).resolve()
    else:
        return None
    common = admin
    if (admin/'commondir').is_file():
        common = (admin/(admin/'commondir').read_text(encoding='utf-8').strip()).resolve()
    return admin, common


def _companion_state(root):
    """Cheap local fingerprint of what a proof depends on; None when it cannot be read."""
    try:
        dirs = _admin_dirs(root)
        if dirs is None:
            return None
        admin, common = dirs
        parts = [str(root), str(admin), str(common), str(SOURCE),
                 hashlib.sha256(json.dumps(sorted(os.environ.items())).encode('utf-8')).hexdigest(),
                 _file_state(admin/'HEAD')]
        try:
            text = (admin/'HEAD').read_text(encoding='utf-8').strip()
        except FileNotFoundError:
            text = ''
        if text.startswith('ref:'):
            ref = text[4:].strip()
            if '..' in ref.split('/'):
                return None
            parts += [_file_state(admin/ref), _file_state(common/ref)]
        parts += [_file_state(admin/'config'), _file_state(common/'config'), _file_state(admin/'config.worktree'),
                  _file_state(common/'info'/'exclude'), _file_state(common/'packed-refs', digest=False)]
        home = Path(os.path.expanduser('~'))
        xdg = Path(os.environ.get('XDG_CONFIG_HOME') or home/'.config')
        files = {home/'.gitconfig', Path(os.environ.get('HOME') or home)/'.gitconfig',
                 xdg/'git'/'config', xdg/'git'/'ignore', home/'.pii-guard'/'visibility.json'}
        files |= _git_system_files(home)
        for config in sorted(files | {admin/'config', common/'config', admin/'config.worktree'}, key=str):
            files.update(_excludes_targets(config, home))
        for path in sorted(files, key=str):
            parts.append((str(path), _file_state(path)))
    except (OSError, ValueError):
        return None
    if any(part is _UNREADABLE or isinstance(part, tuple) and part[-1] is _UNREADABLE for part in parts):
        return None
    return hashlib.sha256(repr(parts).encode('utf-8')).hexdigest()


def _ignore_state(root, relative):
    """Fingerprint the .gitignore files that can decide check-ignore for ``relative``."""
    directory, states = root, [_file_state(root/'.gitignore')]
    for part in relative.rstrip('/').split('/')[:-1]:
        if part in ('', '.'):
            continue
        directory = directory/part
        states.append(_file_state(directory/'.gitignore'))
    if any(state is _UNREADABLE for state in states):
        return None
    return hashlib.sha256(repr(states).encode('utf-8')).hexdigest()


def _proof_result(path, entry, relative, _transient_lock):
    return {'path': str(path), 'repository': entry['repositories'][0], 'repositories': list(entry['repositories']),
            'visibility': 'PRIVATE', 'root': str(entry['root']), 'signature': entry['signature'],
            'head': entry['head'], 'eligibility': 'transient-lock' if _transient_lock else 'versionable-data'}


def _relative(path, eligible, root, directory_owner):
    if (not path.is_relative_to(root) or root.is_relative_to(SOURCE)
            or SOURCE.is_relative_to(root)):
        raise ValueError('PRIVATE repository does not govern destination')
    relative = eligible.relative_to(root).as_posix()
    if directory_owner and relative != '.':
        relative += '/'
    return relative


def _memo_lookup(path, eligible, directory_owner, _transient_lock):
    """Return a result from a still-valid memo entry, or None to prove in full."""
    root = _governing_root(_proof_directory(path))
    if root is None:
        return None
    key = os.path.normcase(str(root))
    entry = _PROOF_MEMO.get(key)
    if entry is None:
        return None
    if (_clock() - entry['proved_at'] > PROOF_TTL_SECONDS or entry['factory'] is not _shared_boundary
            or entry['query'] is not _query or _companion_state(root) != entry['state']):
        _PROOF_MEMO.pop(key, None)
        return None
    try:
        relative = _relative(path, eligible, entry['root'], directory_owner)
    except ValueError:
        return None
    ignore = _ignore_state(entry['root'], relative)
    if ignore is None:
        return None
    if entry['relatives'].get(relative) != ignore:
        boundary = entry['boundary']
        try:
            ignored = boundary.read_private_companion_git(
                entry['proof'], 'check-ignore', '--no-index', '-q', '--', relative).returncode == 0
        except (OSError, ValueError, TypeError, subprocess.SubprocessError, boundary.GitError):
            _PROOF_MEMO.pop(key, None)
            return None
        if ignored:
            raise ValueError('DATA requires initialized PRIVATE versioned storage: '
                             'runtime DATA owner is ignored and cannot be versioned')
        if _ignore_state(entry['root'], relative) != ignore:
            return None
        entry['relatives'][relative] = ignore
    return _proof_result(path, entry, relative, _transient_lock)


def prove_private(destination, *, _transient_lock=False):
    """Require local route proof, live PRIVATE visibility and versionable DATA storage.

    Only file_lock may select the transient exception. Its lock may be ignored,
    but the owning DATA path (or a named coordination directory) must not be.
    A successful proof is reused for an unchanged companion within this process
    (see PROOF_TTL_SECONDS); a refusal is never reused.
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
    with _PROOF_LOCK:
        reused = _memo_lookup(path, eligible, directory_owner, _transient_lock)
        if reused is not None:
            return reused
        return _prove_and_remember(path, eligible, directory_owner, _transient_lock)


def _prove_and_remember(path, eligible, directory_owner, _transient_lock):
    started = _clock()  # an entry's age counts from before the proof, never after it
    walked = _governing_root(_proof_directory(path))
    state_before = _companion_state(walked) if walked is not None else None
    factory, query = _shared_boundary, _query
    boundary = factory()
    try:
        proof = boundary.prove_private_companion(_proof_directory(path))
        root = Path(proof.root).resolve()
        relative = _relative(path, eligible, root, directory_owner)
        ignore_before = _ignore_state(root, relative)
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
    entry = {'root': root, 'repositories': tuple(repositories), 'signature': proof.signature, 'head': head,
             'proof': current, 'boundary': boundary, 'factory': factory, 'query': query, 'proved_at': started,
             'state': state_before, 'relatives': {}}
    # Remember only what this proof established, and only if nothing it depends on moved while
    # it ran. The result is returned either way.
    if (walked is not None and state_before is not None
            and os.path.normcase(str(walked.resolve())) == os.path.normcase(str(root))
            and _companion_state(walked) == state_before):
        if ignore_before is not None and _ignore_state(root, relative) == ignore_before:
            entry['relatives'][relative] = ignore_before
        _PROOF_MEMO[os.path.normcase(str(walked))] = entry
    return _proof_result(path, entry, relative, _transient_lock)


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
