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

CLEAN, DIRTY, UNPUSHED, DETACHED, ERROR = "clean", "dirty", "unpushed", "detached", "error"

# 严重度序。unpushed 排在 dirty 前面:脏文件你自己知道,没推的提交没人会告诉你。
_SEV = {CLEAN: 0, DETACHED: 1, DIRTY: 2, UNPUSHED: 3, ERROR: 4}


def _git(repo: Path, *args: str, timeout: int = 20) -> tuple[int, str]:
    try:
        r = subprocess.run(("git", "-C", str(repo)) + args, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
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


def _load_visibility() -> dict:
    p = os.environ.get("TASK_CONSOLE_VISIBILITY")
    if not p:
        return {}
    try:
        return json.loads(Path(os.path.expanduser(p)).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


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
        return {"available": True, "root": str(base), "repos": [],
                "summary": {"total": 0, "counts": {}, "attention": 0},
                "note": "这个根目录下没有 git 仓"}

    vis = _load_visibility()
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
