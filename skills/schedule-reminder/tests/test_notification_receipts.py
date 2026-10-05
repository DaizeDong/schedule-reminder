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
from make_fixtures import notification_case

CASE = notification_case()

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
    receipts.store.init_db()
    registry = notification_case()['registry']
    def configure(value):
        (tmp_path / "registry.json").write_text(json.dumps(value), encoding="utf-8")
    configure(registry)
    calls = []
    monkeypatch.setattr(relay, "_post_bot", lambda channel, text, files, token:
                        calls.append((channel, text, [os.fspath(f) for f in files] if files else files)) or True)
    return tmp_path, registry, configure, calls


def event(**kwargs):
    return receipts.Event(**dict(CASE['event'], **kwargs))


def send(**kwargs):
    return receipts.deliver(event(), CASE["content"], **kwargs)


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


def test_explicit_occurrence_reentry_does_not_send_twice(sandbox, monkeypatch):
    """Occurrence reentry must return the stored result before entering transport again."""
    import notify
    monkeypatch.setenv("SCHEDULE_RELAY_STREAM", CASE["event"]["stream"])
    assert notify.notify(CASE["content"], run_id=CASE["event"]["run_id"]) is True
    assert notify.notify(CASE["content"], run_id=CASE["event"]["run_id"]) is True
    assert len(sandbox[3]) == 1


def test_event_identity_does_not_alias_delimiters():
    assert receipts.event_id("a:b", "c", "d") != receipts.event_id("a", "b:c", "d")
    with pytest.raises(receipts.ReceiptError):
        event(stream="")


@pytest.mark.parametrize("change", [{"stream": "elsewhere"}, {"owner": "other-owner"}])
def test_same_event_cannot_switch_owner_or_stream(sandbox, change):
    send()
    with pytest.raises(receipts.ReceiptError, match="event conflict"):
        receipts.deliver(event(**change), CASE["content"])
    assert len(sandbox[3]) == 1


def test_same_event_cannot_change_message(sandbox):
    send()
    with pytest.raises(receipts.ReceiptError, match="event conflict"):
        receipts.deliver(event(), CASE["changed_content"])


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


def test_retained_schema_upgrade_preserves_existing_work_order_and_item(sandbox):
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
        conn.execute("PRAGMA user_version = 5")
    receipts.store.init_db()
    receipts.store.init_db()
    with receipts._connection(None) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == receipts.store.SCHEMA_USER_VERSION
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
    monkeypatch.delenv("SCHEDULE_ROUTE_SCRIPTS_DIR", raising=False)
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
    picture.write_bytes(CASE["attachment"].encode("ascii"))
    row = send(channel_id="30003", files=[picture], username="test-identity")
    assert row["state"] == "sent"
    assert sandbox[3] == [("30003", CASE["content"], [str(picture)])]


def test_missing_attachment_fails_before_transport(sandbox):
    row = send(files=[sandbox[0] / "missing.bin"])
    assert row["state"] == "failed" and row["retry_safe"]
    assert sandbox[3] == []


def test_changed_attachment_is_not_silently_retried(sandbox, monkeypatch):
    file = sandbox[0] / "fixture.bin"
    file.write_bytes(CASE["attachment"].encode("ascii"))
    start = receipts._start_send
    def crash(*args, **kwargs):
        raise SystemExit("synthetic pre-send crash")
    monkeypatch.setattr(receipts, "_start_send", crash)
    with pytest.raises(SystemExit):
        send(files=[file])
    expire()
    file.write_bytes(CASE["changed_attachment"].encode("ascii"))
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
    body = CASE["content"] * 1000
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
    attachment.write_bytes(CASE["attachment"].encode("ascii"))
    row = receipts.deliver(event(), "完成" * 3000, files=[attachment])
    assert row["state"] == "sent" and len(calls) > 1
    assert all(req.full_url.endswith("/channels/10001/messages") for req in calls)
    assert sum(CASE["attachment"].encode("ascii") in req.data for req in calls) == 1


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
    request = {"event": event().fields(), "content": CASE["content"]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    assert receipts.main(["deliver"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["receipt"]["state"] == "sent"
    assert notify.notify_event(event(), CASE["content"]) == result["receipt"]
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
sys.path.insert(0, str(Path(os.environ["TEST_RECEIPT_SCRIPTS"]).parents[2] / "tools"))
from make_fixtures import notification_case
CASE = notification_case()
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
event = nr.Event(**CASE["event"])
result = nr.deliver(event, CASE["content"])
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
    payload = json.dumps({"event": event().fields(), "content": CASE["content"]}, ensure_ascii=False).encode("utf-8")
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


def test_prepared_attachment_bytes_survive_change_after_send_marker(sandbox, monkeypatch):
    attachment = sandbox[0] / "fixture.bin"
    original = CASE["attachment"].encode("ascii")
    changed = CASE["changed_attachment"].encode("ascii")
    attachment.write_bytes(original)
    requests = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def accept(request, timeout=None):
        requests.append(request)
        return Response()

    start = receipts._start_send

    def rewrite(*args, **kwargs):
        receipt = start(*args, **kwargs)
        attachment.write_bytes(changed)
        return receipt

    monkeypatch.setattr(receipts, "_start_send", rewrite)
    monkeypatch.setattr(relay, "_post_bot", BOT_POST)
    monkeypatch.setattr(relay.urllib.request, "urlopen", accept)
    receipt = send(files=[attachment])
    assert receipt["state"] == "sent"
    assert len(requests) == 1
    assert original in requests[0].data
    assert changed not in requests[0].data


def test_prepared_registry_does_not_alias_a_mutable_loader_result(sandbox, monkeypatch):
    registry = sandbox[1]
    original_channel = registry["streams"]["test"]["channel_id"]
    monkeypatch.setattr(relay, "load_registry", lambda: registry)
    start = receipts._start_send

    def change(*args, **kwargs):
        receipt = start(*args, **kwargs)
        registry["streams"]["test"]["channel_id"] = registry["big_brother"]["user_id"]
        return receipt

    monkeypatch.setattr(receipts, "_start_send", change)
    assert send()["state"] == "sent"
    assert sandbox[3][0][0] == original_channel


def test_message_id_delivery_contract_still_confirms_external_receipt(sandbox, monkeypatch):
    from make_fixtures import cases
    external = cases()["external_receipt"]
    message_id = external["receipt_id"]

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps({"id": message_id}).encode("utf-8")

    monkeypatch.setattr(relay.urllib.request, "urlopen", lambda *args, **kwargs: Response())
    delivered = relay.deliver(CASE["event"]["stream"], CASE["content"])
    assert delivered == {"kind": "discord-message", "receipt_id": message_id,
                         "delivered": True, "exit_code": 0}
    assert receipts.get_receipt(event().event_id) is None


def test_legacy_notify_does_not_infer_occurrence_from_task_environment(sandbox, monkeypatch):
    import notify
    monkeypatch.setenv("TASK_RUN_ID", CASE["event"]["run_id"])
    sent = []
    monkeypatch.setattr(notify, "_run", lambda argv: sent.append(argv) or True)
    assert notify.notify(CASE["content"]) is True
    assert len(sent) == 1
    assert receipts.get_receipt(event().event_id) is None


def test_missing_receipt_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(receipts.store.SkillError, match="initialize"):
        receipts.get_receipt(event().event_id, db_path=str(missing))
    assert not missing.exists()


def test_receipt_read_and_write_recheck_private_admission(sandbox):
    from make_fixtures import cases
    send()
    metadata = sandbox[0] / ".git" / "config"
    metadata.write_text('[remote "origin"]\nurl = ' + cases()["public_remote"], encoding="utf-8")
    with pytest.raises(receipts.store.SkillError, match="PUBLIC"):
        receipts.get_receipt(event().event_id)
    with pytest.raises(receipts.store.SkillError, match="PUBLIC"):
        send(retry_failed=True)
    assert len(sandbox[3]) == 1


def test_client_does_not_claim_unpersisted_after_delivery_receipt_failure(sandbox, monkeypatch):
    import notification_client
    original = receipts._finish

    def fail_after_send(data, state, error, **kwargs):
        if state == "sent":
            raise OSError("synthetic receipt commit failed")
        return original(data, state, error, **kwargs)

    monkeypatch.setattr(receipts, "_finish", fail_after_send)
    result = notification_client.submit(**CASE["event"], content=CASE["content"])
    assert len(sandbox[3]) == 1
    assert result["state"] == "reconcile"
    assert result["persisted"] is None
    assert result["event_id"] == event().event_id
    assert receipts.get_receipt(event().event_id)["send_started_at"] is not None
    assert send(retry_failed=True)["state"] == "pending"
    assert len(sandbox[3]) == 1


@pytest.mark.parametrize("field,value", [
    ("prepared_sha256", None), ("prepared_sha256", "z" * 64),
    ("target_stream", "other-stream"),
    ("target_changes", [{"accepted": True}]),
    ("payload_artifact", "notification-payloads/../message.txt"),
    ("payload_artifact", "/notification-payloads/" + "a" * 32 + ".txt"),
    ("payload_artifact", "notification-payloads/" + "a" * 32 + ".txt"),
])
def test_corrupt_sent_receipt_requires_reconciliation(sandbox, field, value):
    send()
    with receipts._connection(None) as connection, receipts.store._Tx(connection):
        row = receipts._read(connection, event().event_id)
        if field == "target_stream":
            row["target"]["stream"] = value
        else:
            row[field] = value
        receipts._write(connection, row)
    with pytest.raises(receipts.ReceiptError, match="malformed"):
        send(retry_failed=True)
    assert len(sandbox[3]) == 1


def test_invalid_webhook_identity_is_preflight_failure_without_corrupt_receipt(sandbox):
    registry = sandbox[1]
    registry["streams"]["test"]["webhook"] = "https://example.com/synthetic"
    registry["streams"]["test"]["username"] = 123
    sandbox[2](registry)
    failed = send()
    assert failed["state"] == "failed" and failed["retry_safe"]
    assert failed["error"] == "invalid_username"
    assert receipts.get_receipt(event().event_id) == failed
    registry["streams"]["test"]["username"] = CASE["event"]["owner"]
    sandbox[2](registry)
    assert send() == failed


@pytest.fixture
def native_command_process(monkeypatch):
    """Expose only installed deterministic process transport beneath the blocked model stub."""
    specification = None
    for finder in sys.meta_path:
        find = getattr(finder, "find_spec", None)
        if find is not None:
            specification = find("llmcall", None)
            if specification is not None:
                break
    assert specification is not None and specification.submodule_search_locations
    monkeypatch.setattr(sys.modules["llmcall"], "__path__",
                        list(specification.submodule_search_locations), raising=False)
    from llmcall import process
    run = process.run
    results = []

    def capture(*args, **kwargs):
        result = run(*args, **kwargs)
        results.append((args[0], result))
        return result

    monkeypatch.setattr(process, "run", capture)
    return results


@pytest.mark.parametrize("payload", ["text", "base64", "at-file"])
def test_native_command_payload_protocols_use_one_durable_attempt(sandbox, native_command_process,
                                                                 monkeypatch, payload):
    import base64
    import notification_client
    code = CASE["command_file_code"] if payload == "at-file" else CASE["command_echo_code"]
    policy = notification_client.command_policy([sys.executable, "-X", "utf8", "-c", code],
                                                payload=payload, cwd=sandbox[0])
    if payload == "at-file":
        source = sandbox[0] / "message.txt"
        source.write_text(CASE["content"], encoding="utf-8")
        policy["message_file"] = str(source)
        start = receipts._start_send

        def rewrite_original(*args, **kwargs):
            receipt = start(*args, **kwargs)
            source.write_text(CASE["changed_content"], encoding="utf-8")
            return receipt

        monkeypatch.setattr(receipts, "_start_send", rewrite_original)
    result = send(command=policy)
    assert result["state"] == "sent"
    assert send(command=policy, retry_failed=True) == result
    assert len(native_command_process) == 1
    argv, output = native_command_process[0]
    expected = (base64.b64encode(CASE["content"].encode("utf-8")).decode("ascii")
                if payload == "base64" else CASE["content"])
    assert output.stdout.rstrip("\r\n") == expected
    assert result["target"]["kind"] == "command"
    assert sandbox[3] == []
    if payload == "at-file":
        snapshot = Path(argv[-1][1:])
        assert snapshot != source
        assert snapshot.parent == sandbox[0] / "notification-payloads"
        assert not snapshot.exists()


def test_command_policy_is_pinned_before_owner_redaction(sandbox, native_command_process):
    import notification_client
    policy = notification_client.command_policy(
        [sys.executable, "-X", "utf8", "-c", CASE["command_echo_code"]], cwd=sandbox[0])

    def redact(text):
        policy["argv"][0] = str(sandbox[0] / "missing-notifier")
        return text

    result = send(command=policy, redactor=redact)
    assert result["state"] == "sent"
    assert len(native_command_process) == 1
    assert native_command_process[0][1].stdout.rstrip("\r\n") == CASE["content"]


def test_custom_notifier_missing_executable_fails_before_send(sandbox, native_command_process):
    import notification_client
    policy = notification_client.command_policy([str(sandbox[0] / "missing-notifier")])
    result = send(command=policy)
    assert result["state"] == "failed" and result["retry_safe"]
    assert result["error"] == "custom_notifier_missing"
    assert native_command_process == []
    assert sandbox[3] == []


def test_command_snapshot_flush_failure_cleans_owned_private_file(sandbox, native_command_process,
                                                                 monkeypatch):
    import notification_client
    source = sandbox[0] / "message.txt"
    source.write_text(CASE["content"], encoding="utf-8")
    policy = notification_client.command_policy([sys.executable, "-X", "utf8", "-c", CASE["command_file_code"]],
                                                payload="at-file", cwd=sandbox[0])
    policy["message_file"] = str(source)

    def fail_flush(_):
        raise OSError("synthetic flush failure")

    monkeypatch.setattr(relay.os, "fsync", fail_flush)
    result = send(command=policy)
    assert result["state"] == "failed" and result["retry_safe"]
    assert not list((sandbox[0] / "notification-payloads").glob("*.txt"))
    assert native_command_process == []


@pytest.mark.parametrize('cleanup,outcome', [
    (False, 'failed'), (None, 'failed'), ('response-lost', 'failed'),
    (False, 'success'), (None, 'success'),
])
def test_unconfirmed_command_cleanup_retains_snapshot_for_reconciliation(sandbox, native_command_process,
                                                                        monkeypatch, cleanup, outcome):
    from types import SimpleNamespace
    from llmcall import process
    import notification_client
    source = sandbox[0] / 'message.txt'
    source.write_text(CASE['content'], encoding='utf-8')
    policy = notification_client.command_policy(
        [sys.executable, '-X', 'utf8', '-c', CASE['command_file_code']], payload='at-file', cwd=sandbox[0])
    policy['message_file'] = str(source)
    snapshots = []

    def unresolved(argv, *args, **kwargs):
        snapshots.append(Path(argv[-1][1:]))
        if cleanup == 'response-lost':
            raise TimeoutError('synthetic process response lost')
        return SimpleNamespace(outcome=outcome, error=None if outcome == 'success' else 'synthetic error',
                               cleanup_confirmed=cleanup)

    monkeypatch.setattr(process, 'run', unresolved)
    receipt = send(command=policy)
    assert receipt['state'] == 'uncertain'
    assert snapshots[0].read_text(encoding='utf-8') == CASE['content']
    assert receipt['payload_artifact'] == str(snapshots[0].relative_to(sandbox[0])).replace('\\', '/')
    assert send(command=policy, retry_failed=True) == receipt
    assert len(snapshots) == 1


def test_native_command_timeout_cleans_snapshot_only_after_process_cleanup(sandbox, native_command_process):
    import notification_client
    source = sandbox[0] / 'message.txt'
    source.write_text(CASE['content'], encoding='utf-8')
    policy = notification_client.command_policy(
        [sys.executable, '-X', 'utf8', '-c', CASE['command_wait_code']],
        payload='at-file', cwd=sandbox[0], timeout=2)
    policy['message_file'] = str(source)
    receipt = send(command=policy)
    assert receipt['state'] == 'uncertain'
    assert len(native_command_process) == 1
    argv, result = native_command_process[0]
    assert result.execution_started and result.outcome == 'timeout'
    assert result.cleanup_confirmed is True
    snapshot = Path(argv[-1][1:])
    assert not snapshot.exists()
    assert receipt['payload_artifact'] == snapshot.relative_to(sandbox[0]).as_posix()
    assert send(command=policy, retry_failed=True) == receipt
    assert len(native_command_process) == 1


def test_legacy_reap_reports_keep_distinct_work_order_notification_identities(sandbox, monkeypatch):
    import agent_task
    import agent_tick
    monkeypatch.setenv('TASK_RUN_ID', CASE['event']['run_id'])
    monkeypatch.setattr(agent_tick, '_age_seconds', lambda item: agent_tick.CLAIM_GRACE_SECONDS + 1)
    monkeypatch.setattr(agent_tick, '_log', lambda *args: None)
    legacy_orders = [receipts.store.add_item(title, state='doing', source=agent_task.WORK_SOURCE,
        ext={agent_task.EXT_STATE: agent_task.STATE_RUNNING, agent_task.EXT_STREAM: CASE['event']['stream']})
        for title in (CASE['content'], CASE['changed_content'])]
    assert all(agent_task.operation(item['id']) is None for item in legacy_orders)

    assert agent_tick._reap_legacy(legacy_orders, post=True) == [item['id'] for item in legacy_orders]
    expected = {receipts.event_id('work-order:' + item['id'], 'terminal', 'reconcile')
                for item in legacy_orders}
    with receipts._connection(None, readonly=True) as connection:
        actual = {row[0] for row in connection.execute('SELECT event_id FROM notification_receipts')}
    assert actual == expected and len(actual) == 2
    for item in legacy_orders:
        key = receipts.event_id('work-order:' + item['id'], 'terminal', 'reconcile')
        receipt = receipts.get_receipt(key)
        assert receipt['state'] == 'sent'
        assert receipt['run_id'] == 'work-order:' + item['id']
        assert receipts.store.get_item(item['id'])['state'] == 'blocked'
    assert len(sandbox[3]) == 2
    assert agent_tick._reap_legacy(post=True) == []
    assert len(sandbox[3]) == 2
