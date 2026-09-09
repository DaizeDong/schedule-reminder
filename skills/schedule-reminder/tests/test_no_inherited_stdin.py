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


_SPAWNERS = ("run", "Popen", "check_output", "call", "check_call")


def subprocess_calls(path):
    """找出这个文件里所有 subprocess.run / Popen / check_output 调用。

    ⚠ 第一版只认 `模块名.方法` 这一种形状(`isinstance(f.value, ast.Name) and
    f.value.id == "subprocess"`)。`from subprocess import run` 之后直接 `run(...)`、
    或者 `import subprocess as sp` 之后 `sp.run(...)`,**两种都扫不到**,
    而扫不到的调用不会被判红,也不会出现在「有没有东西可查」那条计数里(它只断言 found>0)。
    此刻生产代码恰好六个文件都写成 `subprocess.run`,所以闸门**看起来**是全的 ——
    一个只认一种写法的静态闸,和一个只在作者本人的习惯下有效的规矩是同一种东西。
    现在跟着 import 走:先看这个文件把 subprocess 绑成了什么名字,再按那个名字匹配;
    from-import 进来的裸名单独收。
    """
    tree = ast.parse(open(path, encoding="utf-8").read())

    mod_names = {"subprocess"}          # `import subprocess` / `import subprocess as sp`
    bare_names = set()                  # `from subprocess import run, Popen`
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess" and a.asname:
                    mod_names.add(a.asname)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name in _SPAWNERS:
                    bare_names.add(a.asname or a.name)

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
                and f.value.id in mod_names and f.attr in _SPAWNERS):
            out.append(node)
        elif isinstance(f, ast.Name) and f.id in bare_names:
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


def test_the_scanner_sees_every_way_of_spelling_the_call(tmp_path):
    """扫描器必须认全三种写法,而不只是作者惯用的那一种。

    ⚠ 「有没有东西可查」那条只断言 found>0,而生产代码此刻恰好六个文件都写成
    `subprocess.run` —— 于是一个只认这一种形状的扫描器**看起来是全的**。
    把 `from subprocess import run` 或 `import subprocess as sp` 引进来的那一天,
    新的调用不会被判红、也不会让计数缺一块:它就是不在扫描范围里。
    **一个只在作者本人的习惯下有效的静态闸,和没有闸门的区别只是它会打印绿色。**

    所以判据用合成文件:三种写法各一次,外加两个长得像但不是的,必须正好数出三个。
    """
    src = "\n".join([
        "import subprocess",
        "import subprocess as sp",
        "from subprocess import run, Popen",
        "subprocess.run(['a'])",          # 1
        "sp.Popen(['b'])",                # 2
        "run(['c'])",                     # 3
        "Popen(['d'])",                   # 4
        # 下面这些不算:同名但不是那个模块的东西
        "class X:",
        "    def run(self): pass",
        "X().run()",
        "other.run(['e'])",
    ])
    p = tmp_path / "sample.py"
    p.write_text(src, encoding="utf-8")
    got = len(subprocess_calls(str(p)))
    assert got == 4, (
        f"扫到 {got} 个,期望 4(subprocess.run / sp.Popen / run / Popen)。"
        f"只认「模块名点方法」时这里只会数到 1。")
