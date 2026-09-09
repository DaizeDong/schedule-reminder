#!/usr/bin/env python3
"""退役这条路的范围闸:只能够到根路径下的任务。

`act.ps1` 把这件事写得很清楚,而且三个动词全都钉了 `-TaskPath '\\'`:

    Root path only. A console that can reach \\Microsoft\\Windows\\** could disable
    Windows Update or Defender maintenance by typing a name, and nothing here needs that reach.

`/api/act` 还在动手前重新枚举一次 `collect.ps1` 并要求名字在册(而 collect 只枚举根路径、
再按 VendorPattern 剔掉厂商任务)。

**而退役这条路一道都没有。** 它的三条 PowerShell 全部不带 `-TaskPath`,
于是一个从 HTTP 过来的名字能够到 `\\Microsoft\\Windows\\**` 底下的任务 ——
而退役比停用更重:它还会改两处登记文件。

实测(2026-09-09,真机):旧写法 `Get-ScheduledTask -TaskName 'SilentCleanup'`
够得到 `\\Microsoft\\Windows\\DiskCleanup\\SilentCleanup`(状态 Ready);钉了根路径之后够不到。
"""
import ast
import os
import sys

import pytest

_SCR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "scripts", "task_console")
sys.path.insert(0, _SCR)

import retire as R  # noqa: E402

# 会真的碰到任务的那几个 cmdlet。它们每一次出现都必须带 -TaskPath。
SCOPED = ("Get-ScheduledTask", "Set-ScheduledTask", "Disable-ScheduledTask",
          "Enable-ScheduledTask", "Start-ScheduledTask", "Stop-ScheduledTask")


def _code_strings(path):
    """源码里当成代码用的字符串常量(排掉 docstring)。

    排 docstring 是必须的:这个文件的说明文字里就写着旧的、不带 -TaskPath 的写法,
    而那正是要禁的形状 —— 一个会把讲规则的文字判成违规的扫描器,第一次跑就会红,
    然后被人关掉。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docs.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def test_every_task_cmdlet_in_retire_pins_the_root_path():
    """静态闸:凡碰任务的 cmdlet,每一次出现都要带 -TaskPath。

    按**出现次数**判而不是「文件里有没有 -TaskPath」:后者在四处里改对一处就能满足,
    而漏掉的那一处正是会出事的那一处。
    """
    bad, checked = [], 0
    for s in _code_strings(os.path.join(_SCR, "retire.py")):
        for cmd in SCOPED:
            idx = 0
            while True:
                i = s.find(cmd, idx)
                if i < 0:
                    break
                idx = i + len(cmd)
                checked += 1
                # 看这条命令到分号/管道为止的那一段里有没有 -TaskPath
                seg = s[i:]
                for stop in (";", "|"):
                    j = seg.find(stop)
                    if j > 0:
                        seg = seg[:j]
                # -InputObject 收的是一个**已经取回来的任务对象**,它的范围由取它的那次
                # Get 决定 —— 而那次 Get 受同一条规则约束,所以这里不是漏洞。
                # (第一版没有这条,把 `Set-ScheduledTask -InputObject $t` 判成了违规:
                #  一个把正确写法也判红的闸门会被整体关掉,那比没有闸门更糟。
                #  正确做法是收紧判据,不是加一条整体豁免。)
                if "-TaskPath" not in seg and "-InputObject" not in seg:
                    bad.append(f"{cmd} 既没钉 -TaskPath 也不是 -InputObject: {seg.strip()[:80]}")
    assert not bad, "退役这条路有 cmdlet 没有钉根路径:\n  " + "\n  ".join(bad)
    assert checked >= 4, f"只扫到 {checked} 处 cmdlet,扫描器大概没扫到东西"


@pytest.mark.skipif(os.name != "nt", reason="要问真机的任务计划")
def test_a_vendor_task_outside_the_root_path_is_out_of_reach():
    """真机判据:一个只存在于 \\Microsoft\\Windows\\** 底下的任务必须够不到。

    静态闸只能证明字符串里有那个参数;这一条证明它真的挡住了东西。
    用 SilentCleanup 是因为它在任何一台 Windows 上都存在于
    `\\Microsoft\\Windows\\DiskCleanup\\` 底下,而且**这条用例只读状态,不动它**。
    """
    from maint import Refused
    with pytest.raises(Refused) as ei:
        R._task_state("SilentCleanup")
    assert ei.value.code in ("state_unreadable", "not_found"), ei.value.code


@pytest.mark.skipif(os.name != "nt", reason="要问真机的任务计划")
def test_a_root_path_task_is_still_reachable():
    """正对照:根路径下的任务照样读得到。

    少了这一条,一个「什么都够不到」的实现也能让上面那条通过 ——
    而那个实现会让退役功能整个失效,同样是静默的。
    """
    import json
    import subprocess
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-ScheduledTask -TaskPath '\\' | Select-Object -First 1 -ExpandProperty TaskName"],
        capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    name = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not name:
        pytest.skip("这台机器上枚举不到根路径任务")
    state = R._task_state(name[0])
    assert state is None or isinstance(state, str), state
    _ = json  # noqa: F841
