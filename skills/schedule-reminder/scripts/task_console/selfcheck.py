"""自检:这台控制台此刻**真的**读到了哪些东西。

存在的理由只有一句:一个被喂了空的检查器打印的绿色,和一个真没查出问题的检查器打印的
绿色一模一样。页面上每一块面板都依赖若干个路径,而路径失效是静默的:环境变量没设、
文件被改名、目录被移走,面板会安安静静地显示一张空表,看起来像「没问题」。

所以这里把每一个来源单独列出来:环境变量叫什么、解析成了哪条路径、读到没有、多新。
读不到就是读不到,不折叠进任何汇总数字里。

判据分四态,刻意不合并:
  ok       读到了,而且(如果声明了保鲜期)还新鲜
  stale    读到了,但比声明的保鲜期旧
  missing  配了路径,但那条路径不存在或读不了
  unset    根本没配这个来源

missing 和 unset 分开是有代价的(多一个状态),但合并的代价更大:
「我没让它检查」和「我让它检查了而它坏了」是两件完全不同的事,后者需要有人立刻去看。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

OK, STALE, MISSING, UNSET = "ok", "stale", "missing", "unset"

# 每个来源:(键, 人话标题, 环境变量或 None, 类型, 保鲜期小时或 None, 是不是必需)
# 保鲜期为 None 表示这个来源不会过期(比如一个脚本文件),而不是「随便多旧都行」。
# 这几个目录**合法地可以是空的**:归档区在还没归档过东西时是空的,缓存在还没生成时是空的。
# 对它们套用「空目录 = 没配好」会让自检对着一个正常状态天天喊,
# 而一道对合法状态开火的闸门会被忽略,连带它真正该抓的那一类一起被忽略。
# (上线当天实测:它对着一个空的 skill 归档区报「读不到」。)
MAY_BE_EMPTY = frozenset(("skill_archive", "plugin_cache"))

SOURCES = (
    ("categories", "任务分类映射", "TASK_CONSOLE_CATEGORIES", "file", None, False),
    ("health", "健康声明清单", "TASK_CONSOLE_HEALTH", "file", None, True),
    ("allowlist", "备份 allow-list", "TASK_CONSOLE_ALLOWLIST", "file", None, False),
    ("history", "轮询观察日志", "TASK_CONSOLE_HISTORY", "file", 48.0, False),
    ("skills", "skill 目录", "TASK_CONSOLE_SKILLS", "dir", None, False),
    ("skill_archive", "skill 归档区", "TASK_CONSOLE_SKILL_ARCHIVE", "dir", None, False),
    ("memory", "记忆池", "TASK_CONSOLE_MEMORY", "dir", None, False),
    ("claude", "claude 可执行文件", "TASK_CONSOLE_CLAUDE", "file", None, False),
    ("repos", "仓库根目录", "TASK_CONSOLE_REPOS", "dir", None, False),
    ("visibility", "仓库可见性表", "TASK_CONSOLE_VISIBILITY", "file", None, False),
    ("archiver", "记忆归档器", "TASK_CONSOLE_MEMORY_ARCHIVER", "file", None, False),
    ("plugin_cache", "插件缓存", "TASK_CONSOLE_PLUGIN_CACHE", "dir", None, False),
    ("sessions", "会话转录目录", "TASK_CONSOLE_SESSIONS", "dir", None, False),
    ("convo_cache", "对话索引缓存", "TASK_CONSOLE_CONVO_CACHE", "file", None, False),
)

# 随包发布的文件。它们没有环境变量:缺了就是安装坏了,而不是没配。
BUNDLED = (
    ("collect", "采集脚本", "collect.ps1"),
    ("act", "动作脚本", "act.ps1"),
    ("runlog", "运行日志脚本", "runlog.ps1"),
    ("page", "页面", "console.html"),
    ("icon", "图标", "icon.svg"),
    # 外壳样式。缺了页面照样打开,但会退回成一堆没有布局的裸元素,
    # 而那看起来像「页面坏了」不像「少了一个文件」。
    ("tabler_css", "外壳样式", "vendor/tabler/tabler.min.css"),
    ("tabler_js", "外壳脚本", "vendor/tabler/tabler.min.js"),
)


# 一个空目录和一个「配了但同步没跑 / 挂载点没挂上 / 路径改过」在文件系统上长得一样,
# 而后者正是这个模块存在的理由所反对的那种绿色:自检整块打绿,对应面板显示 0 个条目。
# 空文件那一半原来已经判 missing,目录这一半却判 ok : 同一个「配了但里面什么都没有」
# 给了两种结论。
def _probe(path: Path, kind: str, max_age_h: float | None, now: float,
           may_be_empty: bool = False) -> tuple[str, dict]:
    info: dict = {"path": str(path)}
    try:
        st = os.stat(path)
    except OSError as e:
        info["why"] = e.__class__.__name__
        return MISSING, info
    is_dir = os.path.isdir(path)
    if kind == "dir" and not is_dir:
        info["why"] = "期望是目录,实际不是"
        return MISSING, info
    if kind == "file" and is_dir:
        info["why"] = "期望是文件,实际是目录"
        return MISSING, info
    info["mtime"] = st.st_mtime
    info["ageHours"] = round((now - st.st_mtime) / 3600.0, 2)
    if is_dir:
        try:
            info["entries"] = len(os.listdir(path))
        except OSError:
            info["entries"] = None
        # 一个空目录,和一个「配了但同步没跑 / 挂载点没挂上 / 路径改过」,在文件系统上
        # 长得一模一样,而后者正是这个模块存在的理由所反对的那种绿色:自检整块打绿,
        # 对应面板显示 0 个条目。空文件那一半原来已经判 missing,目录这一半却判 ok :
        # 同一个「配了但里面什么都没有」给了两种结论。
        # listdir 失败(None)是另一回事,单独说,别和「真的是空的」混成一句。
        if info["entries"] is None:
            info["why"] = "目录列不出来"
            return MISSING, info
        if info["entries"] == 0 and not may_be_empty:
            info["why"] = "目录是空的"
            return MISSING, info
    else:
        info["bytes"] = st.st_size
        # 一个零字节的必需文件读起来跟一个正常文件一样成功,但它什么都给不了。
        if st.st_size == 0:
            info["why"] = "文件是空的"
            return MISSING, info
    if max_age_h is not None and info["ageHours"] > max_age_h:
        info["why"] = f"比声明的保鲜期 {max_age_h}h 旧"
        return STALE, info
    return OK, info


def run(now: float | None = None, here: Path | None = None, env: dict | None = None) -> dict:
    """探一遍所有来源。now / here / env 可注入,测试靠它构造确定的场景。"""
    now = time.time() if now is None else now
    here = Path(__file__).resolve().parent if here is None else here
    env = os.environ if env is None else env

    rows = []
    for key, title, var, kind, max_age, required in SOURCES:
        raw = env.get(var) if var else None
        if not raw:
            rows.append({"key": key, "title": title, "env": var, "state": UNSET,
                         "required": required, "path": None,
                         "why": f"没有设 {var}"})
            continue
        state, info = _probe(Path(os.path.expanduser(raw)), kind, max_age, now,
                             may_be_empty=(key in MAY_BE_EMPTY))
        rows.append(dict({"key": key, "title": title, "env": var, "state": state,
                          "required": required}, **info))

    for key, title, fname in BUNDLED:
        state, info = _probe(here / fname, "file", None, now)
        rows.append(dict({"key": key, "title": title, "env": None, "state": state,
                          "required": True}, **info))

    counts: dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    # 整体判定只看两件事:有没有 missing,以及必需的来源是不是都 ok。
    # unset 不算失败(有些来源本来就是可选的),但它也绝不算通过:它单独列在计数里。
    broken = [r["key"] for r in rows if r["state"] == MISSING]
    required_bad = [r["key"] for r in rows
                    if r["required"] and r["state"] != OK]
    return {
        "generated": now,
        "rows": rows,
        "counts": counts,
        "broken": broken,
        "requiredBad": required_bad,
        "ok": not broken and not required_bad,
        # 探到的比例。它不是健康度,是「这次自检真的看了多少东西」。
        "probed": sum(1 for r in rows if r["state"] in (OK, STALE)),
        "total": len(rows),
    }
