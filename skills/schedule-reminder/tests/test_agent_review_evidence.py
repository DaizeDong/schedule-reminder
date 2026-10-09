"""Negative controls for uncertain execution and completion review."""
import json
from types import SimpleNamespace

import pytest

import agent_run
import agent_task
import store
from make_fixtures import llm_call_result


def load_process():
    """Load only installed process code, without importing a provider or replacing its stub."""
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path
    from types import ModuleType
    name = "_schedule_test_process"
    if name + ".process" in sys.modules:
        return sys.modules[name + ".process"]
    installed = importlib.machinery.PathFinder.find_spec("llmcall")
    if installed is None:
        for finder in sys.meta_path:
            installed = finder.find_spec("llmcall", None)
            if installed is not None:
                break
    if installed is None or not installed.submodule_search_locations:
        raise RuntimeError("installed llmcall process dependency is unavailable")
    directory = Path(next(iter(installed.submodule_search_locations)))
    package = ModuleType(name)
    package.__path__ = [str(directory)]
    sys.modules[name] = package
    spec = importlib.util.spec_from_file_location(name + ".process", directory / "process.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_process(monkeypatch):
    import llmcall
    process = load_process()
    monkeypatch.setattr(llmcall, "process", process, raising=False)
    return process


def _result(mode, **changes):
    fields = dict(text=json.dumps({"verify": "synthetic-check", "changed": [],
                                  "summary": "synthetic result"}) if mode == "agent" else "DONE",
                  provider="synthetic-route", error=None, attempts=[],
                  effective_model="synthetic-" + mode, model_family="family-" + mode,
                  execution_started=True, outcome="success", cleanup_confirmed=True)
    fields.update(changes)
    return llm_call_result(**fields)


def _round(monkeypatch, tmp_path, actor, reviewer, configure=None):
    import llmcall
    store.init_db()
    item = store.add_item("Synthetic review task", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued"})
    item_id = item["id"]
    assert agent_task.claim(item_id)
    assert agent_task.begin_spawn(item_id, 1)
    assert agent_task.start_runner(item_id, 1, 123, 456)
    harness = SimpleNamespace(verifies=[], finished=[], item_id=item_id, options=[], prompts=[])
    monkeypatch.setattr(agent_run, "capture_diff", lambda ws: "HEAD unborn\nsynthetic diff")
    monkeypatch.setattr(agent_run, "detect_changes", lambda *args: ([], "git"))
    def verify(*args, **kwargs):
        harness.verifies.append(args)
        return 0, "synthetic check passed"
    monkeypatch.setattr(agent_run, "run_verify", verify)
    finish = agent_task.finish
    def record_finish(item_id, ok, note="", **kwargs):
        harness.finished.append((item_id, ok, note))
        return finish(item_id, ok, note, **kwargs)
    monkeypatch.setattr(agent_task, "finish", record_finish)
    calls = []
    def call(prompt, **kwargs):
        calls.append(kwargs["mode"])
        harness.options.append(kwargs)
        harness.prompts.append(prompt)
        return actor if kwargs["mode"] == "agent" else reviewer
    monkeypatch.setattr(llmcall, "call", call)
    if configure:
        configure(harness)
    result = agent_run._run_approach(item_id, "synthetic", "synthetic request", str(tmp_path),
                                     0, ["inspect"], False, generation=1)
    return result, harness, calls


@pytest.mark.parametrize("text", ["", '{"verify":"synthetic-check","summary":"partial"}'])
def test_uncertain_actor_never_verifies_or_finishes(monkeypatch, tmp_path, text):
    result, harness, calls = _round(monkeypatch, tmp_path,
        _result("agent", text=text, error="lost response", outcome="execution_uncertain",
                cleanup_confirmed=False), _result("judge"))
    assert result["outcome"] == "reconcile"
    assert calls == ["agent"] and harness.verifies == []
    assert not any(ok for _, ok, _ in harness.finished)


@pytest.mark.parametrize("family", [None, "family-agent"])
def test_unknown_or_same_family_cannot_complete(monkeypatch, tmp_path, family):
    result, harness, _ = _round(monkeypatch, tmp_path,
        _result("agent"), _result("judge", model_family=family))
    assert result["outcome"] == "review_unavailable"
    assert not any(ok for _, ok, _ in harness.finished)


def test_near_match_done_token_cannot_complete(monkeypatch, tmp_path):
    result, harness, _ = _round(monkeypatch, tmp_path,
        _result("agent"), _result("judge", text="DONE_NOT_COMPLETE"))
    assert result["outcome"] == "review_unavailable"
    assert not any(ok for _, ok, _ in harness.finished)


# A live reviewer answered the one-word verdict and then explained it; the old check accepted
# only the bare token, so finished work ended review_unavailable.
EXPLAINED_DONE = ["DONE\n\nThe workspace holds the requested file with the expected date.\n\n"
                  "The verify command reads it back and would fail on any other content.",
                  "\n  done.\r\nThe request is satisfied and nothing outside it changed.",
                  "DONE\n- file present\n- check can fail"]
# Two answers, or none: each keeps the draft instead of completing the order.
AMBIGUOUS = ["DONE\n\nCONTINUE: the date is written in the wrong format.",
             "DONE\ncontinue: one more file is missing",
             "DONE, but the verify command checks the wrong file.",
             "DONE: except the second file.",
             "Looks complete.\nDONE",
             "**DONE**\nAll good.",
             "DONE\nDONE",
             "CONTINUE\nthe date is wrong"]


@pytest.mark.parametrize("text", EXPLAINED_DONE)
def test_review_verdict_reads_the_first_line(text):
    assert agent_run.review_verdict(text) == "done"


@pytest.mark.parametrize("text", ["CONTINUE: the date is wrong",
                                  "CONTINUE:\nthe verify command checks the wrong file.\nDONE otherwise"])
def test_review_verdict_continue_keeps_its_reading(text):
    assert agent_run.review_verdict(text) == "continue"


@pytest.mark.parametrize("text", AMBIGUOUS + ["", "DONE_NOT_COMPLETE", "Not done yet."])
def test_review_verdict_refuses_anything_ambiguous(text):
    assert agent_run.review_verdict(text) is None


@pytest.mark.parametrize("text", EXPLAINED_DONE)
def test_explained_done_completes_the_order(monkeypatch, tmp_path, text):
    result, harness, calls = _round(monkeypatch, tmp_path,
        _result("agent"), _result("judge", text=text))
    assert result["outcome"] == "done" and calls == ["agent", "judge"]
    assert agent_task.operation(harness.item_id)["outcome"] == "done"


@pytest.mark.parametrize("text", AMBIGUOUS)
def test_ambiguous_verdict_keeps_the_draft(monkeypatch, tmp_path, text):
    result, harness, _ = _round(monkeypatch, tmp_path,
        _result("agent"), _result("judge", text=text))
    assert result["outcome"] == "review_unavailable", result
    assert not any(ok for _, ok, _ in harness.finished)


def test_explained_continue_never_completes(monkeypatch, tmp_path):
    result, harness, calls = _round(monkeypatch, tmp_path, _result("agent"),
        _result("judge", text="CONTINUE: the check reads the wrong file.\n\nDetails follow."))
    assert result["outcome"] not in ("done", "review_unavailable"), result
    assert calls[:2] == ["agent", "judge"]
    assert not any(ok for _, ok, _ in harness.finished)


def test_untyped_installed_result_is_explicitly_unresolved(monkeypatch, tmp_path):
    actor = llm_call_result(text="partial draft", include_metadata=False)
    result, harness, calls = _round(monkeypatch, tmp_path, actor, _result("judge"))
    assert result["outcome"] == "reconcile"
    assert calls == ["agent"] and not any(ok for _, ok, _ in harness.finished)


def test_independent_review_finishes_only_owned_generation(monkeypatch, tmp_path):
    result, harness, calls = _round(monkeypatch, tmp_path, _result("agent"), _result("judge"))
    assert result["outcome"] == "done" and calls == ["agent", "judge"]
    assert agent_task.get(harness.item_id)["progress"] == 100
    assert agent_task.operation(harness.item_id)["outcome"] == "done"
    assert harness.options[1]["avoid"] == "family-agent"
    assert [options["timeout"] for options in harness.options] == [
        float(agent_run.ACT_TIMEOUT), float(agent_run.REVIEW_TIMEOUT)]
    for options in harness.options:
        assert not {"chain", "model", "effort", "cwd", "cancel", "requirements"}.intersection(options)


def test_unknown_actor_family_never_calls_reviewer(monkeypatch, tmp_path):
    result, harness, calls = _round(monkeypatch, tmp_path,
        _result("agent", model_family=None), _result("judge"))
    assert result["outcome"] == "review_unavailable" and calls == ["agent"]


def test_workspace_change_during_review_preserves_unfinished_draft(monkeypatch, tmp_path):
    def configure(harness):
        captures = iter(["INITIAL_EVIDENCE", "REVIEWED_EVIDENCE", "LATER_EVIDENCE"])
        monkeypatch.setattr(agent_run, "capture_diff", lambda ws: next(captures))
    result, harness, calls = _round(monkeypatch, tmp_path, _result("agent"), _result("judge"), configure)
    assert result["outcome"] == "review_unavailable" and calls == ["agent", "judge"]
    assert "INITIAL_EVIDENCE" in harness.prompts[1] and "REVIEWED_EVIDENCE" in harness.prompts[1]
    assert agent_task.operation(harness.item_id)["outcome"] == "review_unavailable"


@pytest.mark.parametrize("tail", [{"summary": "missing verify"}, {"verify": "", "summary": "empty check"},
                                {"verify": None, "summary": ""}, {"verify": 42, "summary": "bad check"}])
def test_invalid_verification_contract_never_runs_a_command(monkeypatch, tmp_path, tail):
    result, harness, calls = _round(monkeypatch, tmp_path,
        _result("agent", text=json.dumps(tail)), _result("judge"))
    assert result["outcome"] == "review_unavailable" and calls == ["agent"]
    assert harness.verifies == []


def test_contained_command_preserves_stdout_stderr_and_nonzero_exit(tmp_path, real_process):
    import sys
    rc, output = agent_run._contained_command([sys.executable, "-c",
        "import sys; print('SYNTHETIC_STDOUT'); print('SYNTHETIC_STDERR',file=sys.stderr); sys.exit(7)"],
        str(tmp_path), 10)
    assert rc == 7 and "SYNTHETIC_STDOUT" in output and "SYNTHETIC_STDERR" in output
