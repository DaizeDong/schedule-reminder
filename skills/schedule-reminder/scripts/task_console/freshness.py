"""五态新鲜度:声明 + 产物 mtime + 调度器读数 -> 每个任务此刻是什么状态。

一个匹配过宽的检查器,和一个真的查到了东西的检查器,输出长得一模一样。
这个文件里每一处「判绿」都必须是挣来的,拿不到证据时输出 unknown 而不是 up。

状态是纯函数(抄 healthchecks 的 get_status):**只存时间戳,状态在读的时候现算**。
没有会卡住的 status 字段,没有对账任务,不会出现「库里写着 down 但它其实早恢复了」。
所以本模块不落任何盘,evaluate() 的输出是一次性的快照。

为什么退出码不能单独当判据:Windows 的 LastTaskResult 是 HRESULT 不是 exit code。
0x41301 是「正在运行」、0x41303 是「从未运行过」,两者都不是失败。按「非零即红」去筛,
一批正在运行的任务会被当成失败,而它们好好的。

为什么产物也不能单独当判据:.log 型的产物通常在崩溃路径上照写,
所以「产物新鲜」默认**不能**洗掉一个坏退出码:只有显式声明了
artifact_written_only_on_success 的产物才有这个资格。
"""

from __future__ import annotations

import os
from pathlib import Path

# HRESULT 状态码,不是退出码。
RC_NOT_RUN = 0x41303
RC_RUNNING = 0x41301

# 五个终态 + 一个「没查成」。unknown 与 up 永远是两件事。
UP, GRACE, DOWN, NEVER, RUNNING, PAUSED, UNKNOWN = (
    "up", "grace", "down", "never", "running", "paused", "unknown")

# 严重度序,worst_of 用。unknown 排在 up 之后:它不是好消息,但也不是失败。
_SEVERITY = {UP: 0, RUNNING: 0, PAUSED: 1, UNKNOWN: 2, GRACE: 3, NEVER: 4, DOWN: 5}

# 宽限期默认取周期的两成,但至少一小时:一个 30 分钟周期的任务,
# 六分钟的宽限期只会制造抖动。
# 允许的时钟误差。小于这个值的负龄当成 0(刚写完的产物 mtime 比 now 略大是正常的,
# 文件系统时间戳精度和写入顺序都会造成几秒的负数),超过就当作时钟出了问题。
_FUTURE_SLACK_H = 0.1

_GRACE_RATIO = 0.2
_GRACE_MIN_H = 1.0


def worst_of(*states: str) -> str:
    return max(states, key=lambda s: _SEVERITY.get(s, 0))


def _ok_codes(decl: dict) -> set[int]:
    """两个键名在实际的清单里都出现过(ok_codes / ok_exit_codes),都认。
    一个只认其中一个名字的读取器会把另一批任务的声明静默丢掉,然后天天误报。"""
    out = {0}
    for key in ("ok_codes", "ok_exit_codes"):
        for v in decl.get(key) or ():
            try:
                out.add(int(v))
            except (TypeError, ValueError):
                pass
    return out


def _age_verdict(age_h: float | None, limit_h: float | None, grace_h: float | None) -> str:
    if age_h is None or limit_h is None:
        return UNKNOWN
    if age_h <= limit_h:
        return UP
    if grace_h is None:
        grace_h = max(limit_h * _GRACE_RATIO, _GRACE_MIN_H)
    return GRACE if age_h <= limit_h + grace_h else DOWN


def newest_mtime(path: str, stat=os.stat, listdir=os.listdir, isdir=os.path.isdir):
    """产物可以是一个文件,也可以是一个目录(比如一个按年份分目录的归档)。
    目录取里面最新那个文件的 mtime;空目录返回 None,**不是 0**:0 会被读成 1970 年,
    那是一个看起来很确定的错误答案。

    读不到就返回 None 并附上原因。返回 (mtime, reason)。"""
    p = os.path.expanduser(path)
    try:
        if isdir(p):
            best = None
            for name in listdir(p):
                try:
                    m = stat(os.path.join(p, name)).st_mtime
                except OSError:
                    continue
                if best is None or m > best:
                    best = m
            return (best, None) if best is not None else (None, "产物目录是空的")
        return stat(p).st_mtime, None
    except OSError as e:
        return None, f"读不到产物: {e.__class__.__name__}"


def evaluate(decls, rows, now, mtime_of=None):
    """decls: task-health.json 的 tasks 列表。rows: {name: {...}} 调度器读数。
    now: unix 秒。mtime_of: (path) -> (mtime|None, reason|None),测试从这里注入。

    返回 {"tasks": [...], "summary": {...}}。summary 里 coverage 是**判据本身的体检**:
    一个被喂了空的检查器打印的绿色,和一个真没查出问题的检查器打印的绿色一模一样,
    所以覆盖率跌破阈值本身就该是红的。"""
    mtime_of = mtime_of or newest_mtime
    out = []
    for decl in decls:
        name = decl.get("name")
        if not name:
            continue
        row = rows.get(name) or {}
        reasons = []
        state = None

        # 1) 调度器自己就能回答的三种,优先,且**不看产物**。
        #    一个刚被停用的任务,产物当然是陈的,那不叫故障。
        if row.get("state") == "Disabled":
            state = PAUSED
            reasons.append("人为停用")
        else:
            rc = row.get("last_rc")
            if rc == RC_RUNNING:
                state, reasons = RUNNING, ["正在运行"]
            elif rc == RC_NOT_RUN or not row:
                if not row:
                    state, reasons = UNKNOWN, ["调度器里没有这个任务"]
                else:
                    state, reasons = NEVER, ["从未运行过"]

        if state is None:
            rc = row.get("last_rc")
            ok = _ok_codes(decl)
            rc_bad = rc is not None and rc not in ok

            art = decl.get("artifact")
            art_v, art_age = UNKNOWN, None
            if art:
                m, why = mtime_of(art)
                if m is None:
                    reasons.append(why or "产物不可读")
                else:
                    art_age = (now - m) / 3600.0
                    if art_age < -_FUTURE_SLACK_H:
                        # 产物的时间戳在未来。原因通常是系统时钟被回拨,或者产物从别的机器
                        # 拷回来带着未来的 mtime。负龄会让 _age_verdict 恒判新鲜,
                        # 于是相关任务在时钟追上之前一律绿着,**即使它们已经完全停跑** :
                        # 一个会持续数小时到数天的假绿,而唯一线索是理由里一个负数。
                        # 查不成就说查不成,别拿一个算不出意义的数去判绿。
                        reasons.append(f"产物时间戳在未来 {abs(art_age):.1f}h(时钟回拨?)")
                        art_v = UNKNOWN
                    else:
                        art_v = _age_verdict(art_age, decl.get("artifact_max_age_hours"),
                                             decl.get("grace_hours"))
            else:
                reasons.append("没有声明产物,只能看退出码")

            run_v = UNKNOWN
            if row.get("last_run"):
                run_v = _age_verdict((now - row["last_run"]) / 3600.0,
                                     decl.get("max_age_hours"), decl.get("grace_hours"))

            # 产物新鲜能不能洗掉一个坏退出码?默认不能。
            # .log 型产物通常在崩溃路径上照写,所以只有显式声明
            # artifact_written_only_on_success 的才有这个资格。
            clears = bool(decl.get("artifact_written_only_on_success")) and art_v == UP
            if decl.get("exit_code_is_authoritative"):
                clears = False

            if rc_bad and not clears:
                state = DOWN
                reasons.append(f"退出码 {rc} 不在允许集 {sorted(ok)}")
            else:
                # 产物声明了「证明不了成功」时,它只能抓「彻底不跑了」,不许覆盖退出码。
                if decl.get("artifact_cannot_prove_success") and art_v == UP:
                    state = worst_of(run_v, UNKNOWN if rc is None else UP)
                    reasons.append("产物只能证明它还在跑,证明不了跑对了")
                else:
                    state = worst_of(art_v, run_v) if art else run_v
                if art_age is not None:
                    reasons.append(f"产物 {art_age:.1f}h 前更新")

        missed = row.get("missed_runs")
        if missed:
            reasons.append(f"漏跑 {missed} 次")
            # 只有「本来就该在跑」的任务,漏跑才是信号。一个人为停用的任务当然会一直漏跑,
            # 停几周就能累出几十次,把它升级成告警只会制造一个
            # 永远亮着、因而没人再看的红点。从未跑过和正在跑也同理。
            if state not in (PAUSED, NEVER, RUNNING, UNKNOWN):
                state = worst_of(state, GRACE)

        out.append({
            "name": name,
            "label": decl.get("label") or name,
            "state": state,
            "reasons": reasons,
            "last_run": row.get("last_run"),
            "next_run": row.get("next_run"),
            "last_rc": row.get("last_rc"),
            "missed_runs": missed,
            "artifact": decl.get("artifact"),
            "artifact_max_age_hours": decl.get("artifact_max_age_hours"),
            "cannot_prove_success": bool(decl.get("artifact_cannot_prove_success")),
        })

    counts = {}
    for t in out:
        counts[t["state"]] = counts.get(t["state"], 0) + 1
    judged = sum(v for k, v in counts.items() if k != UNKNOWN)
    total = len(out)
    return {
        "tasks": sorted(out, key=lambda t: (-_SEVERITY.get(t["state"], 0), t["name"])),
        "summary": {
            "total": total,
            "counts": counts,
            "judged": judged,
            # 覆盖率不是装饰:它是「这个检查器有没有真的查到东西」的体检。
            "coverage": round(judged / total, 4) if total else 0.0,
            "bad": counts.get(DOWN, 0) + counts.get(NEVER, 0),
            # 「要人管」这个集合由这里单点决定,页面三处(指标格、侧栏徽章、清单)
            # 一律读 attention,不在 JS 里各写一遍状态集合。
            #
            # 之前 bad 不含 UNKNOWN 而前端清单含,于是同一屏上出现三个数字:
            # 大字格 0、灯板下面写「1 条明细在上面」、清单里真有 1 行。
            # UNKNOWN 恰恰是这个项目最在意的那一类(**没查成**),它在最显眼的那一格里
            # 被静默吃掉、显示成绿色的零。
            #
            # bad 保留原义(判成坏的)并继续被别处使用;attention 是「要人管的」,
            # 两个名字分别说清自己是什么,而不是让一个名字承担两种含义。
            "attention": counts.get(DOWN, 0) + counts.get(NEVER, 0) + counts.get(UNKNOWN, 0),
            "attentionStates": [DOWN, NEVER, UNKNOWN],
        },
    }
