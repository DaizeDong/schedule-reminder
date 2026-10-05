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


def invoke(role):
    if role == "get":
        return agent_task.get(CASE["identity"])
    if role == "claim":
        return agent_task.claim(CASE["identity"])
    return agent_task.finish(CASE["identity"], True)


@pytest.mark.parametrize("case", CASE["page_failures"], ids=lambda case: case["name"])
@pytest.mark.parametrize("consumer", ("orders", "active_items"))
def test_failed_page_never_returns_a_partial_census(monkeypatch, case, consumer):
    owner = agent_task if consumer == "orders" else dispatch
    calls = pages(monkeypatch, owner, "rem" if consumer == "orders" else "_rem", case["pages"])
    with pytest.raises(RuntimeError):
        (agent_task.orders if consumer == "orders" else dispatch._active_items)()
    assert len(calls) <= len(case["pages"])
    assert all(call[0] == "list" for call in calls)


@pytest.mark.parametrize("case", CASE["boundary_failures"], ids=lambda case: case["name"])
@pytest.mark.parametrize("second_census", (False, True))
def test_tick_query_failure_stops_before_claim_or_launch(monkeypatch, case, second_census):
    responses = deepcopy(case["pages"])
    if second_census:
        responses.insert(0, {"items": [deepcopy(CASE["found"]["item"])], "next_cursor": None})
    pages(monkeypatch, agent_task, "rem", responses)
    effects = []
    reaped = []
    monkeypatch.setattr(agent_tick, "reap", lambda *_args, **_kwargs: reaped.append(True) or [])
    monkeypatch.setattr(agent_task, "claim", lambda *_args: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args: effects.append("launch"))
    with pytest.raises(RuntimeError):
        agent_tick.run(post=False)
    assert effects == []
    assert len(reaped) == int(second_census)


@pytest.mark.parametrize("case", CASE["dispatch_failures"], ids=lambda case: case["name"])
@pytest.mark.parametrize("replay", (False, True))
def test_dispatch_query_failure_stops_before_any_planner_or_action(monkeypatch, case, replay):
    owner = dispatch if case["route"] == "pool" else agent_task
    pages(monkeypatch, owner, "_rem" if case["route"] == "pool" else "rem", case["pages"])
    effects = []
    monkeypatch.setattr(dispatch, "call_chain", lambda *_args, **_kwargs: effects.append("model"))
    monkeypatch.setattr(dispatch, "execute", lambda *_args, **_kwargs: effects.append("execute"))
    monkeypatch.setattr(dispatch, "_post", lambda *_args, **_kwargs: effects.append("confirm"))
    record = {"plan": deepcopy(CASE["plan"]) if replay else None, "outcomes": {},
              "authorized_ids": [], "authorized_work_ids": []}
    before = deepcopy(record)
    with pytest.raises(RuntimeError):
        dispatch._dispatch("reminders" if case["route"] == "pool" else CASE["stream"],
                           CASE["reply"], None, False, None, None, record,
                           lambda: effects.append("save"))
    assert effects == []
    assert record == before


@pytest.mark.parametrize("case", CASE["get_failures"], ids=lambda case: case["name"])
@pytest.mark.parametrize("role", ("get", "claim", "finish"))
def test_failed_get_cannot_transition_patch_or_finalize(monkeypatch, case, role):
    mutations = []
    def query(*args):
        if args[0] == "get":
            return deepcopy(case["response"])
        mutations.append(args[0])
        return deepcopy(CASE["found"])
    monkeypatch.setattr(agent_task, "rem", query)
    monkeypatch.setattr(agent_task, "patch_ext", lambda *_args, **_kwargs: mutations.append("patch") or {})
    with pytest.raises(RuntimeError):
        invoke(role)
    assert mutations == []


@pytest.mark.parametrize("role", ("get", "claim", "finish"))
def test_confirmed_absence_is_distinct_and_performs_no_mutation(monkeypatch, role):
    calls = pages(monkeypatch, agent_task, "rem", [CASE["absent"]])
    monkeypatch.setattr(agent_task, "patch_ext", lambda *_args, **_kwargs: pytest.fail("absent item cannot be patched"))
    result = invoke(role)
    if role == "get":
        assert result is None
    elif role == "claim":
        assert result is False
    else:
        assert result["_err"] == "ERR_NOT_FOUND"
    assert calls == [("get", "--id", CASE["identity"])]


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
@pytest.mark.parametrize("consumer", ("orders", "active_items"))
def test_complete_empty_and_multipage_reads_remain_available(monkeypatch, case, consumer):
    owner = agent_task if consumer == "orders" else dispatch
    calls = pages(monkeypatch, owner, "rem" if consumer == "orders" else "_rem", case["pages"])
    result = (agent_task.orders if consumer == "orders" else dispatch._active_items)()
    expected = case["expected"] if consumer == "orders" else [
        {"id": item["id"], "title": item["title"]} for item in case["expected"]]
    assert result == expected
    assert len(calls) == len(case["pages"])


@pytest.mark.parametrize("case", CASE["positives"], ids=lambda case: case["name"])
def test_complete_census_retains_serial_running_and_stop_guards(monkeypatch, case):
    store.init_db()
    pages(monkeypatch, agent_task, "rem", case["pages"] + case["pages"])
    monkeypatch.setattr(agent_tick, "reap", lambda *_args, **_kwargs: [])
    effects = []
    monkeypatch.setattr(agent_task, "claim", lambda *_args: effects.append("claim"))
    monkeypatch.setattr(agent_tick, "launch", lambda *_args: effects.append("launch"))
    result = agent_tick.run(post=False)
    assert result["launched"] is None
    assert result["running"] == (0 if case["name"] == "empty" else 1)
    assert effects == []


@pytest.mark.parametrize("role", ("get", "claim", "finish"))
def test_successful_get_keeps_normal_claim_and_finish_paths(monkeypatch, role):
    store.init_db()
    item = CASE["found"]["item"]
    store.add_item(item["title"], source=agent_task.WORK_SOURCE, ext=item["ext"], _id=item["id"])
    effects = []
    def query(*args):
        if args[0] != "get":
            effects.append(args[0])
        return deepcopy(CASE["found"])
    monkeypatch.setattr(agent_task, "rem", query)
    monkeypatch.setattr(agent_task, "patch_ext", lambda *_args, **_kwargs: effects.append("patch") or {})
    result = invoke(role)
    if role == "get":
        assert result == CASE["found"]["item"]
        assert effects == []
    elif role == "claim":
        assert result is True
        assert effects == []
        assert store.get_item(CASE["identity"])["state"] == "doing"
        assert agent_task.operation(CASE["identity"])["generation"] == 1
    else:
        assert result == CASE["found"]
        assert effects == ["done"]
