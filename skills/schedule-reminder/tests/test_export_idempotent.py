#!/usr/bin/env python3
"""运行事件的持久副本不许出现重复行。

`run-events.jsonl` 按它自己的 docstring 是**运行事件唯一的持久副本**:数据库是缓存,
删了重跑 --backfill 就回来;而 Windows 那个通道是滚动缓冲,约五天覆盖一次,
所以过了那个窗口,这个文件里没有的事件就是没有了。

导出的幂等原本靠 meta 表里的水位线,而「追加文件」和「提交水位线」不可能原子完成:
  - 先写文件后提交:提交失败(锁超时、崩溃)时文件里已经有那批行,meta 还停在旧值,
    下一轮把同一批**再导一遍**。按这份文件重建历史的人会得到翻倍的启动次数,
    而文件里没有任何去重键校验。
  - 先提交后写文件:崩在中间就是 meta 说导过了而文件里没有,那批事件永久丢失。

两种顺序各自会坏,所以正确性不能靠顺序 —— **水位线的权威是文件本身**,
meta 只是缓存。这里钉的就是这一条,用的手段是把「提交没成功」这件事真的制造出来。
"""
import json
import os
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import console_ingest as CI  # noqa: E402
import console_store  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_DB", str(tmp_path / "console.sqlite3"))
    con, path = CI.open_rw()
    assert con, f"建不出临时数据库: {path}"
    yield con
    con.close()


def _feed(db, n, epoch=1, first_rid=1000):
    for i in range(n):
        db.execute(
            "INSERT OR IGNORE INTO run_event"
            "(log_epoch,record_id,task,event_id,ts,day,hour,rc_raw,rc_norm) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (epoch, first_rid + i, "AcmeTask", 201, "2026-09-07 08:06:02",
             "2026-09-07", 8, "0", 0))
    db.commit()


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def test_a_second_export_adds_nothing(db):
    """正对照 + 基本幂等:导两次,文件里还是那些行。"""
    p, _ = console_store.resolve_db()
    out = p.parent / "run-events.jsonl"
    _feed(db, 5)
    ok, n, _ = CI.export_run_events(db)
    assert ok and n == 5
    assert len(_lines(out)) == 5
    ok, n, _ = CI.export_run_events(db)
    assert ok and n == 0, "第二次又导了一遍"
    assert len(_lines(out)) == 5


def test_a_failed_watermark_commit_does_not_duplicate_lines(db, monkeypatch):
    """把「文件写了、水位线没提交上」这件事真的制造出来。

    这是整条用例的重点:光测「连着导两次」证明不了什么,因为那条路上水位线是提交成功的。
    要证明的是**提交失败之后**下一轮不会重导。
    """
    p, _ = console_store.resolve_db()
    out = p.parent / "run-events.jsonl"
    _feed(db, 4)

    real_begin = CI.begin
    monkeypatch.setattr(CI, "begin", lambda con: (_ for _ in ()).throw(
        RuntimeError("database is locked")))
    with pytest.raises(Exception):
        CI.export_run_events(db)
    monkeypatch.setattr(CI, "begin", real_begin)

    # 文件里已经有这 4 行,而 meta 的水位线没动 —— 这正是那个危险状态。
    assert len(_lines(out)) == 4
    mark = db.execute("SELECT value FROM meta WHERE key='export_watermark'").fetchone()
    assert mark is None or mark["value"] in ("0:0", None), \
        "水位线居然提交上了,这条用例没有制造出它要测的状态"

    ok, n, _ = CI.export_run_events(db)
    assert ok
    assert n == 0, f"提交失败之后又把同一批导了一遍({n} 条)"
    assert len(_lines(out)) == 4, "持久副本里出现了重复行"


def test_duplicate_record_ids_never_appear(db, monkeypatch):
    """无论中间失败多少次,(epoch, record_id) 在文件里必须唯一。

    这一条不看条数看键:条数相等也可能是「少导了一批又重导了一批」。
    """
    p, _ = console_store.resolve_db()
    out = p.parent / "run-events.jsonl"
    real_begin = CI.begin
    for round_i in range(3):
        _feed(db, 3, first_rid=1000 + round_i * 10)
        if round_i == 1:
            monkeypatch.setattr(CI, "begin", lambda con: (_ for _ in ()).throw(
                RuntimeError("database is locked")))
            with pytest.raises(Exception):
                CI.export_run_events(db)
            monkeypatch.setattr(CI, "begin", real_begin)
        else:
            CI.export_run_events(db)
    CI.export_run_events(db)

    keys = [(d["e"], d["r"]) for d in _lines(out)]
    assert len(keys) == len(set(keys)), (
        f"持久副本里有重复的 (epoch, record_id):"
        f"{[k for k in set(keys) if keys.count(k) > 1][:5]}")
    assert len(keys) == 9, f"九条事件应当全部导出,实际 {len(keys)}"


def test_a_torn_last_line_does_not_reset_the_watermark(db):
    """文件末尾有一条写到一半的残行时,要往前再找一条,而不是把整份判成读不出来。

    判成读不出来会回落到 meta 的水位线;而在「写了没提交」的那个状态下,
    那正好是**会重导**的那个值。
    """
    p, _ = console_store.resolve_db()
    out = p.parent / "run-events.jsonl"
    _feed(db, 3)
    CI.export_run_events(db)
    with out.open("a", encoding="utf-8") as fh:
        fh.write('{"e": 1, "r": 999999, "t": "Acme')      # 断掉的一行
    e, r = CI._watermark_from_file(out)
    assert (e, r) == (1, 1002), f"残行让水位线退回了 {(e, r)}"
