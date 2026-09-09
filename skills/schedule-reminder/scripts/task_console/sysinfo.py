"""磁盘与残留:这台机器上正在悄悄长大的东西。

三类都有同一个性质:没有任何检查会因为它们变红,它们只是一直涨,直到某天磁盘满了
或者某个目录里堆了几千个孤儿文件才被发现。

目录体积是走文件系统数出来的,不是估的。但数一个几十万文件的目录会让面板转圈,所以
带一个文件数上限:**超了就在结果里说 partial,不假装数完了**。一个数了一半却报出
一个确定数字的体积,比不报还糟。

删除只针对一种东西:形如 temp_git_<数字>_<短串> 的废弃克隆暂存目录。三条闸同时成立
才动手:名字匹配、位于配置根目录的直接子项、且超过声明的年龄。少任何一条都拒绝。
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

# CLI 刷新插件市场时留下的克隆暂存目录。名字形状是固定的,所以可以精确匹配而不是模糊猜。
TEMP_GIT = re.compile(r"^temp_git_\d+_[A-Za-z0-9]+$")

# 数到这么多文件就停,并在结果里标 partial。
WALK_CAP = 60000

# 残留目录要老于这个小时数才允许删:一个正在进行中的克隆看起来和一个废弃的一模一样。
MIN_AGE_H = 2.0


def dir_size(path: Path, cap: int = WALK_CAP) -> dict:
    """目录体积。**任何一处没数进去都要让 partial 为真。**

    ⚠ 原来只有撞上 WALK_CAP 才置 partial,而这里有两处 `except OSError: continue`
    把整棵子树的扫描失败、和单个文件的 stat 失败,都无声丢掉,然后照样返回一个
    `partial: False` 的确定体积。本模块开头那句话说的就是这件事:
    **一个数了一半却报出一个确定数字的体积,比不报还糟。**

    真实触发路径不止「权限」这一种设想:Windows 上超过 MAX_PATH 的深路径
    (插件缓存里的 node_modules 之类)会让不带长路径前缀的 scandir 抛 OSError;
    而这个面板要谈的 temp_git_* 本身就是克隆暂存目录,并发刷新时目录中途被删,
    scandir 抛 FileNotFoundError —— 也就是说最容易在扫描中途消失的正是它要数的东西。

    界面已经准备好了:`sz()` 在 partial 时加一个 "+",旁边还有一行提示。
    缺的一直只是这一半的实现 —— 不变量写下了、字段建好了、UI 也画了,唯独没人置那个标志。
    """
    total = files = 0
    partial = False
    errors = 0
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    if files >= cap:
                        partial = True
                        stack.clear()
                        break
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            files += 1
                    except OSError:
                        errors += 1
                        continue
        except OSError:
            errors += 1
            continue
    return {"bytes": total, "files": files,
            "partial": partial or errors > 0, "errors": errors}


def disk(path: str = ".") -> dict:
    try:
        u = shutil.disk_usage(os.path.expanduser(path))
    except OSError as e:
        return {"available": False, "reason": f"{e.__class__.__name__}"}
    return {"available": True, "total": u.total, "free": u.free, "used": u.used,
            "usedPct": round(u.used / u.total * 100, 1) if u.total else None}


def temp_git_leftovers(root: Path, now: float, errors: list | None = None) -> list[dict]:
    """残留的克隆暂存目录。

    ⚠ 同一个形状:扫描失败时原来直接 `return out`(空列表),单个目录 stat 失败时 continue,
    于是「这里很干净」和「我根本没数成」都渲染成一个确定的 0 个残留。
    调用方传一个 errors 列表进来收账,收到东西就说自己数得不全。
    """
    out = []
    err = errors if errors is not None else []
    try:
        entries = list(os.scandir(root))
    except OSError as e:
        err.append(f"{e.__class__.__name__}: {root}")
        return out
    for e in entries:
        if not e.is_dir(follow_symlinks=False) or not TEMP_GIT.match(e.name):
            continue
        try:
            age = (now - e.stat().st_mtime) / 3600.0
        except OSError as ex:
            err.append(f"{ex.__class__.__name__}: {e.name}")
            continue
        out.append({"name": e.name, "ageHours": round(age, 1),
                    "deletable": age >= MIN_AGE_H})
    out.sort(key=lambda x: -x["ageHours"])
    return out


def read(now: float | None = None) -> dict:
    now = time.time() if now is None else now
    res: dict = {"disk": disk(os.path.expanduser("~"))}

    cache = os.environ.get("TASK_CONSOLE_PLUGIN_CACHE")
    if not cache:
        res["pluginCache"] = {"available": False,
                              "reason": "没有设 TASK_CONSOLE_PLUGIN_CACHE,这一栏是「未检查」。"}
    else:
        p = Path(os.path.expanduser(cache))
        if not p.is_dir():
            res["pluginCache"] = {"available": False, "reason": f"目录不存在: {p}"}
        else:
            # 残留数也要能说「我数得不全」。原来扫描失败直接返回空列表,
            # 于是「这里很干净」和「我根本没数成」都渲染成一个确定的 0 个残留。
            lerr: list[str] = []
            left = temp_git_leftovers(p, now, lerr)
            res["pluginCache"] = dict(
                {"available": True, "path": str(p), "leftovers": left,
                 "leftoverCount": len(left),
                 "leftoverPartial": bool(lerr),
                 "leftoverErrors": lerr[:5],
                 "deletableCount": sum(1 for x in left if x["deletable"])},
                **{"size": dir_size(p)})

    sess = os.environ.get("TASK_CONSOLE_SESSIONS")
    if not sess:
        res["sessions"] = {"available": False,
                           "reason": "没有设 TASK_CONSOLE_SESSIONS,这一栏是「未检查」。"}
    else:
        p = Path(os.path.expanduser(sess))
        if not p.is_dir():
            res["sessions"] = {"available": False, "reason": f"目录不存在: {p}"}
        else:
            res["sessions"] = {"available": True, "path": str(p), "size": dir_size(p)}
    return res


def _force_writable(func, path, exc):
    """rmtree 的错误回调:清掉只读位再重试一次。

    git 把 objects/ 下的文件建成只读,而 Windows 上删一个只读文件会直接 PermissionError。
    实测:42 个废弃目录里有一半是被这个挡住的,而它们看起来只是「跳过了」,
    没有任何东西说明原因。清位再重试,还失败就让它抛出去,由上层记成 skipped。
    """
    if isinstance(exc, PermissionError):
        try:
            os.chmod(path, 0o700)
            func(path)
            return
        except OSError:
            pass
    raise exc


def clean_temp_git(now: float | None = None) -> dict:
    """删掉废弃的克隆暂存目录。三条闸同时成立才动手。"""
    from maint import Refused
    now = time.time() if now is None else now
    cache = os.environ.get("TASK_CONSOLE_PLUGIN_CACHE")
    if not cache:
        raise Refused("没有配 TASK_CONSOLE_PLUGIN_CACHE,拒绝删除任何东西", "no_config")
    root = Path(os.path.expanduser(cache))
    if not root.is_dir():
        raise Refused(f"目录不存在: {root}", "missing_src")
    removed, freed, skipped = [], 0, []
    for item in temp_git_leftovers(root, now):
        if not item["deletable"]:
            # 太新的不动:一个正在进行中的克隆看起来和一个废弃的一模一样。
            skipped.append(item["name"])
            continue
        d = root / item["name"]
        # 再确认一次它确实是这个根目录的直接子项。名字闸已经挡掉了分隔符,
        # 但删除是不可逆的,这里多比一次的代价是零。
        if os.path.normcase(os.path.dirname(os.path.realpath(d))) != \
           os.path.normcase(os.path.realpath(root)):
            skipped.append(item["name"])
            continue
        sz = dir_size(d)["bytes"]
        try:
            shutil.rmtree(d, onexc=_force_writable)
        except OSError as e:
            skipped.append(f"{item['name']} ({e.__class__.__name__})")
            continue
        removed.append(item["name"])
        freed += sz
    return {"ok": True, "removed": len(removed), "freedBytes": freed,
            "skipped": len(skipped), "names": removed[:20]}
