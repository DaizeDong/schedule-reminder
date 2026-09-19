"""Task-owner notification receipts in the existing reminder private SQLite database.

No model dispatch or business execution lives here. A durable send boundary prevents blind
replay after a crash or lost response. It cannot provide exactly-once network delivery.
The additive store migration must be installed before this producer is enabled.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import argparse
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

import relay
import store


class ReceiptError(ValueError):
    """Visible invalid input, conflicting identity, or unavailable receipt storage."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def event_id(run_id, phase, condition):
    """Unambiguous encoding of the approved run/phase/condition business identity."""
    return "notification:v1:" + _json([run_id, phase, condition])


@dataclass(frozen=True)
class Event:
    owner: str
    run_id: str
    phase: str
    condition: str
    stream: str

    def __post_init__(self):
        for value in (self.owner, self.run_id, self.phase, self.condition, self.stream):
            if not isinstance(value, str) or not value.strip():
                raise ReceiptError("event fields, including stream, must be nonempty strings")

    @property
    def event_id(self):
        return event_id(self.run_id, self.phase, self.condition)

    def fields(self):
        return dict(owner=self.owner, run_id=self.run_id, phase=self.phase,
                    condition=self.condition, stream=self.stream, event_id=self.event_id)


def load_language_policy(path=None):
    """Load the maintained policy by explicit dependency path; absence is never a pass.

    Tests and packaged deployments set SCHEDULE_NOTIFICATION_LANGUAGE_POLICY. The installed
    legacy location remains the production default until CONFIG owns a relocation.
    """
    source = Path(path or os.environ.get("SCHEDULE_NOTIFICATION_LANGUAGE_POLICY") or
                  Path.home() / ".claude" / "scripts" / "notification_language.py")
    if not source.is_file():
        raise ReceiptError("notification language rule is not installed")
    spec = importlib.util.spec_from_file_location("_reminder_notification_language", source)
    if spec is None or spec.loader is None:
        raise ReceiptError("notification language rule cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "offences", None)):
        raise ReceiptError("notification language rule has no offences API")
    return module


@contextmanager
def _connection(db_path):
    # Do not create another database or bootstrap the frozen store behind its owner's back.
    path = db_path or store.default_db_path()
    if not os.path.isfile(path):
        raise ReceiptError("receipt database missing; initialize the existing reminder store")
    conn = store._connect(path)
    try:
        conn.execute("PRAGMA synchronous = FULL")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_receipts)")}
        if not {"event_id", "receipt"}.issubset(columns):
            raise ReceiptError("receipt schema missing; integrate the T15 store migration")
        yield conn
    finally:
        conn.close()


def _read(conn, key):
    row = conn.execute("SELECT receipt FROM notification_receipts WHERE event_id=?", (key,)).fetchone()
    if row is None:
        return None
    try:
        data = json.loads(row[0])
        event = Event(**{k: data[k] for k in ("owner", "run_id", "phase", "condition", "stream")})
        started = data["send_started_at"]
        valid = [
            type(data["version"]) is int and data["version"] == 1,
            data["event_id"] == key == event.event_id,
            data["state"] in ("pending", "sent", "failed", "uncertain"),
            isinstance(data["token"], str) and bool(data["token"]),
            type(data["attempts"]) is int and data["attempts"] >= 1,
            type(data["retry_safe"]) is bool,
            isinstance(data["request_sha256"], str) and len(data["request_sha256"]) == 64,
            data["prepared_sha256"] is None or (isinstance(data["prepared_sha256"], str)
                                               and len(data["prepared_sha256"]) == 64),
            started is None or (type(started) in (float, int) and math.isfinite(started)),
            data["target"] is None or _valid_target(data["target"]),
            isinstance(data["target_changes"], list),
            data["error"] is None or isinstance(data["error"], str),
        ]
        int(data["request_sha256"], 16)
        valid.extend(type(data[name]) in (float, int) and math.isfinite(data[name])
                     for name in ("created_at", "updated_at", "expires_at"))
        valid.extend(isinstance(change, dict) and _valid_target(change.get("to"))
                     and type(change.get("accepted")) is bool for change in data["target_changes"])
        if data["state"] == "failed":
            valid.append(data["retry_safe"] and started is None)
        else:
            valid.append(not data["retry_safe"])
        if data["state"] in ("sent", "uncertain"):
            valid.append(started is not None and data["target"] is not None)
        if not all(valid):
            raise ValueError("invalid receipt fields")
        return data
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        raise ReceiptError("malformed notification receipt; manual reconciliation required") from exc


def _valid_target(target):
    if not isinstance(target, dict) or not isinstance(target.get("stream"), str):
        return False
    kind = target.get("kind")
    if type(target.get("fallback")) is not bool or target["fallback"] != (kind == "big_brother"):
        return False
    if kind == "webhook":
        digest = target.get("webhook_sha256")
        return isinstance(digest, str) and len(digest) == 64 and isinstance(target.get("username"), str)
    if kind == 'command':
        digest = target.get('command_sha256')
        return isinstance(digest, str) and len(digest) == 64
    field = {"bot": "channel_id", "big_brother": "user_id"}.get(kind)
    return field is not None and isinstance(target.get(field), str) and bool(target[field])


def _write(conn, data):
    conn.execute("INSERT INTO notification_receipts(event_id,receipt) VALUES(?,?) "
                 "ON CONFLICT(event_id) DO UPDATE SET receipt=excluded.receipt",
                 (data["event_id"], _json(data)))


def get_receipt(key, *, db_path=None):
    with _connection(db_path) as conn:
        return _read(conn, key)


def _claim(event, request_hash, *, retry_failed, lease_seconds, db_path):
    now = time.time()
    with _connection(db_path) as conn, store._Tx(conn):
        data = _read(conn, event.event_id)
        if data is not None:
            if any(data[k] != v for k, v in event.fields().items()) or data["request_sha256"] != request_hash:
                raise ReceiptError("event conflict: owner, stream or delivery payload changed")
            if data["state"] == "pending" and data["expires_at"] <= now:
                started = data["send_started_at"] is not None
                data.update(state="uncertain" if started else "failed", retry_safe=not started,
                            error="send_interrupted" if started else "abandoned_before_send", updated_at=now)
                _write(conn, data)
            if not (data["state"] == "failed" and data["retry_safe"] and retry_failed):
                return data, False
            data.update(attempts=data["attempts"] + 1)
        else:
            data = dict(version=1, **event.fields(), request_sha256=request_hash,
                        prepared_sha256=None, attempts=1, created_at=now, target=None, target_changes=[])
        data.update(state="pending", retry_safe=False, token=uuid.uuid4().hex,
                    expires_at=now + lease_seconds, send_started_at=None, error=None, updated_at=now)
        _write(conn, data)
        return data, True


def _record_target(data, target, *, allow_target_change, db_path, prepared_hash=None):
    now = time.time()
    with _connection(db_path) as conn, store._Tx(conn):
        current = _read(conn, data["event_id"])
        if (current["token"] != data["token"] or current["state"] != "pending"
                or current["expires_at"] <= now):
            raise ReceiptError("notification claim lost before send")
        previous = current["target"]
        if (current["prepared_sha256"] is not None
                and current["prepared_sha256"] != prepared_hash):
            current.update(state="failed", retry_safe=True, error="prepared_payload_changed", updated_at=now)
            _write(conn, current)
            return current, False
        changed = previous is not None and previous != target
        fallback = previous is None and target.get("fallback")
        if changed or fallback:
            current["target_changes"].append({"from": previous, "to": target, "at": now,
                                               "accepted": not changed or allow_target_change})
        if changed and not allow_target_change:
            current.update(state="failed", retry_safe=True, error="target_changed", updated_at=now)
            _write(conn, current)
            return current, False
        current.update(target=target, prepared_sha256=prepared_hash, updated_at=now)
        _write(conn, current)
        return current, True


def _start_send(data, *, db_path):
    now = time.time()
    with _connection(db_path) as conn, store._Tx(conn):
        current = _read(conn, data["event_id"])
        if (current["token"] != data["token"] or current["state"] != "pending"
                or current["expires_at"] <= now or current["target"] is None):
            raise ReceiptError("notification claim lost before send")
        current.update(send_started_at=now, updated_at=now)
        _write(conn, current)
        return current


def _finish(data, state, error, *, db_path):
    with _connection(db_path) as conn, store._Tx(conn):
        current = _read(conn, data["event_id"])
        # A late acknowledgement may resolve this same attempt's uncertainty; it must never
        # overwrite a newer attempt. A stale preflight failure cannot overwrite a send boundary.
        if current["token"] != data["token"] or current["state"] not in ("pending", "uncertain"):
            return current
        if state == "failed" and current["send_started_at"] is not None:
            raise ReceiptError("cannot mark a started delivery safe to retry")
        current.update(state=state, error=error, retry_safe=state == "failed", updated_at=time.time())
        _write(conn, current)
        return current


def deliver(event: Event, content: str, *, channel_id=None, files=None, username=None,
            fallback="none", language="zh-CN", language_policy_path=None, verbatim=(),
            redactor=None, retry_failed=False, allow_target_change=False, lease_seconds=300,
            db_path=None, command=None):
    """Deliver one owner-selected business event; retries never execute business work.

    `retry_failed` authorizes only a proven pre-send failure. Uncertain/sent receipts never
    resend. `allow_target_change` separately authorizes a changed resolved target on retry.
    `redactor`, when needed, is the owner's maintained canonical callable, not a copied regex.
    `language='preserve'` explicitly retains an existing non-Chinese owner policy.
    Return the persisted receipt; only state == 'sent' is a successful delivery.
    """
    if not isinstance(event, Event) or not isinstance(content, str):
        raise ReceiptError("expected Event and text")
    if type(retry_failed) is not bool or type(allow_target_change) is not bool:
        raise ReceiptError("retry_failed and allow_target_change must be booleans")
    if type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ReceiptError("lease_seconds must be finite and positive")
    paths = [os.fspath(p) for p in files or ()]
    request_hash = hashlib.sha256(_json(dict(content=content, channel_id=channel_id, files=paths,
        username=username, fallback=fallback, language=language, verbatim=list(verbatim),
        **({'command': command} if command is not None else {}))).encode()).hexdigest()
    data, claimed = _claim(event, request_hash, retry_failed=retry_failed,
                           lease_seconds=lease_seconds, db_path=db_path)
    if not claimed:
        return data
    try:
        body = redactor(content) if redactor is not None else content
        if not isinstance(body, str) or not body.strip():
            raise relay.PreparationError("empty_content")
        if language == "zh-CN":
            if load_language_policy(language_policy_path).offences(body, verbatim=verbatim):
                raise relay.PreparationError("language_policy_rejected")
        elif language != "preserve":
            raise relay.PreparationError("unsupported_language_policy")
        file_hashes = []
        for path in paths:
            digest = hashlib.sha256()
            with open(path, "rb") as attachment:
                for chunk in iter(lambda: attachment.read(1024 * 1024), b""):
                    digest.update(chunk)
            file_hashes.append(digest.hexdigest())
        prepared_hash = hashlib.sha256(_json([body, file_hashes]).encode()).hexdigest()
        if os.environ.get("AGENT_CENTER_RELAY_DRYRUN"):
            raise relay.PreparationError("dry_run_is_not_delivery")
        if command is not None:
            if channel_id or username or fallback != 'none':
                raise relay.PreparationError('custom_notifier_policy_conflict')
            prepared = relay.prepare_command(command, stream=event.stream, files=paths, content=body)
        else:
            prepared = relay.prepare_send(stream=event.stream, channel_id=channel_id,
                files=paths, username=username, fallback=fallback)
    except Exception as exc:
        # Do not persist raw exception messages: they can include payloads, tokens or paths.
        return _finish(data, "failed", getattr(exc, "code", "preflight_" + type(exc).__name__), db_path=db_path)
    data, accepted = _record_target(data, prepared["target"], allow_target_change=allow_target_change,
                                    db_path=db_path, prepared_hash=prepared_hash)
    if not accepted:
        return data
    data = _start_send(data, db_path=db_path)
    try:
        result = relay.send(body, stream=event.stream, channel_id=channel_id, files=paths or None,
                            username=username, prepared=prepared)
    except Exception:
        return _finish(data, "uncertain", "relay_response_lost", db_path=db_path)
    # False can mean partial chunks or a swallowed response error. None/malformed verdicts
    # are equally ambiguous. Never infer 'no send' from the legacy boolean API.
    return _finish(data, "sent" if result is True else "uncertain",
                   None if result is True else "relay_failed_or_unconfirmed", db_path=db_path)


def main(argv=None):
    """JSON stdin/stdout boundary for consumers; no provider or business subprocess dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("deliver", "get"))
    args = parser.parse_args(argv)
    try:
        binary_input = getattr(sys.stdin, "buffer", None)
        request = json.loads(binary_input.read().decode("utf-8") if binary_input else sys.stdin.read())
        if args.command == "get":
            receipt = get_receipt(request["event_id"])
        else:
            fields = dict(request["event"])
            supplied_id = fields.pop("event_id", None)
            event = Event(**fields)
            if supplied_id is not None and supplied_id != event.event_id:
                raise ReceiptError("event_id does not match run_id/phase/condition")
            options = request.get("delivery", {})
            allowed = {"channel_id", "files", "username", "fallback", "language", "verbatim",
                       "retry_failed", "allow_target_change", "lease_seconds", "command"}
            if not isinstance(options, dict) or set(options) - allowed:
                raise ReceiptError("unsupported delivery options")
            receipt = deliver(event, request["content"], **options)
        ok = receipt is not None and (args.command == "get" or receipt["state"] == "sent")
        print(_json({"api_version": 1, "ok": ok, "receipt": receipt}))
        return 0 if ok else 1
    except Exception as exc:
        # No input, content, credential, or raw transport error is echoed by this boundary.
        print(_json({"api_version": 1, "ok": False, "error": type(exc).__name__,
                     "reason": str(exc) if isinstance(exc, ReceiptError) else "invalid request or storage failure"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
