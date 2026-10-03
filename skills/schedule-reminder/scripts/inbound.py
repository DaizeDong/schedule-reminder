"""Durable PRIVATE inbound work and action receipts; never stored in public source."""
from contextlib import contextmanager
import hashlib
import json
import os
import re
from pathlib import Path
import tempfile
import private_data


def identity(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def root(state_root=None):
    return Path(state_root) if state_root else private_data.data_dir()/"state"


def _read(path):
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("invalid durable inbound record")
    return value


def _write(path, value):
    private_data.prepare_parent(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def stage(stream, channel_id, message_id, kind, payload, state_root=None):
    if not message_id or not channel_id:
        raise ValueError("durable inbound work requires channel and message identity")
    key = identity(kind, str(channel_id), str(message_id))
    path = root(state_root)/"inbound"/(key+".json")
    with private_data.file_lock(str(path)+".lock"):
        current = _read(path)
        if current is None:
            current = {"schema_version": 1, "id": key, "stream": stream,
                       "channel_id": str(channel_id), "message_id": str(message_id),
                       "kind": kind, "payload": payload, "status": "pending", "attempts": 0}
            _write(path, current)
    return current


def pending(state_root=None):
    directory = root(state_root)/"inbound"
    if not directory.exists():
        return []
    result = []
    for path in sorted(directory.glob("*.json")):
        record = _read(path)
        if record and record["status"] == "pending":
            result.append(record)
    return sorted(result, key=lambda row: (row["stream"], row["message_id"], row["id"]))


@contextmanager
def processing(record, state_root=None):
    path = root(state_root)/"inbound"/(record["id"]+".json")
    with private_data.file_lock(str(path)+".process.lock"):
        yield _read(path)


def attempted(record, completed, error=None, state_root=None, *, retryable=True):
    path = root(state_root)/"inbound"/(record["id"]+".json")
    with private_data.file_lock(str(path)+".lock"):
        current = _read(path)
        if current is None:
            raise ValueError("durable inbound record disappeared")
        status = "completed" if completed else "pending" if retryable else "command_failed"
        attempts = current["attempts"] + (current["status"] != "command_failed")
        current.update(status=status, attempts=attempts, last_error=error)
        _write(path, current)

def command_started(record, name, state_root=None):
    """Persist uncertain command execution before calling a potentially non-idempotent handler."""
    path = root(state_root)/"inbound"/(record["id"]+".json")
    with private_data.file_lock(str(path)+".lock"):
        current = _read(path)
        if current is None or current["status"] != "pending":
            raise ValueError("command is not pending")
        current.update(status="command_failed", command=name, attempts=current["attempts"]+1,
                       last_error="command outcome unconfirmed; explicit retry required")
        _write(path, current)


def retry_command(record_id, state_root=None):
    """Explicitly rearm a failed/uncertain handler after its effects have been reviewed."""
    if not isinstance(record_id, str) or re.fullmatch(r"[0-9a-f]{64}", record_id) is None:
        raise ValueError("retry requires the full durable inbound id")
    path = root(state_root)/"inbound"/(record_id+".json")
    with private_data.file_lock(str(path)+".process.lock"):
        with private_data.file_lock(str(path)+".lock"):
            current = _read(path)
            if current is None or current["status"] != "command_failed":
                raise ValueError("only a failed or unconfirmed command can be retried")
            current.update(status="pending", last_error=None,
                           manual_retries=current.get("manual_retries", 0)+1)
            _write(path, current)
            return current


@contextmanager
def dispatch_record(stream, message_id):
    path = root()/"dispatch"/(identity(stream, str(message_id))+".json")
    with private_data.file_lock(str(path)+".lock"):
        value = _read(path) or {"schema_version": 1, "plan": None, "outcomes": {}}
        def save():
            _write(path, value)
        yield value, save
