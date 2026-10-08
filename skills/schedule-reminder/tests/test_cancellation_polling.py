# -*- coding: utf-8 -*-
"""Pollers see a throttled, latching view of work-order ownership.

llmcall (a running model call) and llmcall.process (a verification command) ask their token every
fraction of a second. Answering from the store costs a PRIVATE proof per ask, so the runner hands
them a PolledCancellation: the exact answer is reused for an interval and latched once set. The
runner's own decision points keep the exact OperationCancellation.
"""
import os
import sys

import pytest

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import agent_run  # noqa: E402


class Exact:
    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    def is_set(self):
        self.asked += 1
        answer = self.answers.pop(0) if self.answers else False
        if isinstance(answer, Exception):
            raise answer
        return answer


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_the_exact_answer_is_asked_once_per_interval():
    exact, clock = Exact([False, False, True]), Clock()
    token = agent_run.PolledCancellation(exact, interval=30, clock=clock)
    assert token.is_set() is False and exact.asked == 1
    for _ in range(50):                      # a poller asking every fraction of a second
        clock.now += 0.5
        if clock.now - 1000.0 < 30:
            assert token.is_set() is False
    assert exact.asked == 1
    clock.now = 1030.0
    assert token.is_set() is False and exact.asked == 2
    clock.now = 1060.0
    assert token.is_set() is True and exact.asked == 3


def test_a_set_answer_is_latched():
    exact, clock = Exact([True, False, False]), Clock()
    token = agent_run.PolledCancellation(exact, interval=30, clock=clock)
    assert token.is_set() is True
    clock.now += 3600
    assert token.is_set() is True and exact.asked == 1


def test_an_unanswerable_check_is_a_revocation():
    token = agent_run.PolledCancellation(Exact([RuntimeError("database unavailable")]), interval=30,
                                         clock=Clock())
    assert token.is_set() is True


def test_control_an_interval_of_zero_asks_every_time():
    # The control for the first test: the throttle, not the fake, is what saved the asks.
    exact, clock = Exact([False] * 5), Clock()
    token = agent_run.PolledCancellation(exact, interval=0, clock=clock)
    for _ in range(5):
        token.is_set()
    assert exact.asked == 5


def test_the_default_interval_follows_the_environment(monkeypatch):
    assert agent_run.PolledCancellation(Exact([])).interval == agent_run.CANCEL_POLL_SECONDS


def test_commands_get_the_polled_token_and_it_is_not_wrapped_twice(monkeypatch):
    seen = []

    class FakeProcess:
        @staticmethod
        def resolve_context(cwd=None, env=None):
            return {"cwd": cwd}

        @staticmethod
        def run(argv, prompt, timeout, *, context, cancel=None):
            seen.append(cancel)
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0, stdout="ok", stderr="", outcome="success",
                                   error=None, cleanup_confirmed=True, execution_started=True)

    import llmcall
    monkeypatch.setattr(llmcall, "process", FakeProcess, raising=False)
    monkeypatch.setitem(sys.modules, "llmcall.process", FakeProcess)
    exact = agent_run.OperationCancellation("item", 1)
    agent_run._contained_command(["cmd"], os.getcwd(), 5, cancel=exact)
    polled = agent_run.PolledCancellation(exact)
    agent_run._contained_command(["cmd"], os.getcwd(), 5, cancel=polled)
    agent_run._contained_command(["cmd"], os.getcwd(), 5, cancel=None)
    assert isinstance(seen[0], agent_run.PolledCancellation) and seen[0].exact is exact
    assert seen[1] is polled
    assert seen[2] is None


def test_run_order_gives_its_scope_the_polled_token(monkeypatch):
    scopes = []

    class FakeProcess:
        @staticmethod
        def execution_scope(cancel=None, timeout=None):
            scopes.append(cancel)
            import contextlib
            return contextlib.nullcontext()

    import llmcall
    monkeypatch.setattr(llmcall, "process", FakeProcess, raising=False)
    monkeypatch.setitem(sys.modules, "llmcall.process", FakeProcess)
    monkeypatch.setattr(agent_run.agent_task, "get", lambda item_id: {"id": item_id, "ext": {}})
    monkeypatch.setattr(agent_run.agent_task, "proc_identity", lambda pid: (True, 1.0))
    monkeypatch.setattr(agent_run.agent_task, "start_runner", lambda *a: True)
    monkeypatch.setattr(agent_run.agent_task, "release", lambda *a: True)
    monkeypatch.setattr(agent_run.agent_task, "default_workspace", lambda: os.getcwd())
    monkeypatch.setattr(agent_run, "_execute_order", lambda *a, **k: 0)
    assert agent_run.run_order("item", post_reports=False, generation=3) == 0
    token, = scopes
    assert isinstance(token, agent_run.PolledCancellation)
    assert isinstance(token.exact, agent_run.OperationCancellation)
    assert (token.exact.item_id, token.exact.generation) == ("item", 3)
