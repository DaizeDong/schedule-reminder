"""Generated queue-read regressions; all observations and actions are synthetic."""
from copy import deepcopy

import pytest

import agent_task
import agent_tick
import dispatch
import store
from make_fixtures import schedule11_query_cases

CASE = schedule11_query_cases()


def pages(monkeypatch, owner, attribute, responses):
    pending = iter(deepcopy(responses))
    calls = []
    def query(*args):
        calls.append(args)
        return next(pending)
    monkeypatch.setattr(owner, attribute, query)
    return calls


def busy(*_args, **_kwargs):
    raise store.SkillError("ERR_BUSY", "synthetic busy store")


def add(item):
    store.add_item(item["title"], source=agent_task.WORK_SOURCE, state=item["state"],
                   ext=item["ext"], _id=item["id"])


@pytest.mark.parametrize("case", CASE["page_failures"], ids=lambda case: case["name"])
def test_failed_page_never_returns_a_partial_census(monkeypatch, case):
    calls = pages(monkeypatch, dispatch, "_rem", case["pages"])
    with pytest.raises(RuntimeError):
        dispatch._active_items()
    assert len(calls) <= len(case["pages"])
    assert all(call[0] == "list" for call in calls)


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


def _dispatch_without_effects(monkeypatch, stream, replay):
    effects = []
    monkeypatch.setattr(dispatch, "call_chain", lambda *_args, **_kwargs: effects.append("model"))
    monkeypatch.setattr(dispatch, "execute", lambda *_args, **_kwargs: effects.append("execute"))
    monkeypatch.setattr(dispatch, "_post", lambda *_args, **_kwargs: effects.append("confirm"))
    record = {"plan": deepcopy(CASE["plan"]) if replay else None, "outcomes": {},
              "authorized_ids": [], "authorized_work_ids": []}
    before = deepcopy(record)
    with pytest.raises((RuntimeError, store.SkillError)):
        dispatch._dispatch(stream, CASE["reply"], None, False, None, None, record,
                           lambda: effects.append("save"))
    assert effects == []
    assert record == before


@pytest.mark.parametrize("case", CASE["dispatch_failures"], ids=lambda case: case["name"])
@pytest.mark.parametrize("replay", (False, True))
def test_dispatch_query_failure_stops_before_any_planner_or_action(monkeypatch, case, replay):
    pages(monkeypatch, dispatch, "_rem", case["pages"])
    _dispatch_without_effects(monkeypatch, "reminders", replay)


@pytest.mark.parametrize("replay", (False, True))
def test_dispatch_work_read_failure_stops_before_any_planner_or_action(monkeypatch, replay):
    store.init_db()
    pages(monkeypatch, dispatch, "_rem", [{"items": [], "next_cursor": None}] * 3)
    monkeypatch.setattr(agent_task.store, "list_items", busy)
    _dispatch_without_effects(monkeypatch, CASE["stream"], replay)


def test_confirmed_absence_is_distinct_and_performs_no_mutation():
    store.init_db()
    assert agent_task.get(CASE["identity"]) is None
    assert agent_task.claim(CASE["identity"]) is False
    assert agent_task.finish(CASE["identity"], True, generation=1) is False
    assert store.list_items()["items"] == []


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_empty_and_multipage_reads_remain_available(monkeypatch, case):
    calls = pages(monkeypatch, dispatch, "_rem", case["pages"])
    result = dispatch._active_items()
    assert result == [{"id": item["id"], "title": item["title"]} for item in case["expected"]]
    assert len(calls) == len(case["pages"])


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
