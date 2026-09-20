"""Synthetic owner-contract fixtures; no runtime or user data."""
import json
import sqlite3


def artifact_workspace(path, size=24):
    """Generate a non-code workspace containing a synthetic report."""
    path.mkdir(parents=True, exist_ok=True)
    (path / 'report.txt').write_text(('Acme synthetic report.\n' * (size // 22 + 1))[:size], encoding='utf-8')
    return path


def action_database(path):
    """Generate a full owner database and one actionable synthetic todo."""
    import store
    store.init_db(str(path))
    item = store.add_item('整理 Acme 报告', description='整理合成资料，输出一份报告。',
                          source='user', db_path=str(path))
    return item


def work_database(path):
    with sqlite3.connect(path) as conn:
        conn.executescript('''CREATE TABLE items(id TEXT PRIMARY KEY,title TEXT,state TEXT,source TEXT,
            description TEXT,priority INTEGER,progress INTEGER,due_at TEXT,scheduled_at TEXT,
            project TEXT,created_at TEXT,updated_at TEXT,ext TEXT);
            CREATE TABLE events(seq INTEGER PRIMARY KEY,ts TEXT,item_id TEXT,actor TEXT,event_type TEXT,
            from_state TEXT,to_state TEXT);''')
        rows = [
            ('work-done','Acme result','done','agent-center:work',{'x_agent_exec_state':'done','x_agent_exec_note':'Synthetic summary','secret':'never forward'}),
            ('work-stalled','Acme draft','blocked','agent-center:work',{'x_agent_exec_state':'stalled'}),
            ('work-running','Acme active','doing','agent-center:work',{'x_agent_exec_state':'running'}),
            ('tracked','Synthetic commitment','pending','user',{}),
            ('signal','Synthetic news','pending','daily-hotspots',{}),
        ]
        for item_id,title,state,source,ext in rows:
            conn.execute('INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (item_id,title,state,source,'Synthetic description',1,0,None,None,'Acme',
                 '2030-01-01T00:00:00Z','2030-01-02T00:00:00Z',json.dumps(ext)))
        for seq,item_id in enumerate(('work-done','signal','tracked'),1):
            conn.execute('INSERT INTO events VALUES (?,?,?,?,?,?,?)',
                (seq,'2030-01-02T00:00:00Z',item_id,'synthetic','transition','pending','done'))
    return path
