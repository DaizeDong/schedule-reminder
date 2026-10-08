"""Local lifecycle checks on generated Git storage, without scheduled workers."""
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills/schedule-reminder/scripts"))
import config_lifecycle as lifecycle
import private_data


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


CASES = load("schedule_config_cases", ROOT / "tools/make_fixtures.py").configuration_registry_cases()

@pytest.fixture
def private(tmp_path, monkeypatch):
    generator = load("schedule_guard_fixtures", ROOT / "guards/tools/make_fixtures.py")
    fixture = generator.make_storage_contract_fixture(tmp_path, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    for key in list(os.environ):
        if key.startswith(("GIT_", "SCHEDULE_", "AGENT_CENTER_")):
            monkeypatch.delenv(key)
    for key, value in fixture["companion"].env.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
    api = load("schedule_storage_test", ROOT / "guards/tools/storage_contract.py")
    monkeypatch.setattr(lifecycle, "authorize", lambda path: api.authorize_artifact_write(
        ROOT, Path(path).parent, Path(path).name, artifact_id="registry", visibility_map=fixture["receipt"]))
    monkeypatch.setenv("SCHEDULE_REMINDER_CONFIG", str(fixture["companion"].root))
    return fixture


def test_blank_initializer_is_idempotent_not_ready_and_creates_no_database(private):
    root = private["companion"].root
    path, created = lifecycle.initialize(root)
    assert created and not lifecycle.doctor()["ready"]
    before = path.read_bytes()
    assert lifecycle.initialize(root) == (path, False)
    assert path.read_bytes() == before and not (root / "data").exists()


def test_existing_unknown_fields_survive_initialization(private):
    path = private["companion"].root / "registry.json"
    content = json.dumps(CASES["unknown_fields"]).encode()
    path.write_bytes(content)
    lifecycle.initialize(path.parent)
    assert path.read_bytes() == content


def test_schema_rejects_empty_destinations_and_requires_ingest_identity():
    assert lifecycle.validate({})
    assert lifecycle.validate({"streams": {"example": {}}})
    config = json.loads(json.dumps(CASES["bot_destination"]))
    assert not lifecycle.validate(config)
    assert lifecycle.validate(config, "ingest")
    config["guild_id"] = CASES["guild_id"]
    assert lifecycle.validate(config, "ingest")
    config["big_brother"] = {"user_id": CASES["owner_id"]}
    assert not lifecycle.validate(config, "ingest")


@pytest.mark.parametrize("capability", ["ingest", "work"])
def test_inbound_readiness_requires_the_runtime_owner(capability):
    import ingest
    config = json.loads(json.dumps(CASES["bot_destination"]))
    config["guild_id"] = CASES["guild_id"]
    assert ingest.owner_id(config) is None
    assert any("big_brother.user_id" in error for error in lifecycle.validate(config, capability))
    config["big_brother"] = {"user_id": CASES["owner_id"]}
    assert ingest.owner_id(config) == CASES["owner_id"]
    assert not lifecycle.validate(config, capability)


def test_bom_registry_is_rejected_like_the_runtime_readers(private):
    import ingest
    import relay
    path = private["companion"].root / "registry.json"
    content = json.dumps(CASES["bot_destination"]).encode("utf-8-sig")
    path.write_bytes(content)
    assert not lifecycle.doctor()["ready"]
    assert relay.load_registry() == ingest.load_registry() == {}
    assert path.read_bytes() == content


def test_registry_and_data_overrides_remain_independent(private, monkeypatch, tmp_path):
    root = private["companion"].root
    other = tmp_path / "another-companion"
    monkeypatch.setenv("AGENT_CENTER_CONFIG", str(other / "registry.json"))
    monkeypatch.setenv("SCHEDULE_REMINDER_DATA_DIR", str(tmp_path / "other-data"))
    assert private_data.config_root() == root
    assert private_data.registry_path() == other / "registry.json"
    assert private_data.data_dir() == tmp_path / "other-data"
    assert not lifecycle.doctor()["ready"]


@pytest.mark.parametrize("failure", ["public-push", "ignored"])
def test_initialized_private_root_cannot_hide_wrong_publication_or_ignored_registry(private, failure):
    repo = private["companion"]
    if failure == "public-push":
        repo.git("config", "remote.origin.pushurl", "https://github.com/example-owner/synthetic-public.git")
    else:
        (repo.root / ".gitignore").write_text("registry.json\n")
    with pytest.raises((ValueError, RuntimeError)):
        lifecycle.initialize(repo.root)
    assert not (repo.root / "registry.json").exists()


def test_missing_pinned_schema_dependency_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(private_data, "SOURCE", tmp_path)
    with pytest.raises(ValueError, match="pinned Guards"):
        lifecycle.authorize(tmp_path / "registry.json")


def test_ready_registry_doctor_is_configuration_only_and_does_not_write(private):
    path = private["companion"].root / "registry.json"
    path.write_text(json.dumps(CASES["bot_destination"]), encoding="utf-8")
    before = path.read_bytes()
    result = lifecycle.doctor()
    assert result["ready"] and result["scope"] == "configuration-only"
    assert result["runtime_ready"] is None and not result["external_delivery_verified"]
    assert path.read_bytes() == before and not (path.parent / "data").exists()
