"""Review acceptance uses real evidence and synthetic llmcall identities."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agent_run
from llmcall import Result


def test_agent_call_inherits_policy_and_keeps_hard_requirements(monkeypatch, tmp_path):
    import llmcall
    seen = {}
    def call(prompt, **kwargs):
        seen.update(kwargs)
        return Result(error="unsupported", outcome="capability_unavailable")
    monkeypatch.setattr(llmcall, "call", call)
    requirements = llmcall.ExecutionRequirements(workspace=str(tmp_path), access="workspace_write", replay="never_after_start")
    result = agent_run._llm("synthetic", timeout=3, mode="agent", workspace=str(tmp_path), requirements=requirements)
    assert result.outcome == "capability_unavailable"
    assert not {"chain", "model", "effort", "env"}.intersection(seen)
    assert seen["requirements"].workspace == str(tmp_path)
    assert seen["requirements"].access == "workspace_write"
    assert seen["requirements"].replay == "never_after_start"
    assert seen["requirements"] is requirements


def test_review_prompt_contains_diff_output_and_actual_identity():
    identity = {"effective_model": "synthetic-model-v1", "model_family": "family-a"}
    prompt = agent_run.review_prompt("request", "draft", ["a.py"], "git", "assert-check", 0,
                                     "ACTUAL_OUTPUT", diff="-old\n+new", actor_identity=identity)
    assert "-old\n+new" in prompt and "ACTUAL_OUTPUT" in prompt
    assert "synthetic-model-v1" in prompt and "family-a" in prompt


def test_different_route_is_not_independent_model_family():
    actor = Result(provider="route-a", effective_model="model-a", model_family="family-a")
    same = Result(provider="route-b", effective_model="model-a", model_family="family-a")
    unknown = Result(provider="route-b")
    independent = Result(provider="route-a", effective_model="model-b", model_family="family-b")
    assert not agent_run.independent_review(actor, same)
    assert not agent_run.independent_review(actor, unknown)
    assert agent_run.independent_review(actor, independent)


import pytest
import agent_task
import store
from test_agent_exec import _Harness


@pytest.fixture

def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("SCHEDULE_DB_PATH", str(tmp_path / "synthetic.sqlite3"))
    monkeypatch.setenv("AGENT_CENTER_RUNS", str(tmp_path / "runs"))
    store.init_db()
    return _Harness(monkeypatch, tmp_path, verify_results=[(0, "ACTUAL_VERIFY_OUTPUT")])


def run_round(tmp_path):
    return agent_run._run_approach("wo-1", "synthetic", "synthetic request", str(tmp_path),
                                    0, False, generation=1)


@pytest.mark.parametrize("review", [
    Result(error="unavailable", outcome="capability_unavailable", execution_started=False),
    Result(text="DONE", provider="other-route", effective_model="model-a", model_family="family-a", outcome="success"),
    Result(text="DONE", provider="other-route", outcome="success"),
    Result(text="maybe", provider="other-route", effective_model="model-b", model_family="family-b", outcome="success"),
])
def test_unavailable_unknown_or_same_family_review_preserves_draft(harness, monkeypatch, tmp_path, review):
    calls = []
    def llm(prompt, timeout, mode, **kwargs):
        calls.append(mode)
        return review if mode == "judge" else harness._llm(prompt, timeout, mode, **kwargs)
    monkeypatch.setattr(agent_run, "_llm", llm)
    (tmp_path / "draft.txt").write_text("SYNTHETIC_DRAFT", encoding="utf-8")
    result = run_round(tmp_path)
    assert result["outcome"] == "review_unavailable"
    assert calls == ["agent", "judge"]
    assert agent_task.get("wo-1")["state"] == "blocked"
    assert (tmp_path / "draft.txt").read_text() == "SYNTHETIC_DRAFT"
    assert not any(ok for _, ok, _ in harness.finished)


def test_unknown_actor_identity_never_requests_a_reviewer(harness, monkeypatch, tmp_path):
    def llm(prompt, timeout, mode, **kwargs):
        assert mode == "agent"
        result = harness._llm(prompt, timeout, mode, **kwargs)
        result.effective_model = result.model_family = None
        return result
    monkeypatch.setattr(agent_run, "_llm", llm)
    assert run_round(tmp_path)["outcome"] == "review_unavailable"


def test_execution_uncertain_is_never_replayed(harness, monkeypatch, tmp_path):
    calls = []
    def llm(*args, **kwargs):
        calls.append(1)
        return Result(text="draft", error="lost response", execution_started=True,
                      effects="possible", outcome="execution_uncertain")
    monkeypatch.setattr(agent_run, "_llm", llm)
    assert run_round(tmp_path)["outcome"] == "reconcile"
    assert calls == [1] and harness.verifies == []


def test_cancel_during_verification_does_not_review_or_finish(harness, monkeypatch, tmp_path):
    def verify(cmd, workspace, *, cancel):
        assert not cancel.is_set()
        agent_task.cancel("wo-1", "synthetic cancel")
        assert cancel.is_set()
        return 0, "late successful check"
    monkeypatch.setattr(agent_run, "run_verify", verify)
    assert run_round(tmp_path)["outcome"] == "cancelled"
    assert harness.reviews == [] and harness.finished == []
    assert agent_task.get("wo-1")["state"] == "cancelled"


def test_cancel_after_review_before_finish_wins(harness, monkeypatch, tmp_path):
    original = agent_task.finish
    def finish(iid, ok, *args, **kwargs):
        if ok:
            agent_task.cancel(iid, "cancel at commit boundary")
        return original(iid, ok, *args, **kwargs)
    monkeypatch.setattr(agent_task, "finish", finish)
    assert run_round(tmp_path)["outcome"] == "cancelled"
    assert agent_task.get("wo-1")["state"] == "cancelled"
    assert not any(ok for _, ok, _ in harness.finished)


def test_review_receives_bound_diff_verify_and_identity(harness, monkeypatch, tmp_path):
    diffs = iter(["BEFORE_DIFF", "AFTER_DIFF", "AFTER_DIFF"])
    monkeypatch.setattr(agent_run, "capture_diff", lambda ws: next(diffs))
    assert run_round(tmp_path)["outcome"] == "done"
    prompt = harness.reviews[0]
    for marker in ("BEFORE_DIFF", "AFTER_DIFF", "check.cmd", "ACTUAL_VERIFY_OUTPUT", "model-a", "family-a"):
        assert marker in prompt
    evidence = list(tmp_path.rglob("evidence.json"))
    assert len(evidence) == 1
    import json
    payload = json.loads(evidence[0].read_text(encoding="utf-8"))
    assert payload["work_item_id"] == "wo-1" and payload["run_id"] and payload["attempt_id"]
    assert len(payload["diff_sha256"]) == 64


def test_workspace_changes_during_review_prevent_completion(harness, monkeypatch, tmp_path):
    diffs = iter(["BEFORE", "REVIEWED", "CHANGED_AFTER_REVIEW"])
    monkeypatch.setattr(agent_run, "capture_diff", lambda ws: next(diffs))
    assert run_round(tmp_path)["outcome"] == "review_unavailable"
    assert agent_task.get("wo-1")["state"] == "blocked"


def test_contained_command_preserves_failed_stdout_stderr_and_exit(tmp_path):
    rc, output = agent_run._contained_command([sys.executable, "-c",
        "import sys; print('ACTUAL_STDOUT'); print('ACTUAL_STDERR',file=sys.stderr); sys.exit(7)"],
        str(tmp_path), 10)
    assert rc == 7 and "ACTUAL_STDOUT" in output and "ACTUAL_STDERR" in output


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process identity")
@pytest.mark.parametrize("cancelled", [False, True])
def test_verify_common_process_cleans_descendants_on_timeout_or_cancel(tmp_path, monkeypatch, cancelled):
    import json
    import time
    script = tmp_path / "synthetic_check.py"
    marker = tmp_path / "child.json"
    script.write_text("import subprocess,sys,time,json\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)'])\n"
        "open('child.json','w').write(json.dumps({'pid':p.pid}))\n"
        "time.sleep(20)\n", encoding="utf-8")
    class MarkerCancel:
        def is_set(self):
            return cancelled and marker.exists() and marker.stat().st_size > 0
    monkeypatch.setattr(agent_run, "VERIFY_TIMEOUT", 3)
    command = "& '" + sys.executable.replace("'", "''") + "' '" + str(script).replace("'", "''") + "'"
    started = time.monotonic()
    rc, output = agent_run.run_verify(command, str(tmp_path), cancel=MarkerCancel())
    assert rc == (130 if cancelled else 124), output
    assert time.monotonic() - started < 6
    pid = json.loads(marker.read_text())["pid"]
    assert agent_task.proc_identity(pid)[0] is False


def test_real_git_diff_includes_staged_and_untracked_synthetic_files(tmp_path):
    # No commit or identity is needed for this empty synthetic repository.
    rc, output = agent_run._contained_command(["git", "init", "--quiet"], str(tmp_path), 10)
    assert rc == 0, output
    (tmp_path / "tracked.txt").write_text("BEFORE\n", encoding="utf-8")
    assert agent_run._contained_command(["git", "add", "tracked.txt"], str(tmp_path), 10)[0] == 0
    (tmp_path / "tracked.txt").write_text("AFTER\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("SYNTHETIC_NEW_FILE", encoding="utf-8")
    diff = agent_run.capture_diff(str(tmp_path))
    assert "+BEFORE" in diff and "+AFTER" in diff and "SYNTHETIC_NEW_FILE" in diff


def test_new_commits_cannot_hide_changes_from_working_tree_review(harness, monkeypatch, tmp_path):
    diffs = iter(["HEAD synthetic-before\nSTAGED\n", "HEAD synthetic-after\nSTAGED\n"])
    monkeypatch.setattr(agent_run, "capture_diff", lambda ws: next(diffs))
    assert run_round(tmp_path)["outcome"] == "review_unavailable"
    assert harness.reviews == []
