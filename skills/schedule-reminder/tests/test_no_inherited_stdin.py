#!/usr/bin/env python3
"""每一个子进程调用都必须显式给 stdin 和 creationflags。

这条来自一次真实故障:控制台在生产里是用 pythonw 启动的(无控制台窗口),而所有开发期
测试都用普通 python 跑。普通 python 下有一个有效的 stdin,子进程继承它、一切正常;
pythonw 下没有,子进程继承到一个无效句柄,然后**阻塞在等待输入上**。

表现是:接口 curl 得动、页面上那一栏永远显示「扫描中」、后台堆着一串永不退出的进程。
没有报错,没有超时,因为进程确实还活着。

**探针必须复现真实的调用形状。** 用 python 测一个用 pythonw 跑的东西,测的是另一个程序。

这条测试用 AST 扫源码而不是靠人自觉:一个只靠约定的规则,会在下一个人加一处调用时
静默失效,而失效的表现正是上面那种「看起来只是慢」的挂起。
"""
import ast
import os
import sys

import pytest

PKG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "task_console")
sys.path.insert(0, PKG)

PY_FILES = sorted(f for f in os.listdir(PKG) if f.endswith(".py"))


def subprocess_calls(path):
    """找出这个文件里所有 subprocess.run / Popen / check_output 调用。"""
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) \
                and f.value.id == "subprocess" and f.attr in ("run", "Popen", "check_output",
                                                              "call", "check_call"):
            out.append(node)
    return out


def test_there_are_subprocess_calls_to_check():
    """负对照:如果一处都没扫到,下面那条会空转全绿。"""
    found = sum(len(subprocess_calls(os.path.join(PKG, f))) for f in PY_FILES)
    assert found > 0, "一处 subprocess 调用都没扫到,这条规则形同虚设"


@pytest.mark.parametrize("fname", PY_FILES)
def test_every_subprocess_call_pins_stdin(fname):
    path = os.path.join(PKG, fname)
    for node in subprocess_calls(path):
        kw = {k.arg for k in node.keywords if k.arg}
        assert "stdin" in kw, (
            f"{fname}:{node.lineno} 的子进程调用没有显式给 stdin。"
            "在 pythonw 下它会继承一个无效句柄并永久挂起,而且看起来只是慢。")
        assert "creationflags" in kw, (
            f"{fname}:{node.lineno} 的子进程调用没有给 creationflags。"
            "在 pythonw 下每个控制台子程序都要新分配一个控制台,单次慢几十倍,"
            "并发时直接超时。实测:同一条命令 python 下几毫秒、pythonw 下 4.5 秒、"
            "四个并发全部 15 秒超时;加上 CREATE_NO_WINDOW 之后 0.08 秒。")
