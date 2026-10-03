"""Supported proof outcomes precede Schedule visibility queries; SSH policy belongs to Guards."""
import importlib.util
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import offline_support
import private_data
from make_fixtures import ssh_alias_publication_cases


@pytest.mark.parametrize("case", ssh_alias_publication_cases(), ids=lambda case: case["id"])
@pytest.mark.parametrize("extra_remote", [False, True])
def test_schedule_honors_shared_route_proof_before_visibility(tmp_path, monkeypatch, case, extra_remote):
    calls = []
    boundary = offline_support.synthetic_boundary()
    repositories = case["repositories"] if extra_remote else case["repositories"][:1]
    def prove(directory):
        calls.append(("proof", str(directory)))
        if not case["allowed"] and case["visibility"] == "PRIVATE":
            raise boundary.GitError("synthetic shared transport refusal")
        return SimpleNamespace(root=str(tmp_path), repositories=repositories, signature=case["id"])
    def query(argv):
        assert argv[:3] == ["gh", "repo", "view"]
        assert calls[0][0] == "proof"
        calls.append(("visibility", argv[3]))
        return json.dumps({"nameWithOwner": argv[3], "visibility": case["visibility"]})
    boundary.prove_private_companion = prove
    monkeypatch.setattr(private_data, "_shared_boundary", lambda: boundary)
    monkeypatch.setattr(private_data, "_query", query)
    destination = tmp_path / "pending" / "data.json"
    if case["allowed"]:
        assert private_data.prove_private(destination)["repositories"] == list(repositories)
        assert calls == [("proof", str(tmp_path)), *[("visibility", name) for name in repositories],
                         ("proof", str(tmp_path))]
    else:
        with pytest.raises(ValueError):
            private_data.prepare_parent(destination)
        assert not destination.parent.exists()
    gh_calls = [call for call in calls if call[0] == "visibility"]
    assert bool(gh_calls) is (case["allowed"] or case["visibility"] in {"PUBLIC", "UNKNOWN"})


@pytest.mark.parametrize("missing", [None, "prove_private_companion", "read_private_companion_git"])
def test_schedule_adapter_loads_only_supported_api(monkeypatch, missing):
    boundary = offline_support.synthetic_boundary()
    original = importlib.util.spec_from_file_location
    def execute(module):
        for name in ("prove_private_companion", "read_private_companion_git", "GitError"):
            if name != missing:
                setattr(module, name, getattr(boundary, name))
    loader = SimpleNamespace(create_module=lambda spec: None, exec_module=execute)
    def selected(name, path):
        if Path(path) == private_data.SOURCE / "guards/tools/data_boundary.py":
            return importlib.util.spec_from_loader(name, loader)
        return original(name, path)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", selected)
    if missing:
        with pytest.raises(ValueError, match="supported PRIVATE proof API"):
            offline_support.REAL_BOUNDARY_FACTORY()
    else:
        loaded = offline_support.REAL_BOUNDARY_FACTORY()
        assert loaded.prove_private_companion is boundary.prove_private_companion
        assert loaded.read_private_companion_git is boundary.read_private_companion_git


def test_selected_guard_supports_public_companion_api():
    """Verify signatures on the selected kit without invoking private implementation helpers."""
    module = offline_support.REAL_BOUNDARY_FACTORY()
    inspect.signature(module.prove_private_companion).bind(Path("synthetic-private"))
    inspect.signature(module.read_private_companion_git).bind(object(), "rev-parse", "--verify", "HEAD")
