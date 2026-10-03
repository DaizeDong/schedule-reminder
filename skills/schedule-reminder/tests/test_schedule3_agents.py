"""Synthetic termination and inspection controls for S2-C05/C06."""
import json
from pathlib import Path
import subprocess
import pytest
import agent_task
import agent_tick
import agent_run
F=json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))

@pytest.mark.parametrize('mode',['nonzero','nonzero-gone','nonzero-reused','exception','still-live'])
def test_kill_failure_is_explicit(monkeypatch,mode):
    after = (False,None) if mode=='nonzero-gone' else ((True,32) if mode=='nonzero-reused' else (True,31))
    identities=iter([(True,31),after])
    monkeypatch.setattr(agent_task,'proc_identity',lambda _:next(identities))
    def run(*a,**k):
        if mode=='exception': raise OSError('synthetic taskkill unavailable')
        return subprocess.CompletedProcess(a,1 if mode.startswith('nonzero') else 0,'','synthetic failure')
    monkeypatch.setattr(agent_task.subprocess,'run',run)
    with pytest.raises(RuntimeError):
        agent_task.kill_tree(3101,31)

def test_kill_verified_control(monkeypatch):
    live=iter([(True,31),(False,31)])
    monkeypatch.setattr(agent_task,'proc_identity',lambda _:next(live))
    monkeypatch.setattr(agent_task.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,'',''))
    assert agent_task.kill_tree(3101,31) is True

def test_already_exited_control(monkeypatch):
    monkeypatch.setattr(agent_task,'proc_identity',lambda _:(False,None))
    monkeypatch.setattr(agent_task.subprocess,'run',lambda *a,**k:pytest.fail('must not kill an exited identity'))
    assert agent_task.kill_tree(3101,31) is False

def test_failed_stop_remains_tracked_then_reaped(monkeypatch):
    item={'id':F['message_id'],'title':F['title'],'state':'doing','ext':{
        agent_task.EXT_STATE:agent_task.STATE_RUNNING,agent_task.EXT_PID:3101,
        agent_task.EXT_PSTART:31,agent_task.EXT_STREAM:F['stream']}}
    def rem(*args):
        if args[0]=='update':
            item['ext'].update(json.loads(args[args.index('--ext')+1]))
        elif args[0]=='transition':
            item['state']=args[args.index('--to')+1]
        return {'item':item}
    monkeypatch.setattr(agent_task,'rem',rem)
    monkeypatch.setattr(agent_task,'orders',lambda active_only=True:[item] if item['state'] not in {'done','cancelled'} else [])
    monkeypatch.setattr(agent_task,'append_event',lambda *a,**k:None)
    monkeypatch.setattr(agent_task,'proc_identity',lambda _:(True,31))
    original_run = agent_task.subprocess.run
    termination_attempts = []
    def failed_termination(argv, *args, **kwargs):
        if Path(argv[0]).stem.lower() == 'taskkill':
            termination_attempts.append(argv)
            return subprocess.CompletedProcess(argv,1,'','synthetic access denied')
        return original_run(argv, *args, **kwargs)
    monkeypatch.setattr(agent_task.subprocess,'run',failed_termination)
    result=agent_tick.stop(item['id'],post=False)
    assert len(termination_attempts) == 1
    assert result[0].get('status')=='stop_pending'
    assert item['state'] in {'doing','blocked'}
    assert agent_task.running([item])==[item]
    monkeypatch.setattr(agent_task,'proc_identity',lambda _:(False,None))
    result=agent_tick.reap([item],post=False)
    assert result==[item['id']]
    assert item['state']=='cancelled'

@pytest.mark.parametrize('mode',['nonzero','exception'])
def test_status_inspection_failure_has_explicit_provenance(tmp_path,monkeypatch,mode):
    (tmp_path/'.git').mkdir(exist_ok=True)
    def run(*a,**k):
        if mode=='exception': raise subprocess.TimeoutExpired(a,1)
        return subprocess.CompletedProcess(a,128,'','synthetic status failure')
    monkeypatch.setattr(agent_run.subprocess,'run',run)
    files,via=agent_run.detect_changes(str(tmp_path),['synthetic-change.txt'])
    assert files==['synthetic-change.txt']
    assert 'unavailable' in via and 'self-reported' in via
    assert via in agent_run.review_prompt('synthetic','synthetic',files,via,None,None,'')
    assert via in agent_run._done_report('synthetic','synthetic','synthetic',files,via,None,None,'','synthetic','DONE',0,1,str(tmp_path))

def test_empty_git_status_is_confirmed_clean(tmp_path,monkeypatch):
    (tmp_path/'.git').mkdir(exist_ok=True)
    monkeypatch.setattr(agent_run.subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a,0,'',''))
    assert agent_run.detect_changes(str(tmp_path),['synthetic-change.txt'])==([], 'git')


@pytest.mark.skipif(agent_task.sys.platform != 'win32', reason='Windows identity API')
def test_access_denied_is_not_an_exited_identity(monkeypatch):
    from types import SimpleNamespace
    kernel=SimpleNamespace(OpenProcess=lambda *a:0, GetLastError=lambda:5)
    monkeypatch.setattr(agent_task.ctypes,'windll',SimpleNamespace(kernel32=kernel))
    with pytest.raises(RuntimeError,match='identity'):
        agent_task.kill_tree(3101,31)
