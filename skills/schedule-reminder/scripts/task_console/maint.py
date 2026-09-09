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

# 在 pythonw(GUI 子系统)下,每个控制台子程序都要新分配一个控制台。那次分配很慢,
# 而且并发时根本不成立:实测同一条 git 命令,普通 python 下几毫秒,pythonw 下单次
# 4.5 秒,四个并发全部 15 秒超时。加上这个标志之后单次降到 0.08 秒。
#
# 这个坑只在生产形态下出现,而开发期测试都是用普通 python 跑的:探针必须复现真实的
# 调用形状,否则测的是另一个程序。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# 名字闸。skill 目录名和插件名都过这一道。刻意不含路径分隔符、点号开头、空格。
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,79}$")

# 动作表是闭合的。加一个动作必须改这里,而不是拼一个字符串就能多出一个动词。
ACTIONS = ("skill.archive", "skill.restore", "plugin.enable", "plugin.disable",
           "repo.fetch", "clean.tempgit",
           "memory.archive", "memory.restore",
           "task.retire")

# MEMORY.md 的硬上限。超了尾部条目会在下次会话静默消失,所以这两个数字是护栏不是建议。
# ⚠ 这两个数以前在 maint 和 memops 里**各存了一份**,而两个模块都把它们下发给页面。
# 两份手写的同一个数,没有任何东西对账 —— 改一处而另一处照旧,页面上就会出现
# 两个都自称权威的百分比,而它们的分母不同。
# 现在从 memops 借,那边才是回答记忆池问题的那个模块。
from memops import INDEX_HARD_BYTES, INDEX_HARD_LINES  # noqa: E402,F401


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

    比对的是**解析后的父目录**,不是字符串前缀 —— 前缀比对挡不住 '..'。

    ⚠ 用 abspath 不用 realpath,而且**刻意不解析最后一段**(理由见下面的行内注释:
    本机 skill 是 junction 部署的,realpath 会跟到 junction 的目标上,
    于是每个 linked skill 的归档按钮都会必然失败)。
    代价要说清楚:**这条路因此挡不住「最后一段本身是一个指向别处的链接」**。
    这个 docstring 以前写的是「用 realpath 比对…挡得住一个指向别处的符号链接」——
    那是它正下方的实现按设计**不做**的一件事。一个承诺了实现没做的防护的文档,
    比没有文档更糟:它会让下一个人不再去加那道防护。
    """
    if not SAFE_NAME.match(name or ""):
        raise Refused(f"名字不合法: {name!r}", "bad_name")
    p = (root / name)
    try:
        # 用 abspath 而不是 realpath:abspath 会把 ".." 按字面消掉,
        # 所以 Path(root/"..") 的父目录变成 root 的**上上级**、比对失败 :
        # 那个洞(Path(root/"..").parent 就是 root 本身)照样堵着,
        # 而这个洞正是测试在干净版本上抓出来的,不能因为这次改动重新打开。
        #
        # ⚠ 但**不能解析最后一段**。本机的 skill 是 junction 部署的
        # (skills/<name> 指向别处的仓库),realpath 会跟过去,于是解析后的父目录是
        # junction 目标那边的目录,和 root 对不上。后果是:skill 面板上每个 linked 的
        # 条目都挂着一个点了必然失败的归档按钮,而失败信息是一句听起来像路径穿越攻击的
        # 「目标不是配置根目录的直接子项」,把排查方向整个带偏。
        # 这里要判的是「这个**名字**确实挂在 root 下」,不是「它指向哪里」;
        # 指向哪里是 junction 的自由,不是越界。
        parent = os.path.dirname(os.path.abspath(p))
        root_abs = os.path.abspath(root)
    except OSError as e:
        raise Refused(f"解析不了路径: {e}", "unresolvable") from e
    if os.path.normcase(parent) != os.path.normcase(root_abs):
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
    # 「没设」和「设了但那条路径不在」是两件事。合并之后方向刚好是最误导的那一种:
    # 目录被移走或改名时,页面言之凿凿地说环境变量没设,而它设了 ——
    # **这是把「我让它检查了而它坏了」报成了「我没让它检查」**,人会照着这句去检查一个
    # 没有问题的地方。memops.py 早就为同一件事拆开过两个分支并写下这段理由,
    # 这里 2026-09-09 才跟上(下面两个分支就是修好之后的形态)。
    # ⚠ 上一版注释的末句写的是「而这里**一直是**被推翻的那个旧形态」——
    # 那是我在同一次改动里、紧贴着已经改好的代码写下的一句现在时描述。
    # **一段描述自己正下方代码的注释写错时,读的人会先信注释。**
    if not root:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_SKILLS,skill 这一栏是「未检查」。"}
    if not root.is_dir():
        return {"available": False,
                "reason": f"skill 目录不存在或读不了: {root}"}
    live, budget, unreadable = [], 0, 0
    for name in sorted(os.listdir(root)):
        d = root / name
        if not (d / "SKILL.md").is_file():
            continue
        # _desc_len 返回 None 表示「读不出来」(文件读不了、frontmatter 写坏),
        # 返回 0 表示「真的没写描述」。`or 0` 把这两件事压成同一个数,
        # 于是那份 skill 贡献的字符被当成 0,预算条读数偏低、颜色偏绿,
        # 而真实预算已经更接近上限 : **一个被喂了空的度量打印的绿色,
        # 和一个真没超标的度量打印的绿色一模一样**,而这条预算条正是用来防
        # 「超了之后尾部条目的描述会在下一次会话里静默消失」的。
        raw = _desc_len(d)
        n = raw or 0
        if raw is None:
            unreadable += 1
        budget += n + len(name)
        live.append({"name": name, "chars": n + len(name),
                     "descUnreadable": raw is None,
                     "linked": _is_link(d), "archived": False})
    archived = []
    if arch and arch.is_dir():
        for name in sorted(os.listdir(arch)):
            if (arch / name / "SKILL.md").is_file():
                archived.append({"name": name, "chars": 0, "linked": False, "archived": True})
    return {"available": True, "root": str(root), "archiveSet": bool(arch),
            "skills": live + archived, "liveCount": len(live),
            "archivedCount": len(archived), "budgetChars": budget,
            # 单独报,不并进 0:预算条旁边要能看出「这个数字是不完整的」。
            "descUnreadable": unreadable}


def read_memory() -> dict:
    """记忆池的粗略计数。

    ⚠ 这个函数产出的东西**页面上没有任何读取点**:记忆池那一屏读的是 `/api/mem`
    (memops.read),而它算的是同一批数字的另一份 —— 口径还不完全一样
    (这里数 `*.md` 的总字节,那边按热层/冷层分开数)。
    两份都在下发,谁也不说自己是哪一份。留着它是因为 `/api/maint` 的形状是对外契约,
    突然少一个键会让别的消费方安静地拿到 undefined;但**新的消费方一律该用 /api/mem**,
    而这里的数字只作为那一屏不可用时的粗略兜底。
    """
    root = _root("TASK_CONSOLE_MEMORY")
    if not root:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_MEMORY,记忆池这一栏是「未检查」。"}
    if not root.is_dir():
        return {"available": False,
                "reason": f"记忆池目录不存在或读不了: {root}"}
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
            "hardLines": INDEX_HARD_LINES, "hardBytes": INDEX_HARD_BYTES,
            # 说清自己不是权威。一个不标注口径的第二份数字,和一个错的数字
            # 在读的人那里代价一样:他得先花时间弄明白该信哪个。
            "authority": "/api/mem",
            "note": "粗略计数。记忆池的权威口径在 /api/mem(memops),两边算法不同。"}


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
                           encoding="utf-8", errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        return {"available": False, "reason": f"读插件清单失败: {e.__class__.__name__}"}
    if r.returncode != 0:
        return {"available": False, "reason": f"claude plugin list 退出 {r.returncode}"}
    out, cur = [], None
    lines = (r.stdout or "").splitlines()
    for line in lines:
        t = line.strip()
        if t.startswith("❯"):
            cur = {"name": t.lstrip("❯ ").strip(), "enabled": None}
            out.append(cur)
        elif cur is not None and t.startswith("Status:"):
            cur["enabled"] = ("enabled" in t) or ("loaded" in t)
    # 解析器是按 `claude plugin list` 当前的输出格式写的,而那个格式随时会变。
    # 格式一变就会出现两种都不报错的坏法:
    #   一是一条都没认出来 -> 显示「插件 0 共 0」,和一台真的没装插件的机器逐字相同;
    #   二是认出了名字但没认出 Status -> 每个插件的 enabled 是 None,页面把 None 当假,
    #      于是每一个都被画成「已禁用」并挂上一个「启用」按钮 : 一个看起来完全正常、
    #      可以点的界面,描述的却是一台并不存在的机器,而且它邀请人去启用一个已经启用的插件。
    # 两种都要说出来,而不是让「解析不出来」和「本来就是这样」长得一样。
    # ⚠ 这个条件以前写的是 `if lines and not out` —— 它只在「有输出但一条都没认出来」时
    # 开火,而 **stdout 整个为空时 `lines == []` 让它直接短路失效**,函数落到下面
    # `return {"available": True, "plugins": []}`,也就是上面注释点名要防的那件事:
    # 「插件 0 共 0」,和一台真的没装插件的机器逐字相同。
    # 一个 TUI 程序在非 TTY / 重定向下把渲染写去 stderr,或者输出被吞,就是这个形状,
    # 而它的退出码是 0。**判据要问的是「我认出了几条」,不是「有没有行」。**
    if not out:
        return {"available": False,
                "reason": ("插件清单一条都没解析出来"
                           + ("(claude plugin list 有输出但格式对不上)" if lines
                              else "(claude plugin list 没有任何 stdout)"))}
    unknown = [p["name"] for p in out if p["enabled"] is None]
    if unknown:
        return {"available": False, "plugins": out,
                "reason": f"{len(unknown)} 个插件读不出启用状态(输出格式可能变了)"}
    return {"available": True, "plugins": out}


def read_all() -> dict:
    return {"skills": read_skills(), "memory": read_memory(), "plugins": read_plugins()}


def act(action: str, name: str, arg: str | None = None) -> dict:
    if action not in ACTIONS:
        raise Refused(f"不在动作表里: {action!r}", "bad_action")
    if action == "task.retire":
        # 退役是唯一一个会同时改三处登记的动作。它必须带原因:一个没写原因的退役,
        # 半年后没人敢重启用也没人敢删。参数从这里透传下去,由 retire 自己校验。
        import retire
        return retire.apply(name, arg or "")
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
                       encoding="utf-8", errors="replace", timeout=90, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise Refused(f"claude plugin {verb} 退出 {r.returncode}: {(r.stderr or r.stdout or '').strip()[:200]}", "plugin_failed")
    return {"ok": True, "out": (r.stdout or "").strip()[:400]}


if __name__ == "__main__":
    print(json.dumps(read_all(), ensure_ascii=False, indent=2))
