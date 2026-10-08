"""The scripts against the INSTALLED llmcall 0.3.0 contract, with negative controls.

Three things are pinned here. Every llmcall.call site passes only keywords the installed
signature accepts (the older runtime accepted cwd/cancel/requirements and the current one does
not). The runner turns a real 0.3.0 Result into the execution evidence its receipts need, and
leaves anything else unresolved. And work orders get their full phase budgets again.
"""
from dataclasses import dataclass, field
from pathlib import Path
import sys
from typing import List, Optional

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools"))
import llmcall_contract  # noqa: E402

import agent_run  # noqa: E402
import agent_tick  # noqa: E402

SCRIPTS = ROOT / "skills" / "schedule-reminder" / "scripts"


@pytest.fixture(scope="module")
def contract():
    return llmcall_contract.installed_contract()


def test_every_call_site_uses_only_installed_keywords(contract):
    files = sorted(SCRIPTS.glob("*.py"))
    sites = sum(llmcall_contract.count_calls(p.read_text(encoding="utf-8-sig"), str(p)) for p in files)
    assert sites >= 2, "the scanner recognised no llmcall.call site, so a pass would be vacuous"
    assert llmcall_contract.check_files(files, contract["parameters"]) == []


def test_superseded_keywords_are_flagged(contract):
    # Negative control: the call shape the retired runtime used, written synthetically.
    source = (
        "def _llm(prompt, timeout, mode, *, workspace, cancel=None, actor_family=None, requirements=None):\n"
        "    import llmcall\n"
        "    options = {} if requirements is None else {'requirements': requirements}\n"
        "    return llmcall.call(prompt, mode=mode, timeout=float(timeout), cwd=workspace,\n"
        "                        cancel=cancel, avoid=actor_family, log=print, **options)\n")
    found = {message for _, message in llmcall_contract.check_source(source, contract["parameters"])}
    assert found == {"unsupported keyword cwd=", "unsupported keyword cancel=",
                     "unsupported keyword requirements="}


@pytest.mark.parametrize("source", [
    "import llmcall\ndef f(o):\n    return llmcall.call('p', **o)\n",
    "import llmcall\ndef f(x):\n    o = dict(x)\n    return llmcall.call('p', **o)\n",
    "from llmcall import call as ask\ndef f():\n    o = {}\n    o.update(cwd='.')\n    return ask('p', **o)\n",
])
def test_unprovable_keyword_sources_are_reported(source, contract):
    assert llmcall_contract.check_source(source, contract["parameters"])


def test_installed_result_shape_is_what_the_translation_reads(contract):
    assert {"text", "provider", "error", "attempts", "data"} <= set(contract["result_fields"])
    assert {"provider", "ok", "ms", "error", "reason", "group", "supervision"} <= set(contract["attempt_fields"])
    assert contract["reasons"]["REASON_BUDGET"] in agent_run._NOT_LAUNCHED
    assert contract["reasons"]["REASON_GROUP_REFUSED"] in agent_run._NOT_LAUNCHED
    assert contract["reasons"]["REASON_CLEANUP"] == agent_run._CLEANUP_FAILED
    assert contract["rung_group"] is True


# Synthetic stand-ins with the installed 0.3.0 field names (pinned by the test above).
@dataclass
class Attempt:
    provider: str
    ok: bool
    ms: int
    error: Optional[str] = None
    reason: Optional[str] = None
    group: Optional[str] = None
    redo: Optional[str] = None
    supervision: Optional[str] = None


@dataclass
class Result:
    text: str = ""
    provider: Optional[str] = None
    data: object = None
    error: Optional[str] = None
    attempts: List[Attempt] = field(default_factory=list)
    depth: int = 0
    group: Optional[str] = None

    def __bool__(self):
        return self.provider is not None


@pytest.fixture
def installed(monkeypatch):
    import llmcall
    monkeypatch.setattr(llmcall, "Result", Result, raising=False)
    monkeypatch.setattr(llmcall, "rung_group",
                        lambda name: {"codexg": "codex", "codex": "codex", "cc": "claude",
                                      "claude": "claude"}.get(name, name), raising=False)
    return llmcall


def test_answer_after_a_failed_rung_is_success_with_family(installed):
    evidence = agent_run.execution_evidence(Result(
        text="answer", provider="claude",
        attempts=[Attempt("codexg", False, 900, "synthetic failure", "error", "codex"),
                  Attempt("claude", True, 1200, group="claude")]))
    assert (evidence.outcome, evidence.execution_started, evidence.cleanup_confirmed) == ("success", True, True)
    assert (evidence.model_family, evidence.effective_model, evidence.model_source) == ("claude", "claude", "llmcall-rung")
    assert [a.execution_started for a in evidence.attempts] == [None, True]
    assert evidence.error is None


def test_skipped_rungs_never_started(installed):
    evidence = agent_run.execution_evidence(Result(error="synthetic exhausted", attempts=[
        Attempt("codexg", False, 0, "chain budget exhausted", "budget_exhausted", "codex"),
        Attempt("cc", False, 0, "claude group already refused", "group_already_refused", "claude")]))
    assert (evidence.outcome, evidence.execution_started, evidence.cleanup_confirmed) == ("failed", False, True)
    assert evidence.model_family is None and evidence.effective_model is None


def test_cleanup_failure_and_unowned_rung_are_not_confirmed(installed):
    failed = agent_run.execution_evidence(Result(error="x", attempts=[
        Attempt("codex", False, 10, "process cleanup failed", "process_cleanup_failed", "codex")]))
    assert (failed.outcome, failed.cleanup_confirmed) == ("cleanup_failed", False)
    unowned = agent_run.execution_evidence(Result(text="answer", provider="codex", attempts=[
        Attempt("codex", True, 10, group="codex", supervision="synthetic: job unavailable")]))
    assert unowned.cleanup_confirmed is None and unowned.outcome == "success"


def test_untyped_results_are_left_unresolved(installed):
    from types import SimpleNamespace
    untyped = SimpleNamespace(text="partial", provider="synthetic", error=None, attempts=[])
    assert agent_run.execution_evidence(untyped) is untyped


def test_translated_actor_and_reviewer_complete_only_across_families(installed):
    actor = agent_run.execution_evidence(Result(text="{}", provider="codexg",
                                                attempts=[Attempt("codexg", True, 5, group="codex")]))
    other = agent_run.execution_evidence(Result(text="DONE", provider="claude",
                                                attempts=[Attempt("claude", True, 5, group="claude")]))
    same = agent_run.execution_evidence(Result(text="DONE", provider="codex",
                                               attempts=[Attempt("codex", True, 5, group="codex")]))
    assert agent_run.independent_review(actor, other)
    assert not agent_run.independent_review(actor, same)


def test_actor_gets_the_act_budget_and_reviewer_the_review_budget(installed, monkeypatch):
    seen = []
    monkeypatch.setattr(installed, "call", lambda prompt, **kw: seen.append(kw) or Result())
    agent_run._llm("p", None, agent_run.ACT_TIMEOUT, "agent")
    agent_run._llm("p", None, agent_run.REVIEW_TIMEOUT, "judge", actor_family="codex")
    assert [kw["timeout"] for kw in seen] == [1800.0, 420.0]
    assert seen[1]["avoid"] == "codex"
    assert all(not {"cwd", "cancel", "requirements", "chain", "model"} & kw.keys() for kw in seen)


def test_explicit_requirements_are_refused_before_launch(installed, monkeypatch):
    monkeypatch.setattr(installed, "call", lambda *a, **k: pytest.fail("launched despite requirements"))
    result = agent_run._llm("p", None, 10, "agent", requirements={"access": "read_only"})
    assert (result.outcome, result.execution_started, result.cleanup_confirmed) == (
        "capability_unavailable", False, True)
    assert "not expressible" in result.error


def test_revoked_operation_never_reaches_llmcall(installed, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(installed, "call", lambda *a, **k: pytest.fail("called after revocation"))
    with pytest.raises(agent_run.CleanupUncertain):
        agent_run._llm("p", None, 10, "agent", cancel=SimpleNamespace(is_set=lambda: True))


def test_runner_child_gets_a_hidden_console_not_a_detached_one():
    flags = agent_tick.RUNNER_CREATION_FLAGS
    assert flags & 0x08000000, "CREATE_NO_WINDOW missing"
    # DETACHED_PROCESS makes Windows ignore CREATE_NO_WINDOW, and the runner's console children
    # would each open a visible window.
    assert not flags & 0x00000008


def test_launch_hands_the_runner_those_flags(tmp_path, monkeypatch):
    import agent_task
    import store
    store.init_db()
    item = store.add_item("Synthetic launch", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued", agent_task.EXT_WORKSPACE: str(tmp_path)})
    assert agent_task.claim(item["id"])
    seen = {}
    original = agent_tick.subprocess.Popen

    def spawn(argv, *args, **kwargs):
        # The module is shared, so only the runner launch is intercepted.
        if agent_tick.RUNNER not in [str(value) for value in argv]:
            return original(argv, *args, **kwargs)
        seen.update(kwargs, argv=argv)
        raise OSError("synthetic spawn refusal")
    monkeypatch.setattr(agent_tick.subprocess, "Popen", spawn)
    assert agent_tick.launch(agent_task.get(item["id"]), generation=1, post_reports=False) is False
    assert seen["argv"][2] == agent_tick.RUNNER and seen["cwd"] == str(tmp_path)
    assert not Path(seen["argv"][0]).name.lower().startswith("pythonw")
    if sys.platform == "win32":
        assert seen["creationflags"] == agent_tick.RUNNER_CREATION_FLAGS
