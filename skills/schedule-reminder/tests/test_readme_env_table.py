"""README 的环境变量表 vs 生产代码真的读的那些。

这一条存在的理由是一次真实事故:`TASK_CONSOLE_VISIBILITY` 在代码里是一个正经来源,
自检把它算进分母,而**这台机器上的启动器从来没有设过它** —— 于是几十个仓一个
公开/私有徽章都没有,页面上「表里没登记这几个仓」和「压根没加载这张表」长得一样。
启动器住在这个仓外面,测不到;能测的是它的上游:**README 那张表**。
一个没被写进文档的变量,任何人照着文档配出来的启动器都会缺它,而缺项是静默的。

对账是双向的:文档里多一行(讲一个已经没人读的变量)同样是红的,
因为那会让人去配一个不起作用的东西,然后以为自己配全了。
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "task_console"))

import selfcheck as SC  # noqa: E402

from test_selfcheck import _env_vars_production_actually_reads  # noqa: E402

README = os.path.join(os.path.dirname(SC.__file__), "README.md")


def _documented():
    """README 里被写成表格行的每一个变量名。

    只认反引号包起来、且出现在一个 `|` 开头的表格行里的名字 —— 散文里顺口提一句
    不算「文档化」,那样的提法不会让人真的去 export 它。
    """
    if not os.path.exists(README):
        pytest.fail("README 不在:" + README)
    pat = re.compile(r"`(TASK_CONSOLE_[A-Z_]+)`")
    out = {}
    with open(README, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if not line.lstrip().startswith("|"):
                continue
            for m in pat.findall(line):
                out.setdefault(m, []).append(i)
    return out


def test_the_scanner_found_something():
    """负对照:两边都不能是空的,否则下面那条对账是在比较两个空集合。"""
    doc = _documented()
    assert len(doc) >= 10, f"README 表里只扫到 {len(doc)} 个,扫描器大概没在工作"
    assert len(_env_vars_production_actually_reads()) >= 10


def test_every_env_var_production_reads_is_documented():
    """代码在读的,README 必须有一行。缺一行 = 照文档配的启动器缺一项。"""
    found = _env_vars_production_actually_reads()
    doc = _documented()
    missing = sorted(set(found) - set(doc))
    assert not missing, (
        "这些变量生产代码在读,README 的表里却没有:\n  "
        + "\n  ".join(f"{v}  (首次出现 {found[v][0]})" for v in missing)
        + "\n(照 README 配出来的启动器会缺这几项,而缺项在页面上是静默的。)")


def test_readme_does_not_document_variables_nobody_reads():
    """README 多写的一行,会让人去配一个不起作用的东西。"""
    found = _env_vars_production_actually_reads()
    doc = _documented()
    extra = sorted(set(doc) - set(found))
    assert not extra, (
        "README 表里有,而生产代码里已经没人读了:\n  "
        + "\n  ".join(f"{v}  (README:{doc[v][0]})" for v in extra))


def test_each_documented_row_says_what_happens_when_it_is_unset():
    """每一行都必须讲清楚「不设会怎样」。

    这是这块面板的核心不变量:未检查和通过不能长得一样。一行只写「这是什么」
    的文档,会让人以为不设也没关系。
    """
    rows = {}
    with open(README, encoding="utf-8") as fh:
        for line in fh:
            if not line.lstrip().startswith("|"):
                continue
            m = re.search(r"`(TASK_CONSOLE_[A-Z_]+)`", line)
            if m:
                rows[m.group(1)] = line
    assert rows, "一行都没扫到"
    WORDS = ("unset", "not found", "falls back", "missing", "no default", "only ")
    thin = sorted(v for v, line in rows.items()
                  if not any(w in line.lower() for w in WORDS))
    assert not thin, (
        "这些行没说「不设会发生什么」:\n  " + "\n  ".join(thin))
