"""仓库面板:一眼看完一整片 git 仓的分支、脏文件、有没有没推上去的提交。

为什么值得单独一块:「有没有未推送的提交」这件事没有任何东西会主动告诉你。它不会让
任何检查变红,不会让任何任务失败,只会在某天换机器或者清目录的时候变成永久损失。

三条判据上的讲究:

**没有 upstream 时 ahead/behind 是 None,不是 0。** 一个从没设过上游的分支,和一个
和上游完全同步的分支,在「0/0」这个显示下长得一模一样,而前者的提交其实一个都没推出去。

**`.git` 是文件而不是目录,说明这是 submodule 或 worktree。** 这个区分是承重的:
向上找 `.git` 的代码会停在 submodule 上,把子模块当成独立仓来判断。

**读不了的仓要报错,不能跳过。** 一个悄悄跳过三个仓的扫描器,和一个扫完发现三个仓
都没问题的扫描器,输出的绿色一模一样。

网络动作只有 fetch,而且是显式触发的。push 绝不自动:它是对外动作,撤不回来。
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import os
import subprocess
import time
from pathlib import Path

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


CLEAN, DIRTY, UNPUSHED, DETACHED, ERROR = "clean", "dirty", "unpushed", "detached", "error"

# 严重度序。unpushed 排在 dirty 前面:脏文件你自己知道,没推的提交没人会告诉你。
_SEV = {CLEAN: 0, DETACHED: 1, DIRTY: 2, UNPUSHED: 3, ERROR: 4}


def _git(repo: Path, *args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(("git", "-C", str(repo)) + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        # 失败时要带上 stderr:git 把成功的输出写 stdout,把**失败的原因**写 stderr。
        # 原来只取 stdout,于是仓库面板上一个 error 行只会显示「git status 退出 128」、
        # fetch 失败只会显示「fetch 退出 128:」后面什么都没有 :
        # 真正的原因(认证失败、dubious ownership、远端不存在)读不到,
        # 排查只能到命令行重跑一遍,而那正是这块面板想省掉的事。
        if r.returncode != 0:
            return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
        return r.returncode, (r.stdout or "")
    except (OSError, subprocess.SubprocessError) as e:
        return 127, f"{e.__class__.__name__}: {e}"


def _visibility(path: str, table: dict) -> str | None:
    """可见性表由外部提供(仓外文件)。查不到就是 None,页面显示「未知」而不是猜 PRIVATE。"""
    if not table:
        return None
    key = os.path.normcase(os.path.abspath(path))
    for k, v in table.items():
        if os.path.normcase(os.path.abspath(os.path.expanduser(k))) == key:
            return v if isinstance(v, str) else (v or {}).get("visibility")
    return None


def scan_one(repo: Path, vis_table: dict, now: float) -> dict:
    d = {"name": repo.name, "path": str(repo)}
    dot = repo / ".git"
    d["nested"] = dot.is_file()          # submodule 或 worktree

    rc, out = _git(repo, "status", "--porcelain=v2", "--branch")
    if rc != 0:
        return dict(d, state=ERROR, why=out.strip()[:160] or f"git status 退出 {rc}")

    branch, ahead, behind, upstream, dirty = None, None, None, None, 0
    for line in out.splitlines():
        if line.startswith("# branch.head "):
            branch = line.split(" ", 2)[2]
        elif line.startswith("# branch.upstream "):
            upstream = line.split(" ", 2)[2]
        elif line.startswith("# branch.ab "):
            parts = line.split()
            try:
                ahead, behind = int(parts[2]), -int(parts[3])
            except (IndexError, ValueError):
                ahead = behind = None
        elif line and not line.startswith("#"):
            dirty += 1

    rc2, out2 = _git(repo, "log", "-1", "--format=%ct")
    last = None
    if rc2 == 0 and out2.strip().isdigit():
        last = int(out2.strip())

    rc3, out3 = _git(repo, "remote", "get-url", "origin")
    remote = out3.strip() if rc3 == 0 else None

    if branch in (None, "(detached)"):
        state = DETACHED
    elif ahead:
        state = UNPUSHED
    elif dirty:
        state = DIRTY
    else:
        state = CLEAN

    return dict(d, state=state, branch=branch, upstream=upstream,
                ahead=ahead, behind=behind, dirty=dirty, remote=remote,
                lastCommit=last,
                ageDays=round((now - last) / 86400.0, 1) if last else None,
                visibility=_visibility(str(repo), vis_table),
                # 没有 upstream 时 ahead 是 None,不是 0。页面必须画成「未知」。
                unpushedKnown=ahead is not None)


def _load_visibility() -> tuple[dict, str | None]:
    """可见性表,外加「为什么没有」。

    原来解析失败被吞成空表,于是所有仓的 PUB/PRI 标记一起消失,而那和「表里没登记这几个仓」
    长得一模一样。在这套体系里可见性正是判断一个仓能不能装真实数据的依据,
    **一个静默变空的可见性视图,比没有这个视图更危险**。
    自检只能证明这个文件存在且非空,证明不了它解析得出来。
    """
    p = os.environ.get("TASK_CONSOLE_VISIBILITY")
    if not p:
        return {}, None                      # 没配 = 没启用,不是故障
    try:
        return json.loads(Path(os.path.expanduser(p)).read_text(encoding="utf-8-sig")), None
    except OSError as e:
        return {}, f"可见性表读不到({e.__class__.__name__}),所有仓的公开/私有标记都不显示"
    except ValueError as e:
        return {}, f"可见性表解析失败({e.__class__.__name__}),所有仓的公开/私有标记都不显示"


def scan(root: str | None = None, now: float | None = None, workers: int = 10) -> dict:
    """扫一整片仓。串行扫 49 个仓要几十秒,所以并发;每个仓自己带超时,一个卡住的仓
    不能让整块面板转圈。"""
    now = time.time() if now is None else now
    raw = root if root is not None else os.environ.get("TASK_CONSOLE_REPOS")
    if not raw:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_REPOS,仓库这一栏是「未检查」。"}
    base = Path(os.path.expanduser(raw))
    if not base.is_dir():
        return {"available": False, "reason": f"仓库根目录不存在: {base}"}

    repos = [d for d in sorted(base.iterdir())
             if d.is_dir() and (d / ".git").exists()]
    if not repos:
        # summary 的形状必须和正常分支一致。少给几个键不会报错,只会让前端把
        # `undefined` 拼进副标题(实测:「无上游 undefined」)——
        # 一个 undefined 印在屏幕上比一个说不出来的空更糟,因为它看起来像一个值。
        return {"available": True, "root": str(base), "repos": [],
                "summary": {"total": 0, "counts": {}, "attention": 0,
                            "unknownUpstream": 0,
                            "attentionStates": ["unpushed", "dirty", "error"],
                            "visibilityReason": None},
                "note": "这个根目录下没有 git 仓"}

    vis, vis_reason = _load_visibility()
    out = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(scan_one, r, vis, now): r for r in repos}
        for f in cf.as_completed(futs):
            r = futs[f]
            try:
                out.append(f.result())
            except Exception as e:
                # 扫不动的仓要出现在列表里并且是红的。悄悄跳过等于把它算成没问题。
                out.append({"name": r.name, "path": str(r), "state": ERROR,
                            "why": f"{e.__class__.__name__}: {e}"})

    out.sort(key=lambda x: (-_SEV.get(x["state"], 0), x["name"]))
    counts: dict[str, int] = {}
    for x in out:
        counts[x["state"]] = counts.get(x["state"], 0) + 1
    return {
        "available": True, "root": str(base), "repos": out,
        "summary": {
            "total": len(out),
            "counts": counts,
            # 要人管的:没推的、脏的、扫不动的。detached 单独看,它常常是刻意的。
            "attention": counts.get(UNPUSHED, 0) + counts.get(DIRTY, 0) + counts.get(ERROR, 0),
            # 这个集合必须显式说出来,因为页面上有三处要用它。之前前端自己写了
            # `state !== "clean"`,于是一个游离 HEAD 的仓会让指标格显示绿色的 0、
            # 侧栏徽章不亮,而正下方的清单里有一行「仓库 · 游离 HEAD」 :
            # 后端明确决定「detached 不算要人管」,前端把这个决定推翻了一半。
            # 判定只能留一份,而这一份在这里。
            "attentionStates": [UNPUSHED, DIRTY, ERROR],
            # 可见性表读不出来时要说出来。原来它被吞成空表,于是所有仓的公开/私有标记
            # 一起消失,而那和「表里没登记这几个仓」长得一模一样。
            "visibilityReason": vis_reason,
            "unknownUpstream": sum(1 for x in out if x.get("unpushedKnown") is False),
        },
    }


def fetch(name: str) -> dict:
    """对一个仓跑 fetch。只读网络动作,不改工作树,不推任何东西。"""
    from maint import Refused, SAFE_NAME, _child   # 复用同一道参数闸,不另造一个
    raw = os.environ.get("TASK_CONSOLE_REPOS")
    if not raw:
        raise Refused("没有配 TASK_CONSOLE_REPOS", "no_config")
    if not SAFE_NAME.match(name or ""):
        raise Refused(f"仓名不合法: {name!r}", "bad_name")
    repo = _child(Path(os.path.expanduser(raw)), name)
    if not (repo / ".git").exists():
        raise Refused(f"不是 git 仓: {name}", "missing_src")
    rc, out = _git(repo, "fetch", "--prune", timeout=120)
    if rc != 0:
        raise Refused(f"fetch 退出 {rc}: {out.strip()[:200]}", "fetch_failed")
    return {"ok": True, "out": out.strip()[:400]}
