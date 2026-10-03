"""Paired controls for final review; all runtime inputs come from the generator."""
from copy import deepcopy
import json
import os
import sqlite3

import pytest

import agent_run
import commands
import private_data
import relay
import store
from make_fixtures import schedule12_review_cases
from test_agent_exec import _Harness
from test_schedule3_boundary import native_storage

F = schedule12_review_cases()


def apply_progress(db, operation, value, item):
    if operation == "add":
        return store.add_item(F["title"], progress=value, db_path=db)
    if operation == "update":
        return store.update_item(item["id"], progress=value, db_path=db)
    return store.transition(item["id"], "doing", progress=value, db_path=db)


@pytest.mark.parametrize("operation", ["add", "update", "transition"])
@pytest.mark.parametrize("value", F["bad_progress"])
def test_invalid_progress_preserves_items_and_events(tmp_path, operation, value):
    db = str(tmp_path / "review.sqlite3")
    store.init_db(db)
    item = store.add_item(F["title"], db_path=db)
    with sqlite3.connect(db) as connection:
        before = list(connection.iterdump())
    with pytest.raises(store.SkillError) as error:
        apply_progress(db, operation, value, item)
    assert error.value.error_code == "ERR_BAD_PROGRESS"
    with sqlite3.connect(db) as connection:
        assert list(connection.iterdump()) == before


@pytest.mark.parametrize("operation", ["add", "update", "transition"])
@pytest.mark.parametrize("value", F["good_progress"])
def test_integer_progress_remains_supported(tmp_path, operation, value):
    db = str(tmp_path / "review.sqlite3")
    store.init_db(db)
    item = store.add_item(F["title"], db_path=db)
    assert apply_progress(db, operation, value, item)["progress"] == int(value)


@pytest.mark.parametrize("content", F["blank"], ids=["empty", "space", "long-newlines", "mixed"])
@pytest.mark.parametrize("operation", ["relay", "deliver", "digest"])
def test_blank_text_cannot_claim_delivery(monkeypatch, content, operation):
    monkeypatch.setattr(relay, "load_registry", lambda: pytest.fail("blank input read the registry"))
    monkeypatch.setattr(relay.urllib.request, "urlopen", lambda *a, **k: pytest.fail("blank input sent HTTP"))
    if operation == "digest":
        assert relay.digest(content) is False
    elif operation == "relay":
        assert relay.relay(F["stream"], content) is False
    else:
        with pytest.raises(ValueError, match="blank"):
            relay.deliver(F["stream"], content)


def test_newline_chunks_preserve_every_line():
    body = F["blank"][2] + F["text"] + "\n\n"
    parts = relay.split_for_discord(body)
    assert "\n".join(part.partition("\n")[2] for part in parts) == body


def test_normal_delivery_requires_a_receipt(monkeypatch):
    monkeypatch.setattr(relay, "load_registry", lambda: deepcopy(F["registry"]))
    monkeypatch.delenv("AGENT_CENTER_RELAY_DRYRUN", raising=False)
    calls = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self):
            return json.dumps(F["receipt"]).encode()
    monkeypatch.setattr(relay.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or Response())
    result = relay.deliver(F["stream"], F["text"])
    assert result["delivered"] is True and result["receipt_id"] == F["receipt"]["id"]
    assert len(calls) == 1
    monkeypatch.setattr(relay, "split_for_discord", lambda _: [])
    with pytest.raises(ValueError, match="receipt"):
        relay.deliver(F["stream"], F["text"])
    assert len(calls) == 1


def test_attachment_only_bot_delivery_remains_supported(monkeypatch):
    monkeypatch.setenv("AGENT_CENTER_RELAY_DRYRUN", "1")
    assert relay._post_bot("121", "", ["synthetic-attachment.txt"], "synthetic-token") is True


@pytest.mark.parametrize("bad", F["bad_commands"])
def test_malformed_command_keeps_valid_sibling(monkeypatch, capsys, bad):
    registry = {"commands": {F["bad_name"]: bad, F["command_name"]: F["good_command"]}}
    loaded = commands.load(registry)
    assert [row["name"] for row in loaded] == [F["command_name"]]
    assert loaded[0]["timeout"] == 3
    assert commands.match(F["command_text"], loaded) is loaded[0]
    warning = capsys.readouterr().err
    assert F["bad_name"] in warning and "skipped" in warning


@pytest.mark.parametrize("tail", F["tails"])
def test_json_string_braces_do_not_hide_verification(tail):
    text = json.dumps({"verify": None, "summary": F["text"]}) + "\n```json\n" + json.dumps(tail) + "\n```"
    assert agent_run.parse_tail(text) == tail


def test_complete_final_object_remains_selectable_after_a_malformed_example():
    case = F["recovered_tail"]
    assert agent_run.parse_tail(case["text"]) == case["expected"]


@pytest.mark.parametrize("answer", F["bad_tails"])
def test_invalid_contract_cannot_reach_judge_or_finalize(monkeypatch, tmp_path, answer):
    harness = _Harness(monkeypatch, tmp_path, [(0, F["text"])], answers=[answer or " "])
    result = agent_run._run_approach("wo-1", F["stream"], F["request"], str(tmp_path), 0, ["synthetic"], False)
    assert result["outcome"] == "stalled"
    assert harness.finished == harness.reviews == harness.verifies == []


def test_explicit_explained_null_verify_retains_review(monkeypatch, tmp_path):
    harness = _Harness(monkeypatch, tmp_path, [], answers=[F["null_tail"]])
    result = agent_run._run_approach("wo-1", F["stream"], F["request"], str(tmp_path), 0, ["synthetic"], False)
    assert result["outcome"] == "done"
    assert harness.finished and harness.reviews and harness.verifies == []


def test_quoted_failing_verifier_blocks_completion(monkeypatch, tmp_path):
    harness = _Harness(monkeypatch, tmp_path, [(1, F["text"])], answers=[json.dumps(F["tails"][0])])
    result = agent_run._run_approach("wo-1", F["stream"], F["request"], str(tmp_path), 0, ["synthetic"], False)
    assert result["outcome"] == "stalled"
    assert harness.finished == harness.reviews == []
    assert harness.verifies == [F["failing_verify"]] * 2


@pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
@pytest.mark.parametrize("name", F["unsafe_names"])
def test_windows_nonordinary_paths_fail_before_proof(tmp_path, monkeypatch, name):
    target = tmp_path / name
    monkeypatch.setattr(private_data, "_shared_boundary", lambda: pytest.fail("unsafe path reached Git proof"))
    with pytest.raises(ValueError, match="Windows"):
        private_data.open_for_write(target)


@pytest.mark.skipif(os.name != "nt", reason="Windows filename semantics")
@pytest.mark.parametrize("name", F["unsafe_names"])
@pytest.mark.parametrize("variable,resolver", [("SCHEDULE_REMINDER_CONFIG", private_data.config_root),
                         ("SCHEDULE_REMINDER_DATA_DIR", private_data.data_dir),
                         ("AGENT_CENTER_CONFIG", private_data.registry_path)])
def test_windows_config_paths_are_checked_before_resolution(tmp_path, monkeypatch, name, variable, resolver):
    monkeypatch.setenv(variable, str(tmp_path / name))
    with pytest.raises(ValueError, match="Windows"):
        resolver()


@pytest.mark.parametrize("name", F["ordinary_names"])
def test_native_ordinary_record_reaches_git_index(native_storage, name):
    target = native_storage["repository"] / name
    with private_data.open_for_write(target, encoding="utf-8") as stream:
        stream.write(F["record"])
    native_storage["git"]("add", "--", name)
    assert native_storage["git"]("show", ":" + name) == F["record"].strip()


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate stream semantics")
def test_native_alternate_stream_refused_without_changing_host_or_index(native_storage):
    root = native_storage["repository"]
    name = F["unsafe_names"][0]
    host = root / name.split(":")[0]
    host.write_text(F["record"], encoding="utf-8")
    native_storage["git"]("add", "--", host.name)
    before = native_storage["git"]("ls-files", "--stage")
    with pytest.raises(ValueError, match="Windows"):
        private_data.open_for_write(root / name, encoding="utf-8")
    assert not (root / name).exists()
    assert host.read_text(encoding="utf-8") == F["record"]
    assert native_storage["git"]("ls-files", "--stage") == before
    assert native_storage["git"]("show", ":" + host.name) == F["record"].strip()


@pytest.mark.skipif(os.name != "nt", reason="PowerShell 5.1 verifier")
def test_native_quoted_verifier_runs_and_prevents_completion(monkeypatch, tmp_path):
    original = agent_run.run_verify
    harness = _Harness(monkeypatch, tmp_path, [], answers=[json.dumps(F["tails"][0])])
    outcomes = []
    def verify(command, workspace):
        outcome = original(command, workspace)
        outcomes.append(outcome)
        return outcome
    monkeypatch.setattr(agent_run, "run_verify", verify)
    result = agent_run._run_approach("wo-1", F["stream"], F["request"], str(tmp_path), 0, ["synthetic"], False)
    assert result["outcome"] == "stalled"
    assert outcomes == [(1, "{"), (1, "{")]
    assert harness.finished == harness.reviews == []
