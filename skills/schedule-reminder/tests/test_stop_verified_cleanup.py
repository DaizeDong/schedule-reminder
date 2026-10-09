"""A stop releases the serial slot only after a verified kill of the whole runner tree.

Synthetic process tables only: no real process is observed, opened or killed. The fake backend is
installed through agent_task.process_backend and kill_tree is stubbed, so the same file runs
against a tree without the feature (where the release case must fail: the negative control).
"""
import pytest

import agent_task
import agent_tick
import store
from test_agent_lifecycle import make_order, pool, started_order  # noqa: F401  (fixture reuse)

RUNNER_PID, RUNNER_START = 123, 456


class FakeProcesses:
    """A synthetic process table. A handle is the process record, so PID reuse is modelled."""

    def __init__(self):
        self.current, self.snapshots, self.fail_snapshot_at = {}, 0, None

    def spawn(self, pid, ppid, created):
        record = {"pid": pid, "ppid": ppid, "created": created, "alive": True}
        self.current[pid] = record
        return record

    def end(self, pid):
        self.current.pop(pid)["alive"] = False

    def snapshot(self):
        self.snapshots += 1
        if self.fail_snapshot_at == self.snapshots:
            raise RuntimeError("synthetic snapshot failure")
        return {pid: r["ppid"] for pid, r in self.current.items()}

    def open(self, pid):
        return self.current.get(pid)

    def created(self, handle):
        return handle["created"]

    def alive(self, handle):
        return handle["alive"]

    def close(self, handle):
        pass


@pytest.fixture
def tree(pool, monkeypatch):  # noqa: F811
    fake = FakeProcesses()
    fake.spawn(1, 0, 1)                                 # unrelated system process
    fake.spawn(RUNNER_PID, 1, RUNNER_START)             # the recorded runner
    fake.spawn(200, RUNNER_PID, 500)                    # llmcall child
    fake.spawn(300, 200, 600)                           # model CLI grandchild
    fake.spawn(77, RUNNER_PID, 100)                     # older than the runner: a recycled-PPID stranger
    monkeypatch.setattr(agent_task, "process_backend", lambda: fake, raising=False)
    # The reaper's own liveness probe must not see a real process with the synthetic PID.
    monkeypatch.setattr(agent_task, "proc_identity", lambda pid: (False, None))
    iid = started_order(pool)
    assert agent_task.child_started(iid, 1, {"phase": "actor"})   # in_flight: what blocks today
    fake.iid = iid
    return fake


def stop_with(monkeypatch, fake, *, survivors=(), during=None, after=None, killed=True):
    def kill(pid, pstart):
        assert (pid, pstart) == (RUNNER_PID, RUNNER_START)
        if during:
            during(fake)
        for target in [p for p in fake.current if p in (RUNNER_PID, 200, 300) and p not in survivors]:
            fake.end(target)
        if after:
            after(fake)
        return killed
    monkeypatch.setattr(agent_task, "kill_tree", kill)
    result = agent_tick.stop(fake.iid, post=False, expected_generation=1)
    assert result[0]["stopped"] is True
    assert store.get_item(fake.iid)["state"] == "cancelled"
    return result[0]


def slot_released(fake):
    agent_tick.reap(post=False)
    return agent_task.operation(fake.iid)["released_at"] is not None


def test_verified_kill_with_every_descendant_gone_releases_the_slot(tree, monkeypatch, pool):  # noqa: F811
    stop_with(monkeypatch, tree)
    op = agent_task.operation(tree.iid)
    assert op["released_at"] and op["cleanup_state"] == "quiescent"
    assert '"authority": "verified-tree-kill"' in op["cleanup_receipt"]
    assert slot_released(tree)
    assert agent_task.claim(make_order(pool)["id"])


def test_recorded_grandchild_surviving_a_dead_parent_holds_the_slot(tree, monkeypatch, pool):  # noqa: F811
    result = stop_with(monkeypatch, tree, survivors=(300,))
    assert result.get("cleanup") == "held"
    assert not slot_released(tree)
    assert not agent_task.claim(make_order(pool)["id"])


def test_child_spawned_after_recording_that_survives_holds_the_slot(tree, monkeypatch, pool):  # noqa: F811
    stop_with(monkeypatch, tree, during=lambda fake: fake.spawn(400, 200, 700))
    assert not slot_released(tree)
    assert not agent_task.claim(make_order(pool)["id"])


@pytest.mark.parametrize("failing_snapshot", [1, 2], ids=["before-kill", "fresh-after-kill"])
def test_snapshot_failure_holds_the_slot(tree, monkeypatch, pool, failing_snapshot):  # noqa: F811
    tree.fail_snapshot_at = failing_snapshot
    stop_with(monkeypatch, tree)
    assert not slot_released(tree)
    assert not agent_task.claim(make_order(pool)["id"])


def test_parent_exit_alone_is_never_cleanup(tree, monkeypatch, pool):  # noqa: F811
    stop_with(monkeypatch, tree, killed=False)
    assert not slot_released(tree)


def test_recycled_pid_children_are_not_mistaken_for_survivors(tree, monkeypatch, pool):  # noqa: F811
    def recycle(fake):
        # After the kill pid 200 belongs to a newer stranger whose own child is alive.
        fake.spawn(200, 1, 9000)
        fake.spawn(501, 200, 9100)
    stop_with(monkeypatch, tree, after=recycle)
    assert agent_task.operation(tree.iid)["released_at"] is not None
    assert agent_task.claim(make_order(pool)["id"])


def test_recycled_pid_does_not_hide_a_late_survivor(tree, monkeypatch, pool):  # noqa: F811
    def recycle(fake):
        # The recorded child started 450 after recording and died; 450 survived; pid 200 reused.
        fake.spawn(200, 1, 9000)
    stop_with(monkeypatch, tree, during=lambda fake: fake.spawn(450, 200, 650), after=recycle)
    assert not slot_released(tree)
    assert not agent_task.claim(make_order(pool)["id"])


def test_store_refuses_a_receipt_that_omits_a_recorded_identity(pool):  # noqa: F811
    iid = started_order(pool)
    assert not agent_task.cancel(iid, expected_generation=1).get("_err")
    op = agent_task.operation(iid)
    receipt = {"authority": "verified-tree-kill", "roots": [[999, "1"]], "members": [[999, "1"]],
               "confirmed_at": "2026-01-01T00:00:00+00:00"}
    assert not agent_task.release_verified_cleanup(iid, 1, receipt, op)
    with pytest.raises(ValueError):
        agent_task.release_verified_cleanup(iid, 1, {"authority": "operator-reviewed"}, op)
    assert agent_task.operation(iid)["released_at"] is None
