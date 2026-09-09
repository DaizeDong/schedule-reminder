#!/usr/bin/env python3
"""读取失败不许被记成一次成功的摄入。

`ingest_runlog` 只判 `raw.get("enabled")`。而 **enabled=True 不等于这次读成功了** ——
两条通路都会在读到一半失败时交出 enabled=True 加一个 reason:

  - `runlog.ps1` 在 Get-WinEvent 非「没有事件」的异常里输出
    `enabled=$true / reason='read failed: ...' / events=@()`,然后 exit 0;
  - `evtlog.read` 在 EvtNext 中途失败时返回 `enabled=True / partial=True` 加半批事件。

原来这两种都会走完整段:added=0、ingest_run 落一行 ok=1、
**`runlog-ingest-ok.txt` 的 mtime 被刷新** —— 而那个戳文件的 docstring 承诺的是
「只有成功的 runlog 摄入才写」,健康监控盯的正是它的新鲜度。
于是屏幕上:摄入判绿、顶栏写「最后摄入 刚才」,而这个 Windows 通道约五天覆盖一次,
这段没被摄入的运行历史**是永久丢失的**。

同一份载荷 `server.py` 那边判的是 available=False:两个读者对同一个事实给出相反答案。

这里钉三件事:整读失败 -> 失败且不写戳;部分读取 -> 事件仍然入库但不算成功、不写戳;
正常读取 -> 成功且写戳(正对照,否则「永远不写戳」也能让上面两条通过)。
"""
import os
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import console_ingest as CI  # noqa: E402
import console_store  # noqa: E402


def _events(n=3):
    return [{"task": "AcmeTask", "id": 201, "rid": 9000 + i,
             "t": "2026-09-07 08:06:02", "rc": "0"} for i in range(n)]


PAYLOADS = {
    # (说明, 载荷, 期望成功?, 期望写戳?, 期望入库条数)
    "读取整个失败(PowerShell 那条的形状)": (
        {"enabled": True, "reason": "read failed: Attempted to perform an unauthorized operation.",
         "events": [], "maxRecordId": 0, "oldestRecordId": None, "recordCount": None},
        False, False, 0),
    "读到一半失败(evtlog 那条的形状)": (
        {"enabled": True, "reason": "事件读到一半失败(EvtNextError),下面的条数是不完整的",
         "partial": True, "events": _events(3),
         "maxRecordId": 9002, "oldestRecordId": 1, "recordCount": 500},
        False, False, 3),
    "正常读取": (
        {"enabled": True, "reason": None, "partial": False, "events": _events(4),
         "maxRecordId": 9003, "oldestRecordId": 1, "recordCount": 500},
        True, True, 4),
    "通道是关闭的": (
        {"enabled": False, "reason": "通道是关闭的", "events": []},
        False, False, 0),
}


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_DB", str(tmp_path / "console.sqlite3"))
    con, path = CI.open_rw()
    assert con, f"建不出临时数据库: {path}"
    yield con
    con.close()


@pytest.mark.parametrize("label", list(PAYLOADS))
def test_a_read_failure_is_not_recorded_as_a_successful_ingest(label, db, monkeypatch):
    raw, want_ok, want_stamp, want_rows = PAYLOADS[label]
    monkeypatch.setattr(CI, "_read_runlog", lambda days: (raw, "evtlog"))

    p, _ = console_store.resolve_db()
    stamp = p.parent / "runlog-ingest-ok.txt"
    assert not stamp.exists(), "临时目录里不该已经有成功戳"

    ok, added, msg = CI.ingest_runlog(db, 30)
    db.commit()

    assert ok is want_ok, f"{label}: 返回 ok={ok},期望 {want_ok}({msg})"
    assert stamp.exists() is want_stamp, (
        f"{label}: 成功戳 exists={stamp.exists()},期望 {want_stamp}。"
        f"一个在失败时也刷新的戳,和一个每次运行都刷新的日志文件是同一种东西。")

    # 事件该入库的还是要入库:部分失败不等于把已经读到的那半批扔掉。
    rows = db.execute("SELECT COUNT(*) FROM run_event").fetchone()[0]
    assert rows == want_rows, f"{label}: 入库 {rows} 条,期望 {want_rows}"

    # ingest_run 里那一行的 ok 必须和返回值一致 —— 事后翻账本时它是唯一的线索。
    last = db.execute(
        "SELECT ok, note FROM ingest_run WHERE source='runlog' "
        "ORDER BY rowid DESC LIMIT 1").fetchone()
    assert last is not None, f"{label}: 账本里一行都没记"
    assert bool(last["ok"]) is want_ok, f"{label}: 账本记的 ok 和返回值不一致"
    if not want_ok:
        assert last["note"], f"{label}: 失败了却没有写下任何原因"


def test_the_reason_is_carried_into_the_ledger(db, monkeypatch):
    """失败的原因要能在账本里读到,不能只剩一个 events=0。

    唯一线索是「摄入成功 0 条」的话,人没法判断该查哪边。
    """
    raw = {"enabled": True, "events": [],
           "reason": "read failed: AcmeSpecificFailureText"}
    monkeypatch.setattr(CI, "_read_runlog", lambda days: (raw, "powershell"))
    ok, _added, msg = CI.ingest_runlog(db, 30)
    db.commit()
    assert not ok
    assert "AcmeSpecificFailureText" in msg
    note = db.execute("SELECT note FROM ingest_run WHERE source='runlog' "
                      "ORDER BY rowid DESC LIMIT 1").fetchone()["note"]
    assert "AcmeSpecificFailureText" in note, note
    assert "powershell" in note, "账本里没写走的是哪条通路"


def test_a_total_read_failure_reads_differently_from_a_partial_one(db, monkeypatch):
    """账本必须分得出「一条都没读到」和「读到一半」。

    两者的 ok 都是 0、成功戳都不写,所以从那两个信号上分不出来 ——
    而对看账本的人这是两件事:前者要去查权限或通道,后者已经捞回了一部分,
    要判断的是缺了哪一段。**把两种失败折叠成同一句话,等于把「查哪边」这个信息扔掉。**

    (这条是投毒逼出来的:只留一层判断时,整读失败会被归进「部分读取」那条路,
     两条投毒里有一条永远不会红。)
    """
    notes = {}
    for label, raw in (
        ("total", {"enabled": True, "events": [], "reason": "read failed: AcmeTotalFailure"}),
        ("partial", {"enabled": True, "partial": True, "events": _events(2),
                     "reason": "AcmePartialFailure", "maxRecordId": 9001,
                     "oldestRecordId": 1, "recordCount": 9}),
    ):
        monkeypatch.setattr(CI, "_read_runlog", lambda days, r=raw: (r, "evtlog"))
        CI.ingest_runlog(db, 30)
        db.commit()
        notes[label] = db.execute(
            "SELECT note FROM ingest_run WHERE source='runlog' "
            "ORDER BY rowid DESC LIMIT 1").fetchone()["note"]

    assert "读取失败" in notes["total"], notes["total"]
    assert "部分读取" in notes["partial"], notes["partial"]
    assert notes["total"] != notes["partial"]
    # 部分读取那条要说清「已经捞回了几条」,否则「读到一半」等于没说。
    assert "events=2" in notes["partial"], notes["partial"]
