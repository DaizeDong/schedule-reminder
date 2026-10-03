"""Generated synthetic paired regressions for S2-C02/C03/C04/C11."""
import json
from pathlib import Path
import threading
import pytest
import store
F=json.loads((Path(__file__).parent/'capability_cases.json').read_text(encoding='utf-8'))
C=F['schedule3']

@pytest.fixture
def db(tmp_path):
    path=str(tmp_path/'schedule3.sqlite3')
    store.init_db(path)
    return path

def add(db,**kw):
    return store.add_item(C['title'],db_path=db,due_at=C['past'],**kw)

def mutate(db,item,op):
    if op=='done': store.done(item['id'],db_path=db)
    elif op=='cancelled': store.transition(item['id'],'cancelled',db_path=db)
    elif op=='snooze': store.snooze(item['id'],C['future'],db_path=db)
    elif op=='reschedule': store.update_item(item['id'],due_at=C['future'],db_path=db)
    elif op=='doing': store.transition(item['id'],'doing',db_path=db)
    elif op=='blocked': store.block(item['id'],reason=C['reason'],db_path=db)
    elif op=='recurring': store.update_item(item['id'],recurrence=C['recurrence'],db_path=db)

@pytest.mark.parametrize('failure',['busy','other','interrupt'])
def test_entry_failure_releases_lock(monkeypatch,failure):
    lock=threading.RLock()
    monkeypatch.setattr(store,'_WRITE_LOCK',lock)
    monkeypatch.setattr(store.time,'sleep',lambda _:None)
    error=store.sqlite3.OperationalError('database is locked' if failure=='busy' else 'synthetic begin failure') if failure!='interrupt' else KeyboardInterrupt()
    class Conn:
        def execute(self,sql): raise error
    try:
        with pytest.raises(store.SkillError if failure=='busy' else type(error)) as caught:
            with store._Tx(Conn()): pass
        if failure!='busy': assert caught.value is error
        assert not lock._is_owned()
    finally:
        while lock._is_owned(): lock.release()

@pytest.mark.parametrize('raises',[False,True])
def test_transaction_commit_rollback_control(monkeypatch,raises):
    lock=threading.RLock();monkeypatch.setattr(store,'_WRITE_LOCK',lock)
    calls=[]
    class Conn:
        def execute(self,sql): calls.append(sql)
    try:
        with store._Tx(Conn()):
            if raises: raise ValueError('synthetic abort')
    except ValueError: pass
    assert calls==['BEGIN IMMEDIATE','ROLLBACK' if raises else 'COMMIT']
    assert not lock._is_owned()

@pytest.mark.parametrize('op',C['before_claim'])
def test_changed_snapshot_is_not_delivered(db,monkeypatch,op):
    item=add(db);original=store._due_reached;armed=True;sent=[]
    def before_claim(*a,**k):
        nonlocal armed
        result=original(*a,**k)
        if armed:
            armed=False;mutate(db,item,op)
        return result
    monkeypatch.setattr(store,'_due_reached',before_claim)
    result=store.tick(db_path=db,now=F['now'],notify_fn=lambda x:sent.append(x) or True)
    assert sent==[]
    assert item['id'] not in result['dispatched']

@pytest.mark.parametrize('op',C['before_claim'])
def test_change_after_claim_is_revalidated(db,monkeypatch,op):
    item=add(db);original=store._Tx.__exit__;armed=True;sent=[]
    def exit_hook(self,*a):
        nonlocal armed
        result=original(self,*a)
        if armed:
            armed=False;mutate(db,item,op)
        return result
    monkeypatch.setattr(store._Tx,'__exit__',exit_hook)
    store.tick(db_path=db,now=F['now'],notify_fn=lambda x:sent.append(x) or True)
    assert sent==[]

def test_recurring_success_preserves_new_due_during_delivery(db):
    item=add(db,recurrence=C['recurrence'])
    def send(_):
        mutate(db,item,'reschedule')
        return True
    store.tick(db_path=db,now=F['now'],notify_fn=send)
    current=store.get_item(item['id'],db_path=db)
    assert current['due_at']==store.to_rfc3339(store.parse_dt(C['future']))
    assert current['notified_at'] is None

@pytest.mark.parametrize('terminal',['done','cancelled'])
@pytest.mark.parametrize('delivery',['false','exception'])
def test_failed_delivery_preserves_terminal_change(db,terminal,delivery):
    item=add(db)
    conn=store._connect(db)
    conn.execute('UPDATE items SET retry_count=? WHERE id=?',(store._NOTIFY_MAX_RETRIES-1,item['id']));conn.close()
    def send(_):
        mutate(db,item,terminal)
        if delivery=='exception': raise RuntimeError('synthetic delivery failure')
        return False
    result=store.tick(db_path=db,now=F['now'],notify_fn=send)
    assert store.get_item(item['id'],db_path=db)['state']==terminal
    assert result['blocked']==[] and result['retried']==[]

@pytest.mark.parametrize('recurring',[False,True])
def test_unchanged_delivery_control(db,recurring):
    item=add(db,recurrence=C['recurrence'] if recurring else None)
    result=store.tick(db_path=db,now=F['now'],notify_fn=lambda _:True)
    assert item['id'] in result['dispatched']
    current=store.get_item(item['id'],db_path=db)
    assert (current['due_at']>store.to_rfc3339(store.parse_dt(F['now']))) if recurring else current['notified_at']

@pytest.mark.parametrize('op',['doing','blocked','recurring','reschedule'])
def test_lapse_rechecks_complete_eligibility(db,monkeypatch,op):
    item=add(db);store.tick(db_path=db,now=C['past'],notify_fn=lambda _:True)
    original=store._Tx.__enter__;armed=True
    def before_tx(self):
        nonlocal armed
        if armed:
            armed=False;mutate(db,item,op)
        return original(self)
    monkeypatch.setattr(store._Tx,'__enter__',before_tx)
    result=store.sweep_lapsed(db_path=db,now=F['now'])
    assert result['lapsed']==[]
    assert store.get_item(item['id'],db_path=db)['state']!='cancelled'

def test_lapse_eligible_control(db):
    item=add(db);store.tick(db_path=db,now=C['past'],notify_fn=lambda _:True)
    result=store.sweep_lapsed(db_path=db,now=F['now'])
    assert [x['id'] for x in result['lapsed']]==[item['id']]

@pytest.mark.parametrize('state,patch,code',C['bad_patches'])
def test_update_validates_complete_item(db,state,patch,code):
    item=add(db)
    if state=='blocked': store.block(item['id'],reason=C['reason'],db_path=db)
    elif state!='pending': store.transition(item['id'],state,db_path=db)
    before=store.get_item(item['id'],db_path=db)
    with pytest.raises(store.SkillError) as caught: store.update_item(item['id'],db_path=db,**patch)
    assert caught.value.error_code==code
    assert store.get_item(item['id'],db_path=db)==before

def test_done_update_rejects_unmet_dependency(db):
    item=add(db);other=add(db);store.done(item['id'],db_path=db)
    with pytest.raises(store.SkillError) as caught:
        store.update_item(item['id'],db_path=db,relations=[{'type':'depends-on','target_id':other['id']}])
    assert caught.value.error_code=='ERR_DEPENDENCY_UNMET'

def test_valid_ext_patch_preserves_terminal_invariants(db):
    item=add(db,ext={'synthetic_kept':1});store.done(item['id'],db_path=db)
    current=store.update_item(item['id'],db_path=db,description=C['title'],ext={'synthetic_added':2})
    assert current['progress']==100 and current['end_at']
    assert current['ext']=={'synthetic_kept':1,'synthetic_added':2}
