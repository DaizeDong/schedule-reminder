"""Synthetic termination and inspection controls for S2-C05/C06."""
import json
from pathlib import Path
import subprocess
import pytest
import agent_task
import agent_run
F=json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('mode',['nonzero','timeout','missing'])
def test_status_inspection_failure_has_explicit_provenance(tmp_path,monkeypatch,mode):
    (tmp_path/'.git').mkdir(exist_ok=True)
    def run(argv,*a,**k):
        if mode == 'timeout':
            raise subprocess.TimeoutExpired(argv, 30)
        if mode == 'missing':
            raise FileNotFoundError('synthetic git missing')
        return subprocess.CompletedProcess(argv, 128, '', 'synthetic status failure')
    monkeypatch.setattr(agent_run.subprocess,'run',run)
    files,via=agent_run.detect_changes(str(tmp_path),['synthetic-change.txt'])
    assert files==['synthetic-change.txt']
    assert 'unavailable' in via and 'self-reported' in via
    assert via in agent_run.review_prompt('synthetic','synthetic',files,via,None,None,'')
    assert via in agent_run._done_report('synthetic','synthetic','synthetic',files,via,None,None,'','synthetic','DONE',0,1,str(tmp_path))

def test_empty_git_status_is_confirmed_clean(tmp_path,monkeypatch):
    (tmp_path/'.git').mkdir(exist_ok=True)
    calls = []
    def run(argv,*a,**k):
        calls.append((argv, k))
        return subprocess.CompletedProcess(argv, 0, '', '')
    monkeypatch.setattr(agent_run.subprocess,'run',run)
    monkeypatch.setattr(agent_run,'_observed_call',lambda *a,**k:pytest.fail('read-only git is not journaled'))
    assert agent_run.detect_changes(str(tmp_path),['synthetic-change.txt'])==([], 'git')
    assert calls[0][0][:3] == ['git','--no-optional-locks','status'] and calls[0][1]['timeout'] == 30
    assert calls[0][1]['cwd'] == str(tmp_path)


def test_git_status_keeps_the_first_path_whole(tmp_path,monkeypatch):
    (tmp_path/'.git').mkdir(exist_ok=True)
    monkeypatch.setattr(agent_run.subprocess,'run',lambda argv,*a,**k:subprocess.CompletedProcess(
        argv, 0, ' M data/first.txt\n?? second.txt\n', ''))
    assert agent_run.detect_changes(str(tmp_path),[])==(['data/first.txt','second.txt'], 'git')


@pytest.mark.skipif(agent_task.sys.platform != 'win32', reason='Windows identity API')
def test_access_denied_is_not_an_exited_identity(monkeypatch):
    from types import SimpleNamespace
    kernel=SimpleNamespace(OpenProcess=lambda *a:0, GetLastError=lambda:5)
    monkeypatch.setattr(agent_task.ctypes,'windll',SimpleNamespace(kernel32=kernel))
    with pytest.raises(RuntimeError,match='identity'):
        agent_task.proc_identity(3101)
