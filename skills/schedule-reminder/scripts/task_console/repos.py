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

# 仓库的类型。**全部从仓库自身的形状观察出来,没有任何一张仓名表。**
# 这不是洁癖:一份「哪个仓是什么、谁跟谁有关系」的清单就是舰队地图,
# 而这个文件在公开仓里。判据只认形状,于是它在任何人的机器上都成立,
# 也不会随着增删仓库而过期。
KIND_SKILL = "skill"          # 有 SKILL.md(自己的或 skills/*/ 下的)
KIND_COMPANION = "companion"  # origin 名是 <宿主>-config,而宿主也在这次扫描里
KIND_SHARED = "shared"        # 被这次扫描里别的仓当 submodule 引用
KIND_OTHER = "other"

# 组的显示顺序。伴生仓不单独成组:它跟在自己的宿主下面。
KIND_ORDER = (KIND_SKILL, KIND_SHARED, KIND_OTHER)


def _repo_name_from_url(url: str | None) -> str | None:
    """从 remote URL 取出裸仓名。

    形式有两种:`https://host/owner/name.git` 和 `git@alias:owner/name.git`。
    这段归一化和 guards/tools/datadir.py 的 `_proves_companion` 是同一套 ——
    伴生关系的**权威判据在那里**(origin 名等于 `<skill>-config`,或有 .companion 标记),
    这里只是照它的形状去认。
    ⚠ 不要在这里发明第二套判据。这个面板回答的是「这次扫描里看得见什么关系」,
    不回答「伴生仓在哪、对不对」—— 后者是 datadir 和 data_boundary 的职责,
    而同一个问题有两个都自称权威的答案,正是这个控制台反复在修的那类缺陷。
    """
    if not url:
        return None
    base = url.rstrip("/").rsplit("/", 1)[-1]
    if base.endswith(".git"):
        base = base[:-4]
    if ":" in base:
        base = base.rsplit(":", 1)[-1]
    return base or None


def _submodule_parents(repo: Path) -> list[str]:
    """这个仓把哪些仓当 submodule 用。读 .gitmodules,取每个 url 的裸仓名。

    读文件而不是跑 `git submodule`:后者要求子模块已经 checkout,
    而「声明了但没 checkout」正是这套闸门栽过的那个坑(目录存在且为空,
    git 找不到钩子就什么都不跑、退出 0)。声明本身才是关系的事实。
    """
    p = repo / ".gitmodules"
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if line.startswith("url"):
            _, _, v = line.partition("=")
            n = _repo_name_from_url(v.strip())
            if n:
                out.append(n)
    return out


def _has_skill_manifest(repo: Path) -> bool:
    """SKILL.md 在根上,或者在 skills/<任意一个>/ 下。两种布局真实存在,都要认。"""
    if (repo / "SKILL.md").is_file():
        return True
    skills = repo / "skills"
    try:
        return any((d / "SKILL.md").is_file() for d in skills.iterdir() if d.is_dir())
    except OSError:
        return False

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
                # 形状事实。类型和关系在 scan() 里定,因为那需要看到全部仓;
                # 这里只报「我这个目录长什么样」。
                remoteName=_repo_name_from_url(remote),
                hasSkillManifest=_has_skill_manifest(repo),
                usesShared=_submodule_parents(repo),
                ahead=ahead, behind=behind, dirty=dirty, remote=remote,
                lastCommit=last,
                ageDays=round((now - last) / 86400.0, 1) if last else None,
                visibility=_visibility(str(repo), vis_table),
                # 没有 upstream 时 ahead 是 None,不是 0。页面必须画成「未知」。
                unpushedKnown=ahead is not None)


def _classify(rows: list[dict]) -> None:
    """就地给每个仓定 kind 和关系。只用这次扫描里看得见的事实。

    三条判据互不重叠,优先级 companion > shared > skill > other:
      * origin 名是 `<X>-config` **而且 X 也在这次扫描里** -> X 的伴生仓。
        宿主不在这次扫描里的(比如一个独立的配置备份仓,去掉后缀后并没有那个仓),
        不算伴生 —— 它只是名字长得像。**判据是配对成功,不是名字后缀。**
      * 被别的仓在 .gitmodules 里引用 -> 共享组件。
      * 有 SKILL.md -> skill 仓。

    `companionInScan` 刻意只说「这次扫描里」:一个仓的伴生仓完全可以在扫描根之外
    (真实存在的形态),那时这里是 False,而它**不表示缺口**。
    缺口由 data_boundary 判,这里不抢那个答案。
    """
    by_name: dict[str, dict] = {}
    for r in rows:
        for key in (r.get("remoteName"), r.get("name")):
            if key:
                by_name.setdefault(key, r)

    shared: set[str] = set()
    for r in rows:
        for parent in r.get("usesShared") or []:
            if parent in by_name:
                shared.add(parent)

    for r in rows:
        rn = r.get("remoteName") or r.get("name") or ""
        host = None
        if rn.endswith("-config"):
            cand = rn[: -len("-config")]
            if cand in by_name and by_name[cand] is not r:
                host = by_name[cand]

        if host is not None:
            r["kind"] = KIND_COMPANION
            r["companionOf"] = host.get("name")
            host["companionInScan"] = r.get("name")
        elif rn in shared:
            r["kind"] = KIND_SHARED
        elif r.get("hasSkillManifest"):
            r["kind"] = KIND_SKILL
        else:
            r["kind"] = KIND_OTHER

    for r in rows:
        r.setdefault("companionInScan", None)


def _group_counts(rows: list[dict]) -> dict:
    """每个组标题下实际显示多少个仓。伴生仓归到它宿主所在的组。

    不变量:各组之和 == 仓总数。tests/test_repos.py 钉住了这一条。
    """
    host_kind = {r.get("name"): r.get("kind") for r in rows}
    counts = {k: 0 for k in KIND_ORDER}
    for r in rows:
        k = r.get("kind")
        if k == KIND_COMPANION:
            k = host_kind.get(r.get("companionOf")) or KIND_OTHER
        counts[k] = counts.get(k, 0) + 1
    return counts


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
                            "kinds": {k: 0 for k in (KIND_SKILL, KIND_SHARED,
                                                     KIND_COMPANION, KIND_OTHER)},
                            "kindOrder": list(KIND_ORDER),
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

    _classify(out)
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
            # 每组实际会显示多少个仓。页面用它画分组标题,而不是自己再数一遍 ——
            # 前端数一遍就是同一个事实的第二个来源。
            #
            # ⚠ 伴生仓算进**它宿主所在的那个组**,因为它就画在那里。
            # 按 kind 直接数的话,三个组标题加起来会比「共 N」少掉伴生仓的数目,
            # 而屏幕上没有任何一处解释那个差 —— 两个都自称权威的数字,
            # 读的人只能自己去猜哪个漏了什么。加起来等于总数,就不需要解释。
            "kinds": _group_counts(out),
            "kindOrder": list(KIND_ORDER),
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
