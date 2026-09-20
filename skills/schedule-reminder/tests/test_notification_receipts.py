"""Synthetic receipt tests: real SQLite/processes, fake relay only, explicit dependencies."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import notification_receipts as receipts
import relay
BOT_POST = relay._post_bot


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for name, path in {
        "HOME": tmp_path, "USERPROFILE": tmp_path,
        "SCHEDULE_DB_PATH": tmp_path / "reminder.sqlite3",
        "AGENT_CENTER_RUNS": tmp_path / "runs",
        "AGENT_CENTER_CONFIG": tmp_path / "registry.json",
    }.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)
    def no_network(*args, **kwargs):
        raise AssertionError("network forbidden in receipt tests")
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(relay.urllib.request, "urlopen", no_network)
    candidate = os.environ.get("SCHEDULE_RECEIPT_STORE_CANDIDATE")
    if candidate:
        spec = importlib.util.spec_from_file_location("_t15_staged_store", candidate)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(receipts, "store", module)
    receipts.store.init_db()
    registry = {"streams": {"test": {"channel_id": "10001"}},
                "reader": {"bot_token": "SYNTHETIC_TOKEN"},
                "big_brother": {"user_id": "20002"}}
    def configure(value):
        (tmp_path / "registry.json").write_text(json.dumps(value), encoding="utf-8")
    configure(registry)
    calls = []
    monkeypatch.setattr(relay, "_post_bot", lambda channel, text, files, token:
                        calls.append((channel, text, files)) or True)
    return tmp_path, registry, configure, calls


def event(**kwargs):
    return receipts.Event(**dict(dict(owner="test-owner", run_id="synthetic-run",
        phase="terminal", condition="success", stream="test"), **kwargs))


def send(**kwargs):
    return receipts.deliver(event(), "任务已完成", **kwargs)


def expire():
    with receipts._connection(None) as conn, receipts.store._Tx(conn):
        row = receipts._read(conn, event().event_id)
        row["expires_at"] = 0
        receipts._write(conn, row)


def test_sent_reentry_and_provider_attempts_do_not_duplicate(sandbox):
    first = send()
    assert first["state"] == "sent"
    for _ in range(3):
        assert send(retry_failed=True) == first
    assert len(sandbox[3]) == 1
    assert first["event_id"] == receipts.event_id("synthetic-run", "terminal", "success")


def test_event_identity_does_not_alias_delimiters():
    assert receipts.event_id("a:b", "c", "d") != receipts.event_id("a", "b:c", "d")
    with pytest.raises(receipts.ReceiptError):
        event(stream="")


@pytest.mark.parametrize("change", [{"stream": "elsewhere"}, {"owner": "other-owner"}])
def test_same_event_cannot_switch_owner_or_stream(sandbox, change):
    send()
    with pytest.raises(receipts.ReceiptError, match="event conflict"):
        receipts.deliver(event(**change), "任务已完成")
    assert len(sandbox[3]) == 1


def test_same_event_cannot_change_message(sandbox):
    send()
    with pytest.raises(receipts.ReceiptError, match="event conflict"):
        receipts.deliver(event(), "任务失败")


def test_preflight_failure_requires_explicit_delivery_retry(sandbox):
    _, registry, configure, calls = sandbox
    configure({"streams": {"test": {"channel_id": "10001"}}})
    failed = send()
    assert failed["state"] == "failed" and failed["retry_safe"]
    assert "credentials" in failed["error"]
    configure(registry)
    assert send() == failed
    assert calls == []
    assert send(retry_failed=True)["state"] == "sent"
    assert len(calls) == 1


@pytest.mark.parametrize("verdict", [False, None, {}, "sent"])
def test_failed_empty_or_malformed_relay_verdict_is_uncertain(sandbox, monkeypatch, verdict):
    calls = []
    monkeypatch.setattr(relay, "send", lambda *a, **kw: calls.append(1) or verdict)
    assert send()["state"] == "uncertain"
    assert send(retry_failed=True)["state"] == "uncertain"
    assert calls == [1]


def test_response_lost_is_not_replayed(sandbox, monkeypatch):
    calls = []
    def lost(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("SYNTHETIC_SECRET must not enter a receipt")
    monkeypatch.setattr(relay, "send", lost)
    row = send()
    assert row["state"] == "uncertain" and not row["retry_safe"]
    assert "SYNTHETIC_SECRET" not in json.dumps(row)
    send(retry_failed=True)
    assert calls == [1]


def test_reentrant_sender_sees_pending_claim(sandbox, monkeypatch):
    nested = []
    def transport(*args, **kwargs):
        nested.append(send())
        return True
    monkeypatch.setattr(relay, "send", transport)
    assert send()["state"] == "sent"
    assert len(nested) == 1 and nested[0]["state"] == "pending"


def test_abandoned_pre_send_target_change_requires_opt_in(sandbox, monkeypatch):
    start = receipts._start_send
    def crash(*args, **kwargs):
        raise SystemExit("synthetic crash before send")
    monkeypatch.setattr(receipts, "_start_send", crash)
    with pytest.raises(SystemExit):
        send()
    expire()
    monkeypatch.setattr(receipts, "_start_send", start)
    _, registry, configure, calls = sandbox
    registry["streams"]["test"]["channel_id"] = "30003"
    configure(registry)
    blocked = send(retry_failed=True)
    assert blocked["state"] == "failed" and blocked["error"] == "target_changed"
    assert blocked["target_changes"][-1]["accepted"] is False
    assert calls == []
    sent = send(retry_failed=True, allow_target_change=True)
    assert sent["state"] == "sent" and sent["target"]["channel_id"] == "30003"
    assert sent["target_changes"][-1]["accepted"] is True


@pytest.mark.parametrize("damage", ["not-json", "[]", '{"version":1}'])
def test_malformed_receipt_fails_closed(sandbox, damage):
    send()
    with receipts._connection(None) as conn:
        conn.execute("UPDATE notification_receipts SET receipt=?", (damage,))
    with pytest.raises(receipts.ReceiptError, match="malformed"):
        send(retry_failed=True)
    assert len(sandbox[3]) == 1


def test_missing_schema_never_bootstraps_second_database(sandbox):
    with receipts._connection(None) as conn:
        conn.execute("DROP TABLE notification_receipts")
    with pytest.raises(receipts.ReceiptError, match="schema missing"):
        send()
    assert sandbox[3] == []


def test_v3_upgrade_preserves_existing_work_order_and_v1_item(sandbox, monkeypatch):
    with receipts._connection(None) as conn:
        conn.execute("INSERT INTO items(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                     ("synthetic-item", "synthetic task", "2026-01-01", "2026-01-01"))
        conn.execute("INSERT INTO agent_operations(item_id,generation,run_id,attempt_id,checkpoint,"
                     "created_at,updated_at,started_at,cleanup_state,cleanup_receipt) "
                     "VALUES(?,1,?,?,?,?,?,?,?,?)", ("synthetic-item", "synthetic-run", "synthetic-attempt",
                     "synthetic-checkpoint", "2026-01-01", "2026-01-01", "2026-01-01",
                     "quiescent", '{"synthetic":true}'))
        before = tuple(conn.execute("SELECT * FROM agent_operations").fetchone())
        item = tuple(conn.execute("SELECT * FROM items").fetchone())
        conn.execute("DROP TABLE notification_receipts")
        conn.execute("PRAGMA user_version = 3")
    def no_remigration(*args):
        pytest.fail("T15 must not rerun T14 migrations on a v3 database")
    monkeypatch.setattr(receipts.store, "_migrate_agent_operations", no_remigration)
    monkeypatch.setattr(receipts.store, "_migrate_work_evidence", no_remigration)
    receipts.store.init_db()
    receipts.store.init_db()
    with receipts._connection(None) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        assert tuple(conn.execute("SELECT * FROM agent_operations").fetchone()) == before
        assert tuple(conn.execute("SELECT * FROM items").fetchone()) == item
    assert send()["state"] == "sent"


def test_missing_stream_has_no_secret_default_fallback(sandbox, monkeypatch):
    sandbox[2]({"streams": {}})
    called = []
    monkeypatch.setattr(relay, "_big_brother", lambda text: called.append(text) or True)
    assert send()["error"] == "missing_stream"
    assert called == []


def test_explicit_legacy_fallback_records_target_change(sandbox, monkeypatch):
    _, registry, configure, _ = sandbox
    registry["streams"] = {}
    configure(registry)
    called = []
    monkeypatch.setattr(relay, "_big_brother", lambda text: called.append(text) or True)
    row = send(fallback="big_brother")
    assert row["state"] == "sent" and row["target"]["kind"] == "big_brother"
    assert row["target_changes"][0]["to"]["fallback"] is True
    assert called == ["[test] 任务已完成"]


def test_explicit_channel_does_not_fallback(sandbox, monkeypatch):
    sandbox[2]({"streams": {}, "big_brother": {"user_id": "20002"}})
    monkeypatch.setattr(relay, "_big_brother", lambda text: pytest.fail("unexpected fallback"))
    assert send(channel_id="30003", fallback="big_brother")["error"] == "missing_credentials"


@pytest.mark.parametrize("text,error", [("", "empty_content"), ("completed", "language_policy_rejected"),
                                          ("任務已完成", "language_policy_rejected")])
def test_language_and_empty_contract(sandbox, text, error):
    assert receipts.deliver(event(), text)["error"] == error
    assert sandbox[3] == []


def test_canonical_language_preserves_declared_verbatim(sandbox):
    row = receipts.deliver(event(), "Model-A 模型已更新", verbatim=["Model-A"])
    assert row["state"] == "sent"
    assert sandbox[3][0][1] == "Model-A 模型已更新"


def test_missing_language_dependency_is_failure(sandbox):
    row = send(language_policy_path=str(sandbox[0] / "missing.py"))
    assert row["state"] == "failed" and row["error"] == "preflight_ReceiptError"
    assert sandbox[3] == []


def test_production_default_missing_policy_does_not_search_real_home(sandbox, monkeypatch):
    monkeypatch.delenv("SCHEDULE_NOTIFICATION_LANGUAGE_POLICY", raising=False)
    with pytest.raises(receipts.ReceiptError, match="language rule is not installed"):
        receipts.load_language_policy()


def test_explicit_preserve_language_and_canonical_redactor(sandbox):
    seen = []
    def maintained_redactor(text):
        seen.append(text)
        return "redacted"
    row = receipts.deliver(event(), "synthetic", language="preserve", redactor=maintained_redactor)
    assert row["state"] == "sent" and seen == ["synthetic"]
    assert sandbox[3][0][1] == "redacted"


def test_attachments_and_explicit_channel_preserved(sandbox):
    picture = sandbox[0] / "fixture.bin"
    picture.write_bytes(b"SYNTHETIC_ATTACHMENT")
    row = send(channel_id="30003", files=[picture], username="test-identity")
    assert row["state"] == "sent"
    assert sandbox[3] == [("30003", "任务已完成", [str(picture)])]


def test_missing_attachment_fails_before_transport(sandbox):
    row = send(files=[sandbox[0] / "missing.bin"])
    assert row["state"] == "failed" and row["retry_safe"]
    assert sandbox[3] == []


def test_changed_attachment_is_not_silently_retried(sandbox, monkeypatch):
    file = sandbox[0] / "fixture.bin"
    file.write_bytes(b"SYNTHETIC_ORIGINAL")
    start = receipts._start_send
    def crash(*args, **kwargs):
        raise SystemExit("synthetic pre-send crash")
    monkeypatch.setattr(receipts, "_start_send", crash)
    with pytest.raises(SystemExit):
        send(files=[file])
    expire()
    file.write_bytes(b"SYNTHETIC_CHANGED")
    monkeypatch.setattr(receipts, "_start_send", start)
    row = send(files=[file], retry_failed=True)
    assert row["state"] == "failed" and row["error"] == "prepared_payload_changed"
    assert sandbox[3] == []


def test_length_uses_canonical_chunker_partial_is_uncertain(sandbox, monkeypatch):
    _, registry, configure, _ = sandbox
    registry["streams"]["test"]["webhook"] = "https://example.com/synthetic"
    configure(registry)
    parts = []
    def post(url, payload):
        parts.append(payload)
        return len(parts) != 2
    monkeypatch.setattr(relay, "_post_webhook", post)
    body = "任务已完成" * 1000
    row = receipts.deliver(event(), body, username="owner-identity")
    assert [p["content"] for p in parts] == relay.split_for_discord(body)
    assert all(p["username"] == "owner-identity" for p in parts)
    assert row["state"] == "uncertain"
    assert receipts.deliver(event(), body, username="owner-identity", retry_failed=True) == row


def test_pinned_route_survives_registry_change_before_send(sandbox, monkeypatch):
    start = receipts._start_send
    def change(*args, **kwargs):
        row = start(*args, **kwargs)
        sandbox[1]["streams"]["test"]["channel_id"] = "30003"
        sandbox[2](sandbox[1])
        return row
    monkeypatch.setattr(receipts, "_start_send", change)
    row = send()
    assert row["target"]["channel_id"] == sandbox[3][0][0] == "10001"


def test_actual_bot_transport_lost_response_is_uncertain(sandbox, monkeypatch):
    calls = []
    def lost_response(request, timeout=None):
        calls.append(request)
        raise TimeoutError("synthetic response lost")
    monkeypatch.setattr(relay, "_post_bot", BOT_POST)
    monkeypatch.setattr(relay.urllib.request, "urlopen", lost_response)
    assert send()["state"] == "uncertain"
    assert send(retry_failed=True)["state"] == "uncertain"
    assert len(calls) == 1


def test_actual_attachment_transport_chunks_files_once(sandbox, monkeypatch):
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    def accept(request, timeout=None):
        calls.append(request)
        return Response()
    monkeypatch.setattr(relay, "_post_bot", BOT_POST)
    monkeypatch.setattr(relay.urllib.request, "urlopen", accept)
    attachment = sandbox[0] / "fixture.bin"
    attachment.write_bytes(b"SYNTHETIC_ATTACHMENT_BYTES")
    row = receipts.deliver(event(), "完成" * 3000, files=[attachment])
    assert row["state"] == "sent" and len(calls) > 1
    assert all(req.full_url.endswith("/channels/10001/messages") for req in calls)
    assert sum(b"SYNTHETIC_ATTACHMENT_BYTES" in req.data for req in calls) == 1


def test_expired_predecessor_is_fenced_before_send(sandbox):
    previous, acquired = receipts._claim(event(), "a" * 64, retry_failed=False,
                                         lease_seconds=300, db_path=None)
    assert acquired
    expire()
    current, acquired = receipts._claim(event(), "a" * 64, retry_failed=True,
                                        lease_seconds=300, db_path=None)
    assert acquired and current["token"] != previous["token"]
    target = relay.prepare_send(stream="test")["target"]
    with pytest.raises(receipts.ReceiptError, match="claim lost"):
        receipts._record_target(previous, target, allow_target_change=True, db_path=None)
    assert sandbox[3] == []


def test_expiry_during_send_cannot_enable_another_sender(sandbox, monkeypatch):
    observed = []
    def transport(*args, **kwargs):
        expire()
        observed.append(send(retry_failed=True))
        return True
    monkeypatch.setattr(relay, "send", transport)
    assert send()["state"] == "sent"
    assert len(observed) == 1 and observed[0]["state"] == "uncertain"


def test_dryrun_cannot_make_durable_sent_receipt(sandbox, monkeypatch):
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    row = send()
    assert row["state"] == "failed" and row["error"] == "dry_run_is_not_delivery"
    assert sandbox[3] == []


def test_boolean_retry_policy_cannot_be_string(sandbox):
    with pytest.raises(receipts.ReceiptError, match="booleans"):
        send(retry_failed="false")
    assert sandbox[3] == []


def test_json_cli_and_notify_compatibility_delegate_to_producer(sandbox, monkeypatch, capsys):
    import notify
    request = {"event": event().fields(), "content": "任务已完成"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert receipts.main(["deliver"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["state"] == "sent"
    assert notify.notify_event(event(), "任务已完成") == result["receipt"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"event_id": event().event_id})))
    assert receipts.main(["get"]) == 0
    assert json.loads(capsys.readouterr().out)["receipt"] == result["receipt"]
    assert len(sandbox[3]) == 1


def test_cli_conflicting_id_fails_without_send(sandbox, monkeypatch, capsys):
    fields = event().fields()
    fields["event_id"] = "invalid"
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"event": fields, "content": "完成"})))
    assert receipts.main(["deliver"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert sandbox[3] == []


CHILD = r'''
import os, sys, socket, time
from pathlib import Path
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network forbidden"))
sys.path.insert(0, os.environ["TEST_RECEIPT_SCRIPTS"])
import notification_receipts as nr
import relay
outbox = Path(os.environ["TEST_OUTBOX"])
mode = sys.argv[1]
def fake_send(*args, **kwargs):
    with outbox.open("a", encoding="utf-8") as f:
        f.write("synthetic-send\n")
    if mode == "after-send":
        os._exit(21)
    return True
relay.send = fake_send
if mode == "before-send":
    nr._start_send = lambda *a, **k: os._exit(20)
if mode == "after-boundary":
    relay.send = lambda *a, **k: os._exit(22)
if mode == "compete":
    gate = Path(os.environ["TEST_GATE"])
    deadline = time.monotonic() + 10
    while not gate.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("gate timeout")
        time.sleep(.01)
event = nr.Event("test-owner", "synthetic-run", "terminal", "success", "test")
result = nr.deliver(event, "任务已完成")
print(result["state"])
'''


def child_env(sandbox):
    temp = sandbox[0]
    return dict(os.environ, SCHEDULE_DB_PATH=str(temp / "reminder.sqlite3"),
                AGENT_CENTER_RUNS=str(temp / "runs"), HOME=str(temp), USERPROFILE=str(temp),
                TEST_RECEIPT_SCRIPTS=str(SCRIPTS), TEST_OUTBOX=str(temp / "outbox.txt"),
                TEST_GATE=str(temp / "gate"), PYTHONDONTWRITEBYTECODE="1")


def test_subprocess_cli_utf8_stdin_ignores_windows_locale(sandbox):
    code = r'''
import os, sys, socket, runpy
socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network forbidden"))
sys.path.insert(0, os.environ["TEST_RECEIPT_SCRIPTS"])
import relay
relay.send = lambda text, **kwargs: text == "\u4efb\u52a1\u5df2\u5b8c\u6210"
sys.argv = ["notification_receipts.py", "deliver"]
runpy.run_path(os.path.join(os.environ["TEST_RECEIPT_SCRIPTS"], "notification_receipts.py"), run_name="__main__")
'''
    payload = json.dumps({"event": event().fields(), "content": "任务已完成"}, ensure_ascii=False).encode("utf-8")
    result = subprocess.run([sys.executable, "-c", code], input=payload, capture_output=True,
                            env=dict(child_env(sandbox), PYTHONIOENCODING="cp1252"), timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert json.loads(result.stdout.decode("utf-8"))["receipt"]["state"] == "sent"


def test_real_sqlite_competing_process_claims(sandbox):
    env = child_env(sandbox)
    children = [subprocess.Popen([sys.executable, "-c", CHILD, "compete"], env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(4)]
    (sandbox[0] / "gate").touch()
    try:
        for child in children:
            stdout, stderr = child.communicate(timeout=20)
            assert child.returncode == 0, stderr
            assert stdout.strip() in ("sent", "pending")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
    assert (sandbox[0] / "outbox.txt").read_text().splitlines() == ["synthetic-send"]
    assert receipts.get_receipt(event().event_id)["state"] == "sent"


@pytest.mark.parametrize("mode,state,count", [("before-send", "failed", 0),
    ("after-boundary", "uncertain", 0), ("after-send", "uncertain", 1)])
def test_real_process_crash_windows(sandbox, mode, state, count):
    child = subprocess.run([sys.executable, "-c", CHILD, mode], env=child_env(sandbox),
                           capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert child.returncode in (20, 21, 22), child.stderr
    expire()
    assert send()["state"] == state
    outbox = sandbox[0] / "outbox.txt"
    assert (len(outbox.read_text().splitlines()) if outbox.exists() else 0) == count
    retry = send(retry_failed=True)
    assert retry["state"] == ("sent" if state == "failed" else "uncertain")
    assert len(sandbox[3]) == (1 if state == "failed" else 0)
