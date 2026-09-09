#!/usr/bin/env python3
"""拼出来的 PowerShell 里,每个具名参数都必须真的存在于那个 cmdlet 上。

这道闸的由来是一个活了很久的缺陷:`retire.py` 的 `_disable` 拼的是
`Set-ScheduledTask -TaskName X -Description Y`,而 **Set-ScheduledTask 没有 -Description 参数**。
参数绑定失败是终止错误,整条 -Command 在那里中止、退出码 1,于是退役这条路
**每一次都失败**,而它自己的模块文档还写着「退役 = 停用 + 退出两处登记 + 把原因写进 Description」。

它为什么一直全绿:`tests/test_retire.py` 把整个 `_disable` monkeypatch 掉了。
被替身换掉的那个函数正是唯一会碰真机的那个,于是测试覆盖的是「替身返回了什么」。
**一个把唯一有风险的那一步换成替身的测试套件,和没有测试的区别只在于它会打印绿色。**

所以判据必须来自真机而不是我的记忆:向 PowerShell 要那个 cmdlet 的参数表,
拿它去比对源码里拼出来的每一个 `-Param`。这条规则不认识业务,只认识
「你给了一个这个命令不接受的参数」—— 而那正是当时发生的事。

同一段代码里还叠着第二个 bug,是我写探针时自己撞上的:
`-notlike "*$note*"` 里的 `$note` 是 `[RETIRED] 原因`,而 `-like` 把 `[...]` 当**通配符字符类**,
PowerShell 直接报 "The specified wildcard character pattern is not valid"。
判「包含」要用 `.Contains()`。第二个用例钉的是这一条。
"""
import ast
import json
import os
import re
import subprocess
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

pytestmark = pytest.mark.skipif(os.name != "nt", reason="要问真机的 PowerShell 要参数表")

# 只扫这些文件:它们是拼 PowerShell 字符串的地方。
SOURCES = ("retire.py", "maint.py")

# 「大写开头的动词-名词」形状的 cmdlet,以及它到本条命令结束(; 或 | 或行尾)为止的全部实参。
#
# ⚠ 第一版把实参写成「只抓紧挨着的一串参数标志」,而真实写法是
# `Set-ScheduledTask -TaskName $env:TC_NAME -Description $t.Description`:
# `-TaskName` 后面跟了一个**值**,正则在那里停住,于是 `-Description` 根本没进扫描范围。
# 投毒时才发现:把当年那个 bug 原样放回去,这道闸照样打印绿色 ——
# **它没抓住它存在的唯一理由。** 一个为已修 bug 补的闸,不投毒就等于没写。
_CALL = re.compile(r"\b([A-Z][A-Za-z]+-[A-Z][A-Za-z]+)([^;|\r\n]*)")
_PARAM = re.compile(r"(?<![\w$])-([A-Za-z]+)\b")

# PowerShell 的中缀运算符长得和具名参数一模一样,必须排掉,否则 `-not` / `-like` 会被
# 当成参数报出来 —— 一个动不动就叫的闸门会被绕过,那比没有闸门更糟。
_OPS = frozenset("""not eq ne lt gt le ge like notlike match notmatch contains notcontains
in notin and or xor is isnot as replace split join f band bor bxor bnot shl shr ceq cne
clike cnotlike cmatch imatch""".split())

# PowerShell 给每个 cmdlet 都加的公共参数,不在 Parameters.Keys 里也算合法。
COMMON = frozenset("""Verbose Debug ErrorAction WarningAction InformationAction ErrorVariable
WarningVariable InformationVariable OutVariable OutBuffer PipelineVariable WhatIf Confirm""".split())


def _ps_strings(path):
    """从源码里取出所有**当成代码用的**字符串常量。

    用 AST 而不是正则:正则会把讲这条规则的那段话本身当成代码扫进来。
    但只用 AST 还不够 —— **docstring 也是字符串常量**,而修好那两个 bug 的同时我在
    docstring 里写下了「原来错在 `-notlike "*$note*"`」,于是这道闸第一次跑就把自己的
    说明文字判成了违规。这不是误报,是判据没说清:它要查的是**会被执行的** PowerShell,
    而不是任何一段恰好提到 PowerShell 的文本。
    所以先把 docstring 的节点记下来再排除掉。
    """
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docs):
            out.append(node.value)
    return out


@pytest.fixture(scope="module")
def param_table():
    """向真机要每个用到的 cmdlet 的参数表。问不出来就 fail,不 skip:
    一个「问不出来于是跳过」的闸门,和一个不存在的闸门在报告里长得一样。"""
    names = set()
    for fn in SOURCES:
        for s in _ps_strings(os.path.join(_SCR, fn)):
            for m in _CALL.finditer(s):
                names.add(m.group(1))
    if not names:
        pytest.fail("一个 cmdlet 调用都没扫到 —— 扫描器本身坏了,不是代码干净")
    ps = ("$o=@{}; " + "; ".join(
        f"try {{ $o['{n}'] = @((Get-Command {n} -ErrorAction Stop).Parameters.Keys) }} "
        f"catch {{ $o['{n}'] = $null }}" for n in sorted(names))
        + "; $o | ConvertTo-Json -Compress -Depth 3")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180, stdin=subprocess.DEVNULL)
    assert r.returncode == 0 and r.stdout.strip(), f"问不到参数表: {r.stderr[:300]}"
    return json.loads(r.stdout)


def test_every_named_parameter_exists_on_its_cmdlet(param_table):
    checked, bad = 0, []
    for fn in SOURCES:
        for s in _ps_strings(os.path.join(_SCR, fn)):
            for m in _CALL.finditer(s):
                cmd = m.group(1)
                params = [p for p in _PARAM.findall(m.group(2))
                          if p.lower() not in _OPS]
                known = param_table.get(cmd)
                if known is None:
                    bad.append(f"{fn}: 这台机器上没有 cmdlet {cmd}")
                    continue
                allowed = set(known) | COMMON
                for p in params:
                    checked += 1
                    # PowerShell 允许唯一前缀简写,所以前缀命中也算数。
                    if p not in allowed and not any(
                            k.lower().startswith(p.lower()) for k in allowed):
                        bad.append(f"{fn}: {cmd} 没有 -{p} 这个参数")
    assert not bad, "拼出来的 PowerShell 用了不存在的参数:\n  " + "\n  ".join(bad)
    # 一个被喂了空的检查器打印的绿色,和一个真没查出问题的检查器一模一样。
    assert checked >= 5, f"只校验了 {checked} 个参数,扫描器大概没扫到东西"


def test_note_containment_does_not_use_wildcard_matching():
    """判「Description 里已经有这条退役标记了吗」不能用 -like。

    标记是 `[RETIRED] 原因`,而 -like 把 `[...]` 当字符类:轻则误判成「已经写过」
    而跳过写入,重则直接抛 "The specified wildcard character pattern is not valid"。
    """
    src = "\n".join(_ps_strings(os.path.join(_SCR, "retire.py")))
    offenders = [ln.strip() for ln in src.splitlines()
                 if re.search(r"-(not)?like\b", ln) and "note" in ln]
    assert not offenders, "用 -like 匹配含方括号的标记:\n  " + "\n  ".join(offenders)
    assert ".Contains(" in src, "没有看到 .Contains(,包含判断大概换了别的写法"


def test_write_step_reads_back_instead_of_trusting_exit_code():
    """写完要读回来自证。

    只看退出码的写操作,在「命令跑了但什么都没改」时和成功长得一模一样 ——
    而那正是这个函数原来的形态:它连命令都没跑成,返回的却只有一个退出码。
    """
    src = "\n".join(_ps_strings(os.path.join(_SCR, "retire.py")))
    assert "throw" in src, "停用步骤里没有任何 throw,说明它不会因为读回来不对而失败"
    assert re.search(r"Get-ScheduledTask[^;]*;\s*if \(-not", src) or "$v" in src, \
        "没有看到写完之后重新 Get 一次的自证步骤"
