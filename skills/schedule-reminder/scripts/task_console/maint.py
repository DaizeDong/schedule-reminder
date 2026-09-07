"""维护面板:skill 清单、记忆池用量、插件开关。读是读,写走一张闭合的动作表。

这个文件的危险在于它**移动目录**。所以每一个动作参数都过同一道闸:名字必须落在
一个窄字符集里,并且解析出的真实路径必须是配置根目录的**直接子项**。两条都过不了
就拒绝整个动作,而不是「清洗一下再执行」: 清洗过的参数看起来安全,但没人知道
清洗掉了什么。

所有路径都从环境变量来,没有默认值。没配就是「未检查」,页面照实说,不画一块空的绿板:
一个没被指向任何东西的面板打印的绿色,和一个真的什么问题都没有的面板一模一样。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# 名字闸。skill 目录名和插件名都过这一道。刻意不含路径分隔符、点号开头、空格。
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,79}$")

# 动作表是闭合的。加一个动作必须改这里,而不是拼一个字符串就能多出一个动词。
ACTIONS = ("skill.archive", "skill.restore", "plugin.enable", "plugin.disable",
           "repo.fetch", "clean.tempgit",
           "memory.archive", "memory.restore")

# MEMORY.md 的硬上限。超了尾部条目会在下次会话静默消失,所以这两个数字是护栏不是建议。
INDEX_HARD_LINES = 200
INDEX_HARD_BYTES = 25600


class Refused(Exception):
    """动作被闸门拒绝。

    带 code 不只是为了好看:多道闸互相兜底时,「抛了 Refused」证明不了是哪一道抛的。
    实测过:放开名字闸之后子项闸仍然挡住,去掉子项闸之后名字闸仍然挡住,于是两个
    只断言「抛异常」的测试在各自的投毒下都照样全绿。code 让每条用例钉住它自己那道闸。
    """

    def __init__(self, msg: str, code: str = "refused"):
        super().__init__(msg)
        self.code = code


def _root(env: str) -> Path | None:
    v = os.environ.get(env)
    return Path(os.path.expanduser(v)) if v else None


def _child(root: Path, name: str) -> Path:
    """把一个名字解析成 root 的直接子项,解析不出来就拒绝。

    用 realpath 比对而不是字符串前缀:前缀比对挡不住 '..',也挡不住一个指向别处的
    符号链接。这里要的是「解析完之后它的父目录确实是 root」。
    """
    if not SAFE_NAME.match(name or ""):
        raise Refused(f"名字不合法: {name!r}", "bad_name")
    p = (root / name)
    try:
        # 解析**完整目标**再取它的父目录,而不是解析 p.parent。
        # 后者会被 ".." 骗过去:Path(root/"..").parent 就是 root 本身,于是比对通过,
        # 而这个路径实际指向 root 的上一级。这个洞是测试在干净版本上直接抓出来的。
        target = os.path.realpath(p)
        parent = os.path.dirname(target)
    except OSError as e:
        raise Refused(f"解析不了路径: {e}", "unresolvable") from e
    if os.path.normcase(parent) != os.path.normcase(os.path.realpath(root)):
        raise Refused("目标不是配置根目录的直接子项", "not_child")
    return p


def _is_link(p: Path) -> bool:
    """junction 在 Windows 上不是 symlink,islink() 认不出来。真值是「realpath 变没变」。"""
    try:
        return os.path.normcase(os.path.realpath(p)) != os.path.normcase(os.path.abspath(p))
    except OSError:
        return False


def _desc_len(skill_dir: Path) -> int | None:
    f = skill_dir / "SKILL.md"
    try:
        txt = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.match(r"^---\r?\n(.*?)\r?\n---", txt, re.S)
    if not m:
        return None
    d = re.search(r"^description:\s*(.*)", m.group(1), re.M)
    return len(" ".join(d.group(1).split()).strip('"')) if d else 0


def read_skills() -> dict:
    root, arch = _root("TASK_CONSOLE_SKILLS"), _root("TASK_CONSOLE_SKILL_ARCHIVE")
    if not root or not root.is_dir():
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_SKILLS,skill 这一栏是「未检查」。"}
    live, budget = [], 0
    for name in sorted(os.listdir(root)):
        d = root / name
        if not (d / "SKILL.md").is_file():
            continue
        n = _desc_len(d) or 0
        budget += n + len(name)
        live.append({"name": name, "chars": n + len(name),
                     "linked": _is_link(d), "archived": False})
    archived = []
    if arch and arch.is_dir():
        for name in sorted(os.listdir(arch)):
            if (arch / name / "SKILL.md").is_file():
                archived.append({"name": name, "chars": 0, "linked": False, "archived": True})
    return {"available": True, "root": str(root), "archiveSet": bool(arch),
            "skills": live + archived, "liveCount": len(live),
            "archivedCount": len(archived), "budgetChars": budget}


def read_memory() -> dict:
    root = _root("TASK_CONSOLE_MEMORY")
    if not root or not root.is_dir():
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_MEMORY,记忆池这一栏是「未检查」。"}
    files, total = 0, 0
    for f in root.glob("*.md"):
        try:
            total += f.stat().st_size
        except OSError:
            continue
        files += 1
    idx = root / "MEMORY.md"
    lines = ibytes = None
    if idx.is_file():
        try:
            raw = idx.read_bytes()
            ibytes, lines = len(raw), raw.count(b"\n") + 1
        except OSError:
            pass
    archived = len(list((root / "archive").glob("*.md"))) if (root / "archive").is_dir() else 0
    return {"available": True, "root": str(root), "files": files, "bytes": total,
            "archived": archived, "indexLines": lines, "indexBytes": ibytes,
            "hardLines": INDEX_HARD_LINES, "hardBytes": INDEX_HARD_BYTES}


def _claude() -> str | None:
    v = os.environ.get("TASK_CONSOLE_CLAUDE")
    if v and os.path.isfile(os.path.expanduser(v)):
        return os.path.expanduser(v)
    return shutil.which("claude")


def read_plugins(timeout: int = 40) -> dict:
    exe = _claude()
    if not exe:
        return {"available": False, "reason": "找不到 claude 可执行文件,插件这一栏是「未检查」。"}
    try:
        r = subprocess.run([exe, "plugin", "list"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": f"读插件清单失败: {e.__class__.__name__}"}
    if r.returncode != 0:
        return {"available": False, "reason": f"claude plugin list 退出 {r.returncode}"}
    out, cur = [], None
    for line in (r.stdout or "").splitlines():
        t = line.strip()
        if t.startswith("❯"):
            cur = {"name": t.lstrip("❯ ").strip(), "enabled": None}
            out.append(cur)
        elif cur is not None and t.startswith("Status:"):
            cur["enabled"] = ("enabled" in t) or ("loaded" in t)
    return {"available": True, "plugins": out}


def read_all() -> dict:
    return {"skills": read_skills(), "memory": read_memory(), "plugins": read_plugins()}


def act(action: str, name: str) -> dict:
    if action not in ACTIONS:
        raise Refused(f"不在动作表里: {action!r}", "bad_action")
    if action.startswith("memory."):
        # 归档不在这里实现:它是一个三步的生命周期迁移,而那份逻辑已经存在于一个
        # 专门的脚本里。再写一份的结果一定是两份实现慢慢分叉,然后其中一份在没人
        # 注意的时候开始写出格式不对的索引。这里只负责把按钮转成对它的一次调用。
        import memops
        return memops.act(action.split(".", 1)[1], name)
    if action == "clean.tempgit":
        # 唯一一个删除动作。它不接受名字参数:删哪些由 sysinfo 自己按名字形状 + 年龄
        # 判定,而不是由调用方指定路径。一个接受路径的删除接口迟早会被喂进一条别的路径。
        import sysinfo
        return sysinfo.clean_temp_git()
    if action == "repo.fetch":
        # 只读的网络动作。push 永远不进这张表:它是对外动作,撤不回来,
        # 而一个能一键推送的按钮迟早会在没人看的时候被点到。
        import repos
        return repos.fetch(name)
    if action.startswith("skill."):
        root, arch = _root("TASK_CONSOLE_SKILLS"), _root("TASK_CONSOLE_SKILL_ARCHIVE")
        if not root or not arch:
            raise Refused("没有配 TASK_CONSOLE_SKILLS / TASK_CONSOLE_SKILL_ARCHIVE,拒绝移动任何目录", "no_config")
        arch.mkdir(parents=True, exist_ok=True)
        src_root, dst_root = (root, arch) if action == "skill.archive" else (arch, root)
        src, dst = _child(src_root, name), _child(dst_root, name)
        if not src.is_dir():
            raise Refused(f"源目录不存在: {name}", "missing_src")
        if dst.exists():
            raise Refused(f"目标已存在,不覆盖: {name}", "dst_exists")
        os.rename(src, dst)
        return {"ok": True, "moved": f"{src} -> {dst}"}

    exe = _claude()
    if not exe:
        raise Refused("找不到 claude 可执行文件", "no_claude")
    if not SAFE_NAME.match(name or ""):
        raise Refused(f"插件名不合法: {name!r}", "bad_name")
    verb = "enable" if action == "plugin.enable" else "disable"
    r = subprocess.run([exe, "plugin", verb, name], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    if r.returncode != 0:
        raise Refused(f"claude plugin {verb} 退出 {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}", "plugin_failed")
    return {"ok": True, "out": (r.stdout or "").strip()[:400]}


if __name__ == "__main__":
    print(json.dumps(read_all(), ensure_ascii=False, indent=2))
