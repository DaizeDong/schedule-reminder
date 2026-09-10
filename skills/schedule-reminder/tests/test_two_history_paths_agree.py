#!/usr/bin/env python3
"""健康观察有两条通路,它们交给页面的字段必须是同一套。

主通路是数据库(`server.load_from_db`),回落通路是直接解析日志(`history.load`)。
`load_from_db` 的 docstring 自己写着「Returns (hist, runs, note) shaped EXACTLY like the
file-parsing versions, because console.html reads seventeen keys off them」—— 而它漏了一个。

漏掉的是 `other`:判词表认不出来的那些观察。它**进分母**(judged = 全部 - neutral)
却不出现在任何字段里,于是监控器换一种措辞(FAIL 而不是 FAILED、DEGRADED、WARN)之后,
每一行会显示 health 0.0% 而 ok / bad / stale 全是 0 ——
**同一行里两个自称权威的数字互相矛盾,而没有任何字段说明观察去哪了。**

`history.py` 2026-09 补上了这个桶,还写了一整段注释解释为什么;
而数据库那条**实际会走的**主通路一直没有,所以那次修复在生产通路上完全没生效。
console.html 一直在读 `h.other`,读到的是 undefined。

这里钉的是「两条通路的每任务字段集合一致」,而不是「other 存在」——
按字段集合比对,下一个漏掉的键也会被抓到。

数据是**合成的**,而且刻意包含一条判词表认不出来的措辞。
两条通路都喂同一份合成日志,所以差异只可能来自代码而不是来自两份不同的输入。
"""
import os
import sqlite3
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import console_ingest  # noqa: E402
import console_store  # noqa: E402
import history  # noqa: E402
import server as S  # noqa: E402

# 两条通路都该产出的每任务字段。加一个新字段时两边一起加,这张表跟着改。
REQUIRED = {"obs", "judged", "ok", "bad", "stale", "neutral", "other",
            "health", "visibleRuns", "visibleRunsScope", "byDay"}

# 一份合成的监控日志。措辞照真实格式写,但任务名与内容全是编的。
# 最后一行 "DEGRADED" 是**判词表认不出来的措辞** —— other 这个桶存在的全部理由。
LOG = """\
[2026-09-01 03:00:00]  AcmeSyncJob : OK last 9/1/2026 3:00:00 AM
[2026-09-01 04:00:00]  AcmeSyncJob : OK last 9/1/2026 4:00:00 AM
[2026-09-01 05:00:00]  AcmeSyncJob : FAILED 0x1 last 9/1/2026 5:00:00 AM
[2026-09-01 06:00:00]  AcmeSyncJob : STALE artifact 30h old
[2026-09-01 07:00:00]  AcmeSyncJob : NEUTRAL never run
[2026-09-01 08:00:00]  AcmeSyncJob : DEGRADED something the verdict table has never seen
[2026-09-01 03:00:00]  AcmeReportJob : OK last 9/1/2026 3:00:00 AM
[2026-09-01 04:00:00]  AcmeReportJob : OK last 9/1/2026 4:00:00 AM
"""


@pytest.fixture
def both_paths(tmp_path, monkeypatch):
    """把同一份合成日志同时喂给两条通路,返回 (数据库那份, 日志那份)。

    刻意不读这台机器上的任何东西:一个引用了操作者真实路径的公开仓测试,
    既是 PII 泄漏也是「换台机器就 skip」的空检查。
    """
    log = tmp_path / "monitor.log"
    log.write_text(LOG, encoding="utf-8")
    db = tmp_path / "console.sqlite3"
    monkeypatch.setenv("TASK_CONSOLE_DB", str(db))
    monkeypatch.setenv("TASK_CONSOLE_HISTORY", str(log))

    con, st = console_store.connect_rw(create=True) if hasattr(
        console_store, "connect_rw") else (None, None)
    if con is None:
        con, path = console_ingest.open_rw()
        assert con, f"建不出临时数据库: {path}"
    ok, n, msg = console_ingest.ingest_health(con, str(log), True)
    assert ok, f"合成日志摄入失败: {msg}"
    assert n > 0, "合成日志一条都没被摄入,后面比的是两个空壳"
    con.commit()
    con.close()

    hist_db, _runs, reason = S.load_from_db()
    assert not reason, f"数据库通路不可用: {reason}"
    hist_log = history.load(str(log))
    return hist_db, hist_log


def test_the_synthetic_log_really_contains_an_unrecognised_verdict():
    """先证明这份样本能把 other 这个桶喂起来。

    否则下面几条比的是一个恒为 0 的字段,而那种绿色和真的一致长得一样。
    """
    klasses = [history.classify(ln.split(" : ", 1)[1])
               for ln in LOG.strip().splitlines()]
    assert "other" in klasses, "合成日志里没有一条判词表认不出来的措辞"
    assert {"ok", "bad", "stale", "neutral"} <= set(klasses), "四个已知桶没喂全"


def test_database_path_produces_every_required_field(both_paths):
    db = both_paths[0].get("tasks") or {}
    one = next(iter(db.values()))
    missing = REQUIRED - set(one)
    assert not missing, f"数据库通路少了这些字段: {sorted(missing)}"


def test_log_path_produces_every_required_field(both_paths):
    lg = both_paths[1].get("tasks") or {}
    one = next(iter(lg.values()))
    missing = REQUIRED - set(one)
    assert not missing, f"日志通路少了这些字段: {sorted(missing)}"


def test_the_two_paths_expose_the_same_field_set(both_paths):
    """两边的字段集合必须一致。

    这一条比上面两条强:上面两条只查一张手写清单里的键,而一张手写清单本身就可能漏。
    这一条拿两边互相当对方的判据 —— 任何一边单方面加或减一个字段都会红。
    """
    db = both_paths[0].get("tasks") or {}
    lg = both_paths[1].get("tasks") or {}
    a, b = set(next(iter(db.values()))), set(next(iter(lg.values())))
    assert a == b, (f"只在数据库通路里: {sorted(a - b)};"
                    f" 只在日志通路里: {sorted(b - a)}")


def test_the_two_paths_count_the_unrecognised_bucket_the_same(both_paths):
    db = both_paths[0].get("tasks") or {}
    lg = both_paths[1].get("tasks") or {}
    assert db["AcmeSyncJob"]["other"] == lg["AcmeSyncJob"]["other"] == 1
    # 正对照:认得出的那个任务里这个桶必须是 0,否则上面那个 1 可能来自别处。
    assert db["AcmeReportJob"]["other"] == 0


def test_judged_denominator_accounts_for_every_bucket(both_paths):
    """health 的分母必须能被屏幕上的桶解释干净。

    judged = obs - neutral,而屏幕上显示的是 ok/bad/stale/other。
    四个桶加起来必须正好等于 judged,否则就有一批观察进了分母却不在任何一栏里 ——
    那正是 other 这个桶存在的理由。
    """
    for label, d in zip(("数据库", "日志"), both_paths):
        for name, c in (d.get("tasks") or {}).items():
            s = c["ok"] + c["bad"] + c["stale"] + c["other"]
            assert s == c["judged"], (
                f"{label}通路 {name}: ok+bad+stale+other={s} 但 judged={c['judged']} —— "
                f"有观察进了分母却不在任何一栏里")


def test_an_empty_run_event_table_says_why(both_paths):
    """库可用但 run_event 是空表时,必须说出原因。

    旁边 hist 那一半为同一情形写了三种具体原因,而 runs 的 reason 恒为 None ——
    页面上只剩一句没有原因的「无运行日志」,而那句话在
    「运行日志通道是关着的」「摄入器从没跑过」「摄入器停了」三种完全不同的处境下逐字相同。
    这三种要采取的行动互不相同,而屏幕上分不出是哪一种。

    (这条 fixture 只摄入了健康观察,没有摄入运行事件,所以 run_event 天然是空的 ——
     正是要测的那个状态,不需要额外制造。)
    """
    _hist, runs, reason = S.load_from_db()
    assert not reason
    assert runs["available"] is False, "run_event 是空的,available 不该为真"
    assert runs["reason"], "库里没有运行事件,却没有给出任何原因"
    assert "摄入" in runs["reason"] or "通道" in runs["reason"], runs["reason"]


def test_a_populated_run_event_table_gives_no_reason(both_paths, tmp_path):
    """负对照:有运行事件时不许报原因。见谁都叫的字段会被无视。"""
    import console_store as CS
    con, st = CS.connect_ro()
    assert not st
    con.close()
    # 直接往库里塞一条运行事件,再重新读
    import sqlite3
    p, _ = CS.resolve_db()
    w = sqlite3.connect(str(p))
    w.execute("INSERT OR IGNORE INTO run_event"
              "(log_epoch,record_id,task,event_id,ts,day,hour,rc_raw,rc_norm) "
              "VALUES(1,7001,'AcmeSyncJob',201,'2026-09-01 03:00:00','2026-09-01',3,'0',0)")
    w.commit()
    w.close()
    _hist, runs, reason = S.load_from_db()
    assert not reason
    assert runs["available"] is True
    assert runs["reason"] is None, runs["reason"]


# --------------------------------------------------------------------------- 顶层字段
# 上面几条比的是**每任务**的字段。顶层那一层一直没人比,而页面从顶层读的键
# 一样多:available / reason / days / caveat / matched / source / lastIngest / ingest。
# 实测:给数据库通路加 ingest 判定、日志通路不加,上面五条用例全绿 ——
# 于是回落一次,顶栏那段判定代码什么都不画,而那和「摄入器好好的」在屏幕上一样。
TOP_REQUIRED = {"available", "reason", "days", "matched", "source", "caveat",
                "lastIngest", "ingest"}


def test_both_paths_expose_the_same_top_level_field_set(both_paths):
    a, b = set(both_paths[0]), set(both_paths[1])
    assert a == b, (f"只在数据库通路里: {sorted(a - b)};"
                    f" 只在日志通路里: {sorted(b - a)}")


def test_both_paths_carry_every_top_level_key_the_page_reads(both_paths):
    """互相当判据还不够:两边**同时**漏掉一个键时,上一条是绿的。"""
    for label, d in (("数据库", both_paths[0]), ("日志", both_paths[1])):
        missing = TOP_REQUIRED - set(d)
        assert not missing, f"{label}通路顶层少了: {sorted(missing)}"


def test_the_fallback_path_says_the_ingester_does_not_apply(both_paths):
    """回落通路不经过摄入器 —— 但「不适用」和「键不在」不能长得一样。

    也不能谎称 ok:那会让人以为摄入器在跑,而此刻页面上的数字根本不是它给的。
    """
    v = both_paths[1]["ingest"]
    assert v["state"] == "n/a", v
    assert v["why"], "n/a 也要说清楚为什么不适用"


def test_the_database_path_actually_judges(both_paths):
    """正对照:主通路给的必须是一个真判定,不是同一个 n/a。"""
    v = both_paths[0]["ingest"]
    assert v["state"] != "n/a", "主通路把摄入新鲜度也说成不适用,那这道闸没在判任何东西"
    assert v["state"] in {"ok", "stale", "loss", "failed", "never", "unknown"}, v
