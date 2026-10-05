import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / 'skills/schedule-reminder/scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('artifact_fixtures', ROOT / 'tools/make_fixtures.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def module():
    assert (SCRIPTS / 'agent_artifacts.py').is_file(), 'Artifact evidence has not been implemented'
    return __import__('agent_artifacts')


def test_non_code_outputs_have_deterministic_evidence(tmp_path):
    evidence = module()
    workspace = fixtures.artifact_workspace(tmp_path / 'output')
    before = evidence.capture_artifacts(str(workspace))
    assert 'report.txt' in before and before == evidence.capture_artifacts(str(workspace))
    fixtures.artifact_workspace(workspace, size=40)
    assert before != evidence.capture_artifacts(str(workspace))
    (workspace / 'report.txt').unlink()
    assert 'report.txt' not in evidence.capture_artifacts(str(workspace))


def test_artifact_evidence_refuses_oversized_or_missing_inputs(tmp_path):
    evidence = module()
    workspace = fixtures.artifact_workspace(tmp_path / 'output', size=100)
    with pytest.raises(evidence.ArtifactEvidenceUnavailable):
        evidence.capture_artifacts(str(workspace), max_bytes=10)
    with pytest.raises(evidence.ArtifactEvidenceUnavailable):
        evidence.capture_artifacts(str(tmp_path / 'missing'))


def test_runner_uses_artifacts_only_for_explicit_artifact_work(tmp_path, monkeypatch):
    module()
    import agent_run
    workspace = fixtures.artifact_workspace(tmp_path / 'output')
    monkeypatch.setattr(agent_run.agent_task, 'get', lambda _id: {'ext': {'x_agent_exec_evidence': 'artifacts'}})
    monkeypatch.setattr(agent_run, 'capture_diff', lambda _path: pytest.fail('Artifact work must not require Git'))
    token = agent_run._OPERATION.set(('synthetic-work', 1))
    try:
        assert 'report.txt' in agent_run._capture_evidence(str(workspace))
    finally:
        agent_run._OPERATION.reset(token)
