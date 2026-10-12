"""Generated queue-read regressions; all observations and actions are synthetic."""
from copy import deepcopy

import pytest

import agent_task
import agent_tick
import dispatch
import store
from make_fixtures import schedule11_query_cases

CASE = schedule11_query_cases()


def busy(*_args, **_kwargs):
    raise store.SkillError("ERR_BUSY", "synthetic busy store")


def add(item):
    store.add_item(item["title"], source=agent_task.WORK_SOURCE, state=item["state"],
                   ext=item["ext"], _id=item["id"])


def test_tick_store_failure_stops_before_claim_or_launch(monkeypatch):
    store.init_db()
    add(CASE["found"]["item"])
    monkeypatch.setattr(agent_task.store, "list_items", busy)
    effects = []
    monkeypatch.setattr(agent_task, "claim", lambda *_args, **_kwargs: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args, **_kwargs: effects.append("launch"))
    with pytest.raises(store.SkillError):
        agent_tick.run(post=False)
    assert effects == []


def _dispatch_without_effects(monkeypatch, stream, plan=None):
    effects = []
    monkeypatch.setattr(dispatch, "call_chain", lambda *_args, **_kwargs: effects.append("model"))
    monkeypatch.setattr(dispatch, "execute", lambda *_args, **_kwargs: effects.append("execute"))
    monkeypatch.setattr(dispatch, "_post", lambda *_args, **_kwargs: effects.append("confirm"))
    record = {"plan": deepcopy(plan), "outcomes": {},
              "authorized_ids": [CASE["identity"]], "authorized_work_ids": [CASE["identity"]]}
    if plan is not None:
        record.update(action_identity_version=2, action_keys=[
            dispatch._action_identity(stream, None, index, action)
            for index, action in enumerate(plan["actions"])])
    before = deepcopy(record)
    with pytest.raises((RuntimeError, store.SkillError)):
        dispatch._dispatch(stream, CASE["reply"], None, False, None, None, record,
                           lambda: effects.append("save"))
    assert effects == []
    assert record == before


@pytest.mark.parametrize("stream", ("reminders", CASE["stream"]))
def test_dispatch_census_failure_stops_before_any_planner_or_action(monkeypatch, stream):
    store.init_db()
    monkeypatch.setattr(store, "list_items", busy)
    _dispatch_without_effects(monkeypatch, stream)


@pytest.mark.parametrize("read", sorted(CASE["retry_plans"]))
def test_retry_target_read_failure_stops_before_any_action(monkeypatch, read):
    store.init_db()
    monkeypatch.setattr(store if read == "active_ids" else agent_task, read, busy)
    _dispatch_without_effects(monkeypatch, CASE["stream"], CASE["retry_plans"][read])


def test_confirmed_absence_is_distinct_and_performs_no_mutation():
    store.init_db()
    assert agent_task.get(CASE["identity"]) is None
    assert agent_task.claim(CASE["identity"]) is False
    assert agent_task.finish(CASE["identity"], True, generation=1) is False
    assert store.list_items()["items"] == []


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_empty_and_multipage_reads_remain_available(case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    result = dispatch._active_items()
    assert result == [{"id": item["id"], "title": item["title"]} for item in case["expected"]]


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_work_census_reads_every_page_in_order(case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    result = agent_task.orders()
    assert [(row["id"], row["ext"]["x_agent_exec_state"]) for row in result] == [
        (item["id"], item["ext"]["x_agent_exec_state"]) for item in case["expected"]]


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_census_retains_serial_running_and_stop_guards(monkeypatch, case):
    store.init_db()
    for item in case["expected"]:
        add(item)
    monkeypatch.setattr(agent_tick, "reap", lambda *_args, **_kwargs: [])
    effects = []
    monkeypatch.setattr(agent_task, "claim", lambda *_args, **_kwargs: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args, **_kwargs: effects.append("launch"))
    result = agent_tick.run(post=False)
    assert result["launched"] is None
    assert result["running"] == (0 if case["name"] == "empty" else 1)
    assert effects == []


def test_successful_get_keeps_normal_claim_path():
    store.init_db()
    add(CASE["found"]["item"])
    assert agent_task.get(CASE["identity"])["ext"] == CASE["found"]["item"]["ext"]
    assert agent_task.claim(CASE["identity"]) is True
    assert store.get_item(CASE["identity"])["state"] == "doing"
    assert agent_task.operation(CASE["identity"])["generation"] == 1
