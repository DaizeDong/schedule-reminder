"""Generated default-path regressions; all storage and observations are synthetic."""
import importlib
import json
import os
from pathlib import Path
import sys

import pytest
import agent_task
import digest
import dispatch
import inbound
import ingest
import ingest_tick
import private_data
import store
from make_fixtures import runtime_defaults_case

KIT = private_data.SOURCE / "guards/tools/datadir.py"


@pytest.fixture
def defaults(tmp_path, monkeypatch):
    case = runtime_defaults_case(tmp_path / "defaults", KIT)
    for name in list(os.environ):
        if name.startswith(("SCHEDULE_", "AGENT_CENTER_", "AGENT_EXEC_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(case["home"]))
    monkeypatch.setenv("USERPROFILE", str(case["home"]))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: case["home"]))
    monkeypatch.setattr(private_data, "SOURCE", case["source"])
    for owner, attribute in ((ingest, "_STATE_DIR"), (dispatch, "_STATE_DIR"), (ingest_tick, "_LOG")):
        monkeypatch.setattr(owner, attribute, None)
    return case


def test_initialized_sibling_precedes_legacy_home_companion(defaults):
    defaults["private_repository"](defaults["home"] / ".schedule-reminder-config")
    assert private_data.config_root() == defaults["companion"]
    assert private_data.data_dir() == defaults["companion"] / "data"
    assert private_data.registry_path() == defaults["companion"] / "registry.json"


def test_legacy_home_companion_is_discovered_without_inferred_creation(defaults):
    (defaults["companion"] / ".companion").unlink()
    legacy = defaults["private_repository"](defaults["home"] / ".schedule-reminder-config")
    assert private_data.config_root() == legacy
    assert not (defaults["companion"] / "data").exists()


def test_unproven_sibling_refuses_when_no_companion_is_initialized(defaults):
    (defaults["companion"] / ".companion").unlink()
    before = set(defaults["companion"].rglob("*"))
    with pytest.raises(ValueError, match="companion|Companion"):
        private_data.config_root()
    assert set(defaults["companion"].rglob("*")) == before


def test_missing_companion_reads_empty_and_writes_fail_before_creation(defaults):
    (defaults["companion"] / ".companion").unlink()
    (defaults["companion"] / ".git/config").unlink()
    (defaults["companion"] / ".git").rmdir()
    defaults["companion"].rmdir()
    missing = defaults["home"] / ".schedule-reminder-config"
    assert private_data.config_root() == missing
    assert store.list_items()["items"] == []
    assert ingest.load_registry() == {}
    with pytest.raises(ValueError):
        private_data.prepare_parent(missing / "state/record.json")
    assert not missing.exists()


@pytest.mark.parametrize("dependency", ("missing", "unsupported"))
def test_missing_guards_fails_default_discovery_explicitly(defaults, dependency):
    path = defaults["source"] / "guards/tools/datadir.py"
    if dependency == "missing":
        path.unlink()
    else:
        path.write_text("# Synthetic unsupported dependency.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Guards.*dependency|dependency.*Guards"):
        private_data.config_root()


@pytest.mark.parametrize("variable", ("SCHEDULE_REMINDER_CONFIG", "AGENT_CENTER_CONFIG"))
@pytest.mark.parametrize("json_file", (False, True))
def test_explicit_config_directory_and_registry_selection(defaults, monkeypatch, variable, json_file):
    selected = defaults["private_repository"](defaults["home"] / "selected")
    value = selected / "selected-registry.json" if json_file else selected
    monkeypatch.setenv(variable, str(value))
    assert private_data.config_root() == selected
    expected = value if variable == "AGENT_CENTER_CONFIG" else selected / "registry.json"
    assert private_data.registry_path() == expected


def test_config_and_registry_pointers_keep_independent_precedence(defaults, monkeypatch):
    selected = defaults["private_repository"](defaults["home"] / "selected-config")
    registry = defaults["home"] / "selected-registry.json"
    monkeypatch.setenv("SCHEDULE_REMINDER_CONFIG", str(selected))
    monkeypatch.setenv("AGENT_CENTER_CONFIG", str(registry))
    assert private_data.config_root() == selected
    assert private_data.registry_path() == registry


def test_discovery_intent_never_replaces_private_write_proof(defaults):
    config = defaults["companion"] / ".git/config"
    config.write_text((defaults["source"] / ".git/config").read_text(encoding="utf-8"), encoding="utf-8")
    assert private_data.config_root() == defaults["companion"]
    target = defaults["companion"] / "state/record.json"
    with pytest.raises(ValueError):
        private_data.prepare_parent(target)
    assert not target.parent.exists()


@pytest.mark.parametrize("override", (False, True))
def test_default_namespaces_and_writers_use_the_selected_roots(defaults, monkeypatch, override):
    root = defaults["companion"]
    data = defaults["private_repository"](defaults["home"] / "selected-data") if override else root / "data"
    if override:
        monkeypatch.setenv("SCHEDULE_REMINDER_DATA_DIR", str(data))
    environment = dict(os.environ)
    assert private_data.config_root() == root
    assert dict(os.environ) == environment
    assert private_data.data_dir() == data
    assert Path(store.default_db_path()) == data / "db.sqlite3"
    assert Path(agent_task.runs_root()) == root / "agent-runs"
    assert Path(digest._path()) == root / "digest.json"
    assert Path(ingest.state_dir()) == root / "state"
    assert inbound.root() == data / "state"
    assert inbound.root(ingest.state_dir()) == root / "state"
    digest._save(defaults["digest"])
    ingest_tick._log(defaults["record"])
    agent_task.append_event(defaults["item"], "synthetic-default")
    with inbound.dispatch_record(defaults["stream"], defaults["message"]) as (record, save):
        save()
    assert json.loads((root / "digest.json").read_text(encoding="utf-8")) == defaults["digest"]
    assert defaults["record"].strip() in (root / "state/ingest_tick.log").read_text(encoding="utf-8")
    assert (root / "agent-runs" / defaults["item"]["id"] / "events.jsonl").is_file()
    assert (data / "state/dispatch" / (inbound.identity(defaults["stream"], defaults["message"]) + ".json")).is_file()
    assert not (data / "digest.json").exists()


def test_cli_reads_the_ingest_inbox_default_without_model_or_send(defaults, monkeypatch):
    inbox = Path(ingest._inbox_file(defaults["stream"]))
    with private_data.open_for_write(inbox, encoding="utf-8") as stream:
        stream.write(defaults["record"])
    calls = []
    monkeypatch.setattr(dispatch, "dispatch", lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    monkeypatch.setattr(sys, "argv", ["dispatch.py", "--stream", defaults["stream"], "--no-post"])
    assert dispatch.main() == 0
    assert calls[0][0] == (defaults["stream"], defaults["record"])
    assert calls[0][1]["post"] is False


def test_reload_of_runtime_modules_is_inert(defaults, monkeypatch):
    effects = []
    def blocked(*args, **kwargs):
        effects.append(True)
        raise AssertionError("runtime import attempted storage or transport")
    with monkeypatch.context() as guard:
        guard.setattr(private_data, "config_root", blocked)
        guard.setattr(private_data, "data_dir", blocked)
        guard.setattr(private_data, "prepare_parent", blocked)
        guard.setattr(private_data, "open_for_write", blocked)
        guard.setattr(private_data, "_query", blocked)
        for module in (agent_task, digest, dispatch, inbound, ingest, ingest_tick):
            importlib.reload(module)
    assert effects == []
