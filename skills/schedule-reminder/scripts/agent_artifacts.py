"""Bounded deterministic evidence for a dedicated non-code output directory."""
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import private_data


def _filesystem():
    path = private_data.SOURCE / 'guards/fleet_guards/filesystem.py'
    private_data.assert_writable_path(path)
    if not path.is_file():
        raise RuntimeError('Guards filesystem dependency is missing; initialize the reviewed submodule')
    spec = importlib.util.spec_from_file_location('_schedule_artifact_filesystem', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fs = _filesystem()


class ArtifactEvidenceUnavailable(RuntimeError):
    pass


def _plain(path):
    return fs.validate_path(path).lstat()


def capture_artifacts(workspace, max_bytes=1000000, max_files=200):
    try:
        root = Path(workspace)
        if not root.is_absolute() or not root.is_dir() or root in (Path(root.anchor), Path.home()):
            raise ArtifactEvidenceUnavailable('invalid artifact workspace')
        if type(max_bytes) is not int or max_bytes <= 0 or type(max_files) is not int or max_files <= 0:
            raise ArtifactEvidenceUnavailable('invalid artifact limits')
        for part in (root, *root.parents):
            _plain(part)
        records, directories, total = [], [], 0
        pending = [root]
        while pending:
            directory = pending.pop()
            before_dir = _plain(directory)
            directories.append((directory, (before_dir.st_dev, before_dir.st_ino, before_dir.st_mtime_ns)))
            for path in sorted(directory.iterdir()):
                before = _plain(path)
                if stat.S_ISDIR(before.st_mode):
                    if len(directories) + len(pending) >= max_files:
                        raise ArtifactEvidenceUnavailable('too many artifact directories')
                    pending.append(path)
                    continue
                if not stat.S_ISREG(before.st_mode):
                    raise ArtifactEvidenceUnavailable('non-regular artifact')
                if len(records) >= max_files or total + before.st_size > max_bytes:
                    raise ArtifactEvidenceUnavailable('artifact limits exceeded')
                data = fs.read_bounded(path, max_bytes - total)
                after = _plain(path)
                if (len(data) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_ino, after.st_size, after.st_mtime_ns)):
                    raise ArtifactEvidenceUnavailable('artifact changed during capture')
                total += len(data)
                record = {'path': path.relative_to(root).as_posix(), 'size': len(data),
                          'sha256': hashlib.sha256(data).hexdigest()}
                try:
                    record['text'] = data.decode('utf-8')
                except UnicodeDecodeError:
                    record['binary'] = True
                records.append(record)
        for path, stamp in directories:
            after_dir = _plain(path)
            if (after_dir.st_dev, after_dir.st_ino, after_dir.st_mtime_ns) != stamp:
                raise ArtifactEvidenceUnavailable('artifact directory changed during capture')
        return json.dumps({'files': sorted(records, key=lambda row: row['path'])},
                          ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    except (OSError, ValueError) as exc:
        raise ArtifactEvidenceUnavailable('artifact evidence unavailable') from exc
