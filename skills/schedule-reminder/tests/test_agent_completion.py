"""How a work order completes: the executor's own check decides, and no second model is called.

Every model call is a stub of `llmcall.call`; nothing here reaches a real provider.
"""
import json
import os
from pathlib import Path
import sqlite3
import sys
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


CONTRACT = json.dumps({"verify": "synthetic-check", "changed": [], "summary": "synthetic result"})


def _result(**changes):
    fields = dict(text=CONTRACT, provider="synthetic-route", error=None, attempts=[],
                  effective_model="synthetic-agent", model_family="family-agent",
                  execution_started=True, outcome="success", cleanup_confirmed=True)
    fields.update(changes)
    return llm_call_result(**fields)


def _round(monkeypatch, tmp_path, actors, verify_results=((0, "synthetic check passed"),),
           post_reports=False):
    """One approach of a started order. `actors` are the executor's answers in call order (the
    last one repeats); `verify_results` are the system-run check's results in order."""
    import llmcall
    store.init_db()
    item = store.add_item("Synthetic work order", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued"})
    item_id = item["id"]
    assert agent_task.claim(item_id)
    assert agent_task.begin_spawn(item_id, 1)
    assert agent_task.start_runner(item_id, 1, 123, 456)
    h = SimpleNamespace(item_id=item_id, calls=[], options=[], prompts=[], verifies=[],
                        finished=[], posts=[])

    def verify(cmd, workspace, **kwargs):
        h.verifies.append(cmd)
        return verify_results[min(len(h.verifies), len(verify_results)) - 1]
    monkeypatch.setattr(agent_run, "run_verify", verify)
    finish = agent_task.finish

    def record_finish(item_id, ok, note="", **kwargs):
        h.finished.append((item_id, ok, note))
        return finish(item_id, ok, note, **kwargs)
    monkeypatch.setattr(agent_task, "finish", record_finish)
    monkeypatch.setattr(agent_run, "post", lambda stream, text, **kwargs: h.posts.append((stream, text)))

    def call(prompt, **kwargs):
        h.calls.append(kwargs["mode"])
        h.options.append(kwargs)
        h.prompts.append(prompt)
        return actors[min(len(h.calls), len(actors)) - 1]
    monkeypatch.setattr(llmcall, "call", call)
    h.item = store.get_item(item_id)
    result = agent_run._run_approach(h.item, "synthetic", "synthetic request", str(tmp_path), 0,
                                     post_reports, generation=1)
    return result, h


def _records(h):
    return {p.relative_to(agent_task.run_dir(h.item)).as_posix()
            for p in Path(agent_task.run_dir(h.item)).rglob("*") if p.is_file()}


@pytest.mark.parametrize("family", ["family-agent", None])
def test_passing_check_completes_with_exactly_one_model_call(monkeypatch, tmp_path, family):
    result, h = _round(monkeypatch, tmp_path, [_result(model_family=family)])
    assert result["outcome"] == "done"
    assert h.calls == ["agent"], "a passing check completes the order without a second model"
    assert h.verifies == ["synthetic-check"]
    assert h.finished == [(h.item_id, True, "synthetic result")]
    operation = agent_task.operation(h.item_id)
    assert operation["outcome"] == "done" and operation["checkpoint"] == "done"
    assert agent_task.get(h.item_id)["progress"] == 100
    assert [options["timeout"] for options in h.options] == [float(agent_run.ACT_TIMEOUT)]
    for options in h.options:
        assert not {"avoid", "chain", "model", "effort", "cwd", "cancel", "requirements"} & options.keys()
    names = {Path(name).name for name in _records(h)}
    assert names == {"prompt.txt", "actor.json", "answer.txt", "verify.txt"}


@pytest.mark.parametrize("answer,summary", [
    ("Synthetic report written to report.txt for AcmeCorp.",
     "Synthetic report written to report.txt for AcmeCorp."),
    ('Synthetic report written.\n{"verify": 42, "summary": "bad check"}', "Synthetic report written."),
    ('Synthetic report written.\n```json\n{"summary": "no verify key"}\n```', "Synthetic report written."),
    ('Synthetic report written.\n{"verify": "synthetic-check"', "Synthetic report written."),
    ('{"verify": "", "summary": "Synthetic empty check."}', "Synthetic empty check."),
    ('{"verify": null, "summary": ""}', "(执行者没有给出说明)"),
])
def test_missing_or_invalid_contract_completes_on_the_executor_answer(monkeypatch, tmp_path, answer, summary):
    result, h = _round(monkeypatch, tmp_path, [_result(text=answer)], post_reports=True)
    assert result["outcome"] == "done"
    assert h.calls == ["agent"] and h.verifies == [], "without a contract nothing is run"
    assert h.finished == [(h.item_id, True, summary)]
    assert agent_task.get(h.item_id)["ext"][agent_task.EXT_NOTE] == summary
    [(_stream, report)] = h.posts
    assert summary in report and "没有给出有效的验证契约" in report
    assert "verify.txt" not in {Path(name).name for name in _records(h)}


@pytest.mark.parametrize("answer", [
    '{"verify": 42, "changed": ["report.txt", " ", "notes.txt"], "summary": "Synthetic bad check."}',
    'Synthetic report written.\n{"changed": ["report.txt", "notes.txt"], "summary": "no verify key"}',
])
def test_invalid_contract_keeps_the_self_reported_changed_files(monkeypatch, tmp_path, answer):
    result, h = _round(monkeypatch, tmp_path, [_result(text=answer)], post_reports=True)
    assert result["outcome"] == "done" and h.verifies == []
    [(_stream, report)] = h.posts
    assert "改动(self-reported):notes.txt, report.txt" in report


def test_a_long_answer_is_bounded_as_its_summary(monkeypatch, tmp_path):
    answer = "Synthetic finding. " * 400
    result, h = _round(monkeypatch, tmp_path, [_result(text=answer)], post_reports=True)
    assert result["outcome"] == "done"
    summary = agent_run.answer_summary(answer)
    assert answer.startswith(summary[:-3]) and len(summary) <= agent_run.SUMMARY_LIMIT + 3
    assert h.finished == [(h.item_id, True, summary[:200])]
    assert len(h.posts[0][1]) < len(answer)


def test_failing_then_passing_check_takes_two_executor_calls(monkeypatch, tmp_path):
    result, h = _round(monkeypatch, tmp_path, [_result()],
                       verify_results=[(1, "SYNTHETIC_FIRST_FAILURE"), (0, "synthetic check passed")])
    assert result["outcome"] == "done"
    assert h.calls == ["agent", "agent"]
    assert h.verifies == ["synthetic-check", "synthetic-check"]
    assert "SYNTHETIC_FIRST_FAILURE" not in h.prompts[0]
    assert "SYNTHETIC_FIRST_FAILURE" in h.prompts[1], "the next round sees the real failure"
    assert h.finished == [(h.item_id, True, "synthetic result")]
    assert {name.split("/")[-2] for name in _records(h)} == {"r1", "r2"}


def test_null_verify_completes_without_running_a_check(monkeypatch, tmp_path):
    text = json.dumps({"verify": None, "changed": ["report.txt"],
                       "summary": "Synthetic review by a person is needed; no command can check it."})
    result, h = _round(monkeypatch, tmp_path, [_result(text=text)], post_reports=True)
    assert result["outcome"] == "done"
    assert h.calls == ["agent"] and h.verifies == []
    [(_stream, report)] = h.posts
    assert "无法用一条命令验证" in report
    assert "改动(self-reported):report.txt" in report


@pytest.mark.parametrize("rc", [124, 127, 130])
def test_interrupted_check_reconciles_without_another_call(monkeypatch, tmp_path, rc):
    result, h = _round(monkeypatch, tmp_path, [_result()], verify_results=[(rc, "synthetic interruption")])
    assert result["outcome"] == "reconcile"
    assert h.calls == ["agent"]
    assert not any(ok for _, ok, _ in h.finished)


@pytest.mark.parametrize("text", ["", '{"verify":"synthetic-check","summary":"partial"}'])
def test_uncertain_actor_never_verifies_or_finishes(monkeypatch, tmp_path, text):
    result, h = _round(monkeypatch, tmp_path, [
        _result(text=text, error="lost response", outcome="execution_uncertain", cleanup_confirmed=False)])
    assert result["outcome"] == "reconcile"
    assert h.calls == ["agent"] and h.verifies == []
    assert not any(ok for _, ok, _ in h.finished)


def test_untyped_installed_result_is_explicitly_unresolved(monkeypatch, tmp_path):
    actor = llm_call_result(text="partial draft", include_metadata=False)
    result, h = _round(monkeypatch, tmp_path, [actor])
    assert result["outcome"] == "reconcile"
    assert h.calls == ["agent"] and not any(ok for _, ok, _ in h.finished)


def test_historical_review_rows_stay_readable(monkeypatch, capsys, tmp_path):
    """Orders written while the retired review existed keep listing, reporting and feeding."""
    from reminder_work_feed import read_work_feed
    store.init_db()
    item = store.add_item("Synthetic historical order", source=agent_task.WORK_SOURCE,
                          ext={agent_task.EXT_STATE: "queued", "x_agent_exec_evidence": "artifacts"})
    item_id = item["id"]
    assert agent_task.claim(item_id) and agent_task.begin_spawn(item_id, 1)
    assert agent_task.start_runner(item_id, 1, 123, 456)
    with sqlite3.connect(os.environ["SCHEDULE_DB_PATH"]) as conn:
        conn.execute("UPDATE agent_operations SET baseline=?, baseline_sha256=?, baseline_workspace=? "
                     "WHERE item_id=?", ("HEAD unborn\nsynthetic", "a" * 64, str(tmp_path), item_id))
    assert agent_task.finish(item_id, False, "synthetic historical review outcome",
                             exec_state_value="review_unavailable", generation=1)
    assert agent_task.release(item_id, 1)
    old = agent_task.get(item_id)
    assert agent_task.exec_state(old) == "review_unavailable"
    assert old["ext"]["x_agent_exec_evidence"] == "artifacts"
    assert agent_task.operation(item_id)["baseline_sha256"] == "a" * 64
    assert [order["id"] for order in agent_task.orders(active_only=False)] == [item_id]
    monkeypatch.setattr(sys, "argv", ["agent_task.py", "list", "--all"])
    assert agent_task.main() == 0
    assert json.loads(capsys.readouterr().out)[0]["exec"] == "review_unavailable"
    monkeypatch.setattr(sys, "argv", ["agent_task.py", "status"])
    assert agent_task.main() == 0
    assert json.loads(capsys.readouterr().out)["reservations"] == []
    feed = read_work_feed(db_path=os.environ["SCHEDULE_DB_PATH"])
    rows = {row["id"]: row for row in feed["items"]}
    assert rows[item_id]["execution"]["state"] == "review_unavailable"


def test_new_orders_reuse_a_live_order_enqueued_before_evidence_was_removed(tmp_path):
    """The request identity keeps its old four-slot shape, so a live order from before the change
    (enqueued without an evidence mode) is still found instead of duplicated."""
    import hashlib
    from creation_guard import _scope
    store.init_db()
    item = agent_task.enqueue("synthetic", "Synthetic request", workspace=str(tmp_path))
    legacy = ["Synthetic request", os.path.normcase(os.path.abspath(item["ext"][agent_task.EXT_WORKSPACE])), None, None]
    expected = hashlib.sha256(json.dumps(legacy, ensure_ascii=False).encode("utf-8")).hexdigest()
    assert item["ext"]["x_agent_exec_request_identity"] == expected
    assert _scope(item) == {"work_request": expected}


def test_contained_command_preserves_stdout_stderr_and_nonzero_exit(tmp_path, real_process):
    rc, output = agent_run._contained_command([sys.executable, "-c",
        "import sys; print('SYNTHETIC_STDOUT'); print('SYNTHETIC_STDERR',file=sys.stderr); sys.exit(7)"],
        str(tmp_path), 10)
    assert rc == 7 and "SYNTHETIC_STDOUT" in output and "SYNTHETIC_STDERR" in output


def test_dispatch_gives_an_order_without_a_usable_folder_its_own_workspace(tmp_path_factory):
    import dispatch
    workspace = dispatch._order_workspace(None, "dispatch:synthetic-key")
    assert Path(workspace).is_dir() and Path(workspace) != Path.home()
    assert Path(workspace).resolve().is_relative_to(Path(agent_task.runs_root()).resolve())
    assert dispatch._order_workspace(None, "dispatch:synthetic-key") == workspace
    assert dispatch._order_workspace(str(Path.home()), "dispatch:synthetic-key") == workspace
    missing = tmp_path_factory.getbasetemp() / "synthetic-missing-folder"
    assert dispatch._order_workspace(str(missing), "dispatch:synthetic-key") == workspace
    plain = tmp_path_factory.mktemp("plain-request")
    assert dispatch._order_workspace(str(plain), "k") == str(plain)
