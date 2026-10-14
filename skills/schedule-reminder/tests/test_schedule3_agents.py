"""Synthetic termination controls for S2-C05/C06."""
import pytest
import agent_task


@pytest.mark.skipif(agent_task.sys.platform != 'win32', reason='Windows identity API')
def test_access_denied_is_not_an_exited_identity(monkeypatch):
    from types import SimpleNamespace
    kernel=SimpleNamespace(OpenProcess=lambda *a:0, GetLastError=lambda:5)
    monkeypatch.setattr(agent_task.ctypes,'windll',SimpleNamespace(kernel32=kernel))
    with pytest.raises(RuntimeError,match='identity'):
        agent_task.proc_identity(3101)
