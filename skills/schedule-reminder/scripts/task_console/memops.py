"""记忆池诊断,以及把归档动作接到既有的归档器上。

这里**不实现归档**。归档是一个三步的生命周期迁移(移文件、改 frontmatter 状态、
从热索引摘行、重建冷索引),而那个逻辑已经存在于一个专门的脚本里。再写一份的结果
一定是两份实现慢慢分叉,然后其中一份在没人注意的时候开始写出格式不对的索引。

所以这个模块只做两件事:算出**值得人看的诊断**,以及把按钮转成对那个脚本的一次调用。
脚本路径来自环境变量,没配就是「未检查」,不猜一个默认位置。

诊断里最值钱的三项:
  索引对照两条硬上限 : 超了尾部条目会在下一次会话里静默消失,没有任何告警
  孤儿链接 : [[名字]] 指向一个不存在的记忆,读到它的人会以为那里有东西
  不可达文件 : 池子里有这个文件,但热索引和冷索引都没有指向它的行
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


INDEX_HARD_LINES = 200
INDEX_HARD_BYTES = 25600

LINK = re.compile(r"\[\[([A-Za-z0-9_\-]+)\]\]")
INDEX_ENTRY = re.compile(r"^- \[[^\]]*\]\(([^)]+\.md)\)", re.M)


def _dirs():
    raw = os.environ.get("TASK_CONSOLE_MEMORY")
    if not raw:
        return None, None
    root = Path(os.path.expanduser(raw))
    return root, root / "archive"


FRONTMATTER_NAME = re.compile(r"\A---\r?\n(?:.*\r?\n)*?name:\s*\S", re.M)


def _is_memory(p: Path) -> bool:
    """按**形状**判断一个文件是不是记忆:开头是 frontmatter 且里面有 name。

    不按名字排除。池子里除了记忆还住着宪法、索引、模板这类文档,它们没有 frontmatter。
    用名字列表排除的话,每加一份新文档就要有人记得回来改那张表,而忘了改的表现是
    一条天天亮着的假告警,而天天喊狼来了的告警等于没有告警。

    顺带一个实测的副作用:宪法正文里写着占位符形式的双括号链接,把它当记忆扫会
    凭空报出一个孤儿链接。
    """
    try:
        head = p.read_text("utf-8", "replace")[:600]
    except OSError:
        return False
    return bool(FRONTMATTER_NAME.match(head))


def read() -> dict:
    root, arch = _dirs()
    # 「没设」和「设了但那条路径不在」必须分开说。合并之后,记忆池目录被移走或改名时,
    # 页面言之凿凿地说环境变量没设,而它设了 : **这是把「我让它检查了而它坏了」
    # 报成了「我没让它检查」,方向刚好是最误导的那一种**,人会照着这句去检查一个没问题的地方。
    if not root:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_MEMORY,记忆池诊断这一栏是「未检查」。"}
    if not root.is_dir():
        return {"available": False,
                "reason": f"记忆池目录不存在或读不了: {root}"}

    live = {p.stem: p for p in root.glob("*.md") if _is_memory(p)}
    cold = {p.stem: p for p in arch.glob("*.md") if _is_memory(p)} if arch.is_dir() else {}

    idx = root / "MEMORY.md"
    lines = ibytes = None
    referenced: set[str] = set()
    if idx.is_file():
        raw = idx.read_bytes()
        ibytes, lines = len(raw), raw.count(b"\n") + 1
        txt = raw.decode("utf-8", "replace")
        referenced = {Path(m).stem for m in INDEX_ENTRY.findall(txt)}
    cold_idx = arch / "MEMORY-archive.md" if arch else None
    if cold_idx and cold_idx.is_file():
        referenced |= {Path(m).stem
                       for m in INDEX_ENTRY.findall(cold_idx.read_text("utf-8", "replace"))}

    # 孤儿链接:指向一个既不在热层也不在冷层的名字。
    orphans: dict[str, list[str]] = {}
    for stem, p in list(live.items()) + list(cold.items()):
        try:
            body = p.read_text("utf-8", "replace")
        except OSError:
            continue
        for name in set(LINK.findall(body)):
            if name not in live and name not in cold:
                orphans.setdefault(name, []).append(stem)

    unreachable = sorted(s for s in live if s not in referenced)
    dangling = sorted(s for s in referenced if s not in live and s not in cold)

    sizes = []
    for stem, p in live.items():
        try:
            sizes.append({"slug": stem, "bytes": p.stat().st_size})
        except OSError:
            continue
    sizes.sort(key=lambda x: -x["bytes"])

    return {
        "available": True, "root": str(root),
        "live": len(live), "cold": len(cold),
        "indexLines": lines, "indexBytes": ibytes,
        "hardLines": INDEX_HARD_LINES, "hardBytes": INDEX_HARD_BYTES,
        "linePct": round(lines / INDEX_HARD_LINES * 100, 1) if lines else None,
        "bytePct": round(ibytes / INDEX_HARD_BYTES * 100, 1) if ibytes else None,
        # 三项都可能是空的,而空在这里是好消息 : 但它必须是数出来的空,不是没数。
        "orphanLinks": [{"name": k, "from": sorted(v)} for k, v in sorted(orphans.items())],
        "unreachable": unreachable,
        "danglingIndex": dangling,
        "biggest": sizes[:8],
        "archiverConfigured": bool(os.environ.get("TASK_CONSOLE_MEMORY_ARCHIVER")),
    }


def act(verb: str, slug: str) -> dict:
    """把按钮转成对既有归档器的一次调用。verb 只有 archive / restore 两种。"""
    from maint import Refused, SAFE_NAME
    script = os.environ.get("TASK_CONSOLE_MEMORY_ARCHIVER")
    root, _ = _dirs()
    if not script or not root:
        raise Refused("没有配 TASK_CONSOLE_MEMORY_ARCHIVER / TASK_CONSOLE_MEMORY", "no_config")
    sp = Path(os.path.expanduser(script))
    if not sp.is_file():
        raise Refused(f"归档器不存在: {sp}", "missing_src")
    if verb not in ("archive", "restore"):
        raise Refused(f"不支持的记忆动作: {verb!r}", "bad_action")
    if not SAFE_NAME.match(slug or ""):
        raise Refused(f"slug 不合法: {slug!r}", "bad_name")
    cmd = [sys.executable, str(sp), "--memory-dir", str(root), f"--{verb}", slug]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f"调不动归档器: {e.__class__.__name__}", "archiver_failed") from e
    if r.returncode != 0:
        raise Refused(f"归档器退出 {r.returncode}: "
                      f"{(r.stderr or r.stdout or '').strip()[:200]}", "archiver_failed")
    return {"ok": True, "out": (r.stdout or "").strip()[:400]}
