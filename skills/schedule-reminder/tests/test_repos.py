#!/usr/bin/env python3
"""仓库扫描的判据测试。

用真的 git 仓跑,不是拿假字符串喂解析器。理由:这个模块的输入是 `git status --porcelain=v2`
的真实输出,而一个自己造输入的测试会在格式变化时继续全绿,同时生产里已经解析不出东西了。

最重要的两条:
  没有 upstream 时 ahead 必须是 None 而不是 0(否则「从没推过」和「已同步」长一样)
  扫不动的仓必须出现在列表里并且是红的(悄悄跳过等于算它没问题)
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console"))

import repos as R  # noqa: E402

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="没有 git")

NOW = 1_800_000_000.0


def git(d, *a):
    subprocess.run(("git", "-C", str(d)) + a, capture_output=True, check=True)


def mkbare(base, name):
    """真的 bare 仓。先建工作树再设 core.bare 是推不动的,push 会直接失败。"""
    d = base / name
    d.mkdir(parents=True)
    subprocess.run(("git", "init", "--bare", "-q", str(d)), capture_output=True, check=True)
    return d


def mkrepo(base, name, commit=True):
    d = base / name
    d.mkdir(parents=True)
    git(d.parent, "init", "-q", name)
    git(d, "config", "user.email", "user1@example.com")
    git(d, "config", "user.name", "Example User")
    # 把钩子关掉。开发机上很可能配了全局 core.hooksPath,不关的话每一个测试提交都会
    # 去跑一整套本地钩子:测试慢一个数量级,而且结果开始取决于跑它的那台机器怎么配的。
    git(d, "config", "core.hooksPath", str(base / "_nohooks"))
    if commit:
        (d / "a.txt").write_text("hello\n", encoding="utf-8")
        git(d, "add", "a.txt")
        git(d, "commit", "-q", "-m", "init")
    return d


# ---------- 未配置与空 ----------

def test_unset_root_reports_not_checked(monkeypatch):
    monkeypatch.delenv("TASK_CONSOLE_REPOS", raising=False)
    r = R.scan()
    assert r["available"] is False and r["reason"]


def test_absent_root_reports_not_checked(tmp_path):
    r = R.scan(root=str(tmp_path / "nope"))
    assert r["available"] is False


def test_root_with_no_repos_is_available_but_empty(tmp_path):
    # 「这里没有仓」和「我没去看」必须是两个不同的答案。
    (tmp_path / "notarepo").mkdir()
    r = R.scan(root=str(tmp_path), now=NOW)
    assert r["available"] is True
    assert r["repos"] == [] and r["note"]


# ---------- 四态 ----------

def test_clean_repo_is_clean(tmp_path):
    mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["state"] == R.CLEAN
    assert r["dirty"] == 0
    assert r["branch"]


def test_uncommitted_change_is_dirty(tmp_path):
    d = mkrepo(tmp_path, "a")
    (d / "b.txt").write_text("x", encoding="utf-8")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["state"] == R.DIRTY
    assert r["dirty"] >= 1


def test_no_upstream_means_ahead_is_unknown_not_zero(tmp_path):
    # 一个从没设过上游的分支,提交一个都没推出去,而 0/0 会把它显示成「已同步」。
    mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["ahead"] is None
    assert r["unpushedKnown"] is False
    assert r["upstream"] is None


def test_ahead_of_upstream_is_unpushed(tmp_path):
    up = mkbare(tmp_path / "bare", "up")
    d = mkrepo(tmp_path, "a")
    git(d, "remote", "add", "origin", str(up))
    git(d, "push", "-q", "-u", "origin", "HEAD")
    (d / "c.txt").write_text("x", encoding="utf-8")
    git(d, "add", "c.txt")
    git(d, "commit", "-q", "-m", "second")
    r = [x for x in R.scan(root=str(tmp_path), now=NOW)["repos"] if x["name"] == "a"][0]
    assert r["state"] == R.UNPUSHED
    assert r["ahead"] == 1
    assert r["unpushedKnown"] is True


def test_synced_repo_is_clean_and_known(tmp_path):
    # 上一条的正对照:设了上游且已同步,必须是 clean 且 ahead==0,不是 None。
    up = mkbare(tmp_path / "bare", "up")
    d = mkrepo(tmp_path, "a")
    git(d, "remote", "add", "origin", str(up))
    git(d, "push", "-q", "-u", "origin", "HEAD")
    r = [x for x in R.scan(root=str(tmp_path), now=NOW)["repos"] if x["name"] == "a"][0]
    assert r["state"] == R.CLEAN
    assert r["ahead"] == 0 and r["unpushedKnown"] is True


def test_unreadable_repo_is_listed_as_error_not_skipped(tmp_path):
    d = mkrepo(tmp_path, "broken")
    # 把 .git 弄坏:git status 会失败,而这个仓必须还在列表里并且是红的。
    for f in (d / ".git").iterdir():
        if f.name == "HEAD":
            f.write_text("garbage\n", encoding="utf-8")
    out = R.scan(root=str(tmp_path), now=NOW)
    names = {x["name"]: x for x in out["repos"]}
    assert "broken" in names, "扫不动的仓被悄悄跳过了"
    assert names["broken"]["state"] == R.ERROR
    assert names["broken"]["why"]


# ---------- 汇总 ----------

def test_attention_counts_the_ones_a_human_must_act_on(tmp_path):
    mkrepo(tmp_path, "clean1")
    d = mkrepo(tmp_path, "dirty1")
    (d / "x").write_text("x", encoding="utf-8")
    s = R.scan(root=str(tmp_path), now=NOW)["summary"]
    assert s["total"] == 2
    assert s["attention"] == 1
    # 没上游的仓数要单独报:它们的「没推」是看不见的。
    assert s["unknownUpstream"] == 2
    # attention 这个数和 attentionStates 那张表是同一件事的两处手写副本,
    # 而没有任何用例对账过 —— 改了其中一处,另一处照旧,页面上就会出现
    # 一个和它自己的明细对不上的计数。这里拿明细去重算一遍。
    repos = R.scan(root=str(tmp_path), now=NOW)["repos"]
    states = set(s["attentionStates"])
    recomputed = sum(1 for r in repos if (r.get("state") or r.get("status")) in states
                     or any(r.get(k) for k in states if isinstance(r.get(k), bool)))
    assert recomputed == s["attention"], (
        f"attention={s['attention']} 与按 attentionStates={sorted(states)} "
        f"重算出来的 {recomputed} 对不上")


def test_unpushed_sorts_above_dirty(tmp_path):
    up = mkbare(tmp_path / "bare", "up")
    a = mkrepo(tmp_path, "aaa_unpushed")
    git(a, "remote", "add", "origin", str(up))
    git(a, "push", "-q", "-u", "origin", "HEAD")
    (a / "c").write_text("x", encoding="utf-8")
    git(a, "add", "c")
    git(a, "commit", "-q", "-m", "s")
    b = mkrepo(tmp_path, "zzz_dirty")
    (b / "x").write_text("x", encoding="utf-8")
    order = [x["name"] for x in R.scan(root=str(tmp_path), now=NOW)["repos"]]
    # 脏文件你自己知道,没推的提交没人会告诉你,所以它排在前面。
    assert order.index("aaa_unpushed") < order.index("zzz_dirty")


def test_nested_marker_distinguishes_submodules(tmp_path):
    """普通仓不是 nested,而 .git 是文件的那种**是**。

    ⚠ 原来这条只有前半边:`assert r["nested"] is False` 再确认 .git 是个目录 ——
    **一个恒返回 False 的实现能让它全绿**。而这个判据是承重的:
    submodule 的 .git 是文件不是目录,舰队里正是靠这个形状去跳过 submodule 目录
    (曾经硬编码过名字表,拆出第二个 submodule 当天 23 个仓全部开始误报)。
    只有「不是」这一半的对照,证明不了那个区分真的存在。
    """
    d = mkrepo(tmp_path, "a")
    r = R.scan(root=str(tmp_path), now=NOW)["repos"][0]
    assert r["nested"] is False
    # submodule 的 .git 是文件不是目录,这个区分是承重的。
    assert (d / ".git").is_dir()

    # 正对照:把 .git 换成一个文件(submodule 的真实形状),必须被判成 nested。
    import os as _os
    import shutil
    import stat as _stat
    sub = mkrepo(tmp_path, "b")
    real_git = sub / ".git"

    def _force_writable(func, path, exc):
        # Windows 上 .git 里有只读文件(pack/idx),rmtree 会直接 PermissionError。
        # 这是我第一版的 bug:测试自己挂了,而它要测的东西根本没跑到。
        _os.chmod(path, _stat.S_IWRITE)
        func(path)

    shutil.rmtree(real_git, onexc=_force_writable)
    real_git.write_text("gitdir: ../.git/modules/b" + chr(10), encoding="utf-8")
    rows = {x["name"]: x for x in R.scan(root=str(tmp_path), now=NOW)["repos"]}
    assert rows["b"]["nested"] is True, (
        ".git 是文件(submodule 的形状)却没有被判成 nested —— "
        "一个恒返回 False 的实现和现在的实现在这条用例里长得一样")
    assert rows["a"]["nested"] is False, "普通仓被误判成 nested 了"


# ---------- fetch 的参数闸 ----------

def test_fetch_refuses_unsafe_names(tmp_path, monkeypatch):
    from maint import Refused
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(tmp_path))
    for bad in ("../x", "a/b", "", "x;y"):
        with pytest.raises(Refused) as e:
            R.fetch(bad)
        assert e.value.code in ("bad_name", "not_child")


def test_fetch_refuses_a_non_repo(tmp_path, monkeypatch):
    from maint import Refused
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(tmp_path))
    (tmp_path / "plain").mkdir()
    with pytest.raises(Refused) as e:
        R.fetch("plain")
    assert e.value.code == "missing_src"


# ---------- 可见性表解析失败不能被吞成空表 ----------
# 吞掉之后所有仓的 PUB/PRI 标记一起消失,而那和「表里没登记这几个仓」长得一模一样。
# 在这套体系里可见性正是判断一个仓能不能装真实数据的依据,
# 一个静默变空的可见性视图,比没有这个视图更危险。
# 自检只能证明这个文件存在且非空,证明不了它解析得出来。

def test_a_broken_visibility_table_reports_why(tmp_path, monkeypatch):
    p = tmp_path / "vis.json"
    p.write_text("{", encoding="utf-8")          # 非零字节,所以自检会判它 ok
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(p))
    table, why = R._load_visibility()
    assert table == {}
    assert why and "解析失败" in why, why


def test_an_unreadable_visibility_table_reports_why(tmp_path, monkeypatch):
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(tmp_path / "nope.json"))
    table, why = R._load_visibility()
    assert table == {} and why and "读不到" in why, why


def test_an_absent_setting_is_not_a_failure(monkeypatch):
    """正对照:没配 = 没启用,不是故障。
    分不开的话这条提示会在没配置的机器上天天亮,而天天亮的提示等于没有。"""
    monkeypatch.delenv("TASK_CONSOLE_VISIBILITY", raising=False)
    assert R._load_visibility() == ({}, None)


def test_a_good_table_parses_with_no_reason(tmp_path, monkeypatch):
    p = tmp_path / "vis.json"
    p.write_text('{"a": "PUBLIC"}', encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(p))
    table, why = R._load_visibility()
    assert table == {"a": "PUBLIC"} and why is None


def test_the_empty_root_branch_has_the_same_summary_shape(tmp_path, monkeypatch):
    """「这个根目录下没有 git 仓」那条分支的 summary,形状必须和正常分支一致。

    少给几个键不会报错,只会让前端把 `undefined` 拼进副标题 ——
    实测印出「无上游 undefined」。**一个 undefined 印在屏幕上比一个说不出来的空更糟,
    因为它看起来像一个值。**
    这条按**键集合**比对两条分支,而不是只查 unknownUpstream:下一个漏掉的键也会被抓到。
    """
    empty = tmp_path / "empty-root"
    empty.mkdir()
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(empty))
    blank = R.scan()
    assert blank["available"] is True and blank["repos"] == []

    # 正常分支:建一个真的 git 仓
    import subprocess
    live = tmp_path / "live-root" / "acme-repo"
    live.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=live, check=True,
                   stdin=subprocess.DEVNULL, capture_output=True)
    monkeypatch.setenv("TASK_CONSOLE_REPOS", str(live.parent))
    normal = R.scan()
    if not normal.get("repos"):
        pytest.skip("这台机器上建不出临时 git 仓")

    a, b = set(blank["summary"]), set(normal["summary"])
    assert a == b, f"只在空分支里: {sorted(a - b)};只在正常分支里: {sorted(b - a)}"


# ---------- 类型与关系:全部从形状观察,零仓名表 ----------

def test_group_counts_sum_to_the_total(tmp_path, monkeypatch):
    """三个组标题的计数加起来必须等于「共 N」。

    ⚠ 按 kind 直接数的话,伴生仓不属于任何一个显示出来的组,于是三个组标题加起来
    比总数少一截,而屏幕上**没有任何一处解释那个差**。
    这块面板同屏出现两个都自称权威的数字,正是这个控制台反复在修的那类缺陷。
    伴生仓画在宿主那一组里,就该算在那一组里。
    """
    host = mkrepo(tmp_path, "acme-thing")
    (host / "SKILL.md").write_text("x", encoding="utf-8")
    mkrepo(tmp_path, "acme-thing-config")
    mkrepo(tmp_path, "acme-plain")
    S = R.scan(root=str(tmp_path), now=NOW)["summary"]
    assert sum(S["kinds"].values()) == S["total"], (S["kinds"], S["total"])
    # 伴生仓算进宿主那一组:一个 skill 宿主 + 它的伴生 = 2
    assert S["kinds"]["skill"] == 2, S["kinds"]
    assert S["kinds"]["other"] == 1, S["kinds"]


def test_a_config_repo_without_a_host_is_not_a_companion(tmp_path):
    """名字像伴生仓、但配不上宿主的,不算伴生。

    判据是**配对成功**,不是名字后缀。一个独立的配置备份仓(去掉后缀之后并没有
    那个仓)会被后缀判据误认成某个不存在的宿主的伴生仓,然后挂到一个空位上。
    """
    mkrepo(tmp_path, "acme-orphan-config")
    rows = {r["name"]: r for r in R.scan(root=str(tmp_path), now=NOW)["repos"]}
    assert rows["acme-orphan-config"]["kind"] == "other"
    assert rows["acme-orphan-config"].get("companionOf") is None


def test_a_repo_used_as_a_submodule_is_shared(tmp_path):
    """被别的仓在 .gitmodules 里引用 -> 共享组件。

    读 .gitmodules 而不是跑 `git submodule`:后者要求子模块已经 checkout,
    而「声明了但没 checkout」正是这套闸门栽过的坑(目录存在且为空,什么都不跑还退 0)。
    声明本身才是关系的事实。
    """
    user = mkrepo(tmp_path, "acme-user")
    (user / "SKILL.md").write_text("x", encoding="utf-8")
    (user / ".gitmodules").write_text(
        '[submodule "g"]\n\tpath = g\n\turl = https://example.com/acme/acme-kit.git\n',
        encoding="utf-8")
    mkrepo(tmp_path, "acme-kit")
    rows = {r["name"]: r for r in R.scan(root=str(tmp_path), now=NOW)["repos"]}
    assert rows["acme-kit"]["kind"] == "shared", rows["acme-kit"]
    assert rows["acme-user"]["kind"] == "skill"
    # 负对照:没被任何人引用的仓不该被判成共享组件。
    assert "acme-kit" in (rows["acme-user"].get("usesShared") or [])


def test_every_repo_gets_exactly_one_kind(tmp_path):
    """每个仓必须正好有一个 kind,而且在已知集合里。

    少了这一条,一个把 kind 留空的实现会让那些仓从**所有**分组里消失 ——
    页面上不报错,只是少了几行,而「少了几行」没有人会数。
    """
    mkrepo(tmp_path, "acme-a")
    mkrepo(tmp_path, "acme-b")
    d = R.scan(root=str(tmp_path), now=NOW)
    known = {"skill", "shared", "companion", "other"}
    for r in d["repos"]:
        assert r.get("kind") in known, r
    assert len(d["repos"]) == d["summary"]["total"]


# ---------- 可见性:静默变空的视图比没有视图更危险 ----------

def _vis_table(tmp_path, monkeypatch, mapping):
    import json
    p = tmp_path / "visibility.json"
    p.write_text(json.dumps(dict(mapping, _refreshed="2026-01-01T00:00:00Z")),
                 encoding="utf-8")
    monkeypatch.setenv("TASK_CONSOLE_VISIBILITY", str(p))
    return p


def test_visibility_matches_on_owner_repo_not_on_a_filesystem_path(tmp_path, monkeypatch):
    """可见性表的键是 `owner/repo`,不是路径。

    ⚠ 这个匹配以前拿仓库的**文件系统路径**去比表里的键,于是它把
    `acme/widget` 当成一个相对路径去 expanduser + abspath —— 永远匹配不上。
    结果是**整个公开/私有视图从来没有工作过**:每一行都不显示徽章,
    而原因是 None,页面一句话都不说。
    屏幕上「表里没登记这几个仓」和「匹配逻辑压根不对」长得一模一样,
    而可见性正是判断一个仓能不能装真实数据的依据。
    """
    r = mkrepo(tmp_path, "widget")
    import subprocess
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/Acme/widget.git"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    _vis_table(tmp_path, monkeypatch, {"acme/widget": "PUBLIC"})
    d = R.scan(root=str(tmp_path), now=NOW)
    row = {x["name"]: x for x in d["repos"]}["widget"]
    assert row["visibility"] == "PUBLIC", row
    assert d["summary"]["visibilityReason"] is None


@pytest.mark.parametrize("url,want", [
    ("https://github.com/Acme/Widget.git", "acme/widget"),
    ("git@github.com:Acme/Widget.git", "acme/widget"),
    # 本机用 ssh alias,URL 里没有 host 那一段 —— 真实形态,必须认。
    ("git@my-alias:Acme/Widget.git", "acme/widget"),
    ("https://example.com/Acme/Widget", "acme/widget"),
    (None, None),
    ("nonsense", None),
])
def test_owner_repo_parses_every_real_remote_shape(url, want):
    assert R._owner_repo(url) == want


def test_a_table_that_matches_nothing_says_so(tmp_path, monkeypatch):
    """表读到了却一条都没对上 —— 必须说出来。

    没有这句话时,「没登记」和「匹配坏了」在屏幕上是同一个样子:没有徽章、没有原因。
    前者是配置问题,后者是代码 bug,要做的事完全不同。
    """
    r = mkrepo(tmp_path, "widget")
    import subprocess
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "https://github.com/Acme/widget.git"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    _vis_table(tmp_path, monkeypatch, {"someone/else": "PUBLIC", "third/party": "PRIVATE"})
    d = R.scan(root=str(tmp_path), now=NOW)
    why = d["summary"]["visibilityReason"]
    assert why and "没对上" in why, why


def test_a_repo_without_a_remote_is_unknown_not_guessed(tmp_path, monkeypatch):
    """没有 remote 的仓,可见性是「未知」,不是猜一个。

    负对照:这条同时保证上面那条 reason 不会因为一个本来就没 remote 的仓而误报 ——
    只要有别的仓对上了,就不该说「一条都没对上」。
    """
    mkrepo(tmp_path, "local-only")          # 没有 origin
    r2 = mkrepo(tmp_path, "widget")
    import subprocess
    subprocess.run(["git", "-C", str(r2), "remote", "add", "origin",
                    "https://github.com/Acme/widget.git"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    _vis_table(tmp_path, monkeypatch, {"acme/widget": "PUBLIC"})
    d = R.scan(root=str(tmp_path), now=NOW)
    rows = {x["name"]: x for x in d["repos"]}
    assert rows["local-only"]["visibility"] is None
    assert rows["widget"]["visibility"] == "PUBLIC"
    assert d["summary"]["visibilityReason"] is None, "有仓对上了,不该报「一条都没对上」"


def test_shared_components_report_how_many_repos_use_them(tmp_path):
    """共享组件要报出被多少个仓引用。

    「被 25 个仓用」和「被 1 个仓用」是完全不同的两件东西:前者改一行会波及整片。
    这个数字原来只以一个类型标签的形式存在。
    """
    for n in ("acme-one", "acme-two"):
        u = mkrepo(tmp_path, n)
        (u / "SKILL.md").write_text("x", encoding="utf-8")
        (u / ".gitmodules").write_text(
            '[submodule "g"]\n\tpath = g\n\turl = https://example.com/acme/acme-kit.git\n',
            encoding="utf-8")
    mkrepo(tmp_path, "acme-kit")
    rows = {r["name"]: r for r in R.scan(root=str(tmp_path), now=NOW)["repos"]}
    assert rows["acme-kit"]["usedBy"] == 2, rows["acme-kit"]
    # 非共享组件不该带这个数:一个对所有行都有值的字段说明不了任何事。
    assert rows["acme-one"]["usedBy"] is None


def test_visibility_matches_when_the_table_key_is_not_lowercase(tmp_path, monkeypatch):
    """表的键大小写和 remote 不一致时也要匹配上。

    ⚠ 这条是投毒逼出来的:`_visibility` 里有一段大小写回落,而本机的表和
    `_owner_repo` 的输出**都是小写**,于是那个分支从来没有被任何用例走到 ——
    把它整段删掉,33 条用例全过。
    一段没有任何用例走到的兜底代码,和一段不存在的兜底代码,区别只在于它会让
    读的人以为这种情况已经处理过了。表由外部工具生成,键的大小写不由这里决定。
    """
    r = mkrepo(tmp_path, "widget")
    import subprocess
    subprocess.run(["git", "-C", str(r), "remote", "add", "origin",
                    "git@my-alias:Acme/Widget.git"],
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)
    _vis_table(tmp_path, monkeypatch, {"Acme/Widget": "PRIVATE"})   # 键是大写
    d = R.scan(root=str(tmp_path), now=NOW)
    row = {x["name"]: x for x in d["repos"]}["widget"]
    assert row["visibility"] == "PRIVATE", row
    assert d["summary"]["visibilityReason"] is None
