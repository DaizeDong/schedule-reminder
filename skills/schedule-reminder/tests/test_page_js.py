#!/usr/bin/env python3
"""页面里的脚本必须能解析。

这条来自同一个错误在一天里犯了两次:往 `if / else if / else` 链中间插了两行,整块脚本
当场解析失败。表现是**页面照常打开、状态码 200、每一栏都停在「读取中」** :
没有红色,没有报错弹窗,只是什么都不动。一个语法错误在这里长得像「后端有点慢」。

两次的成因也一样:替换锚点用了一行同时出现在启动区和分发链里的代码。所以这条测试不是
在防手滑,是在防一类**改法**:任何按文本替换往这个文件里插东西的动作,都要有一道
能当场报错的闸。

node 不在就跳过,并且大声说明跳过了 : 一个静默跳过的检查比没有检查更糟,
它会让人以为这里被看过了。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

PAGE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                    "scripts", "task_console", "console.html")
NODE = shutil.which("node")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def scripts():
    """页面里所有内联脚本块。"""
    html = open(PAGE, encoding="utf-8").read()
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def check(js: str):
    fd, path = tempfile.mkstemp(suffix=".js")
    os.close(fd)
    try:
        open(path, "w", encoding="utf-8").write(js)
        r = subprocess.run([NODE, "--check", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60,
                           stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
        return r.returncode, (r.stderr or r.stdout or "")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def test_there_is_a_script_to_check():
    """负对照的第一半:抽不到脚本时下面那条会空转全绿。"""
    blocks = scripts()
    assert blocks, "页面里一个内联脚本块都没抽到"
    assert sum(len(b) for b in blocks) > 5000, "抽到的脚本短得不像整页逻辑"


@pytest.mark.skipif(NODE is None, reason="没有 node,无法解析页面脚本")
def test_every_inline_script_parses():
    for i, js in enumerate(scripts()):
        rc, err = check(js)
        assert rc == 0, f"第 {i + 1} 个脚本块解析失败:\n{err[:600]}"


@pytest.mark.skipif(NODE is None, reason="没有 node,无法解析页面脚本")
def test_the_check_can_actually_fail():
    """负对照的另一半:证明这道闸真的会红。

    没有它,一个永远返回 0 的检查器和一个真的解析通过的检查器输出一模一样。
    投毒的形状刻意选成那次真事故:往 if/else 链中间插一行。
    """
    good = scripts()[0]
    poisoned = good.replace("else await loadMaint();",
                            "loadConvos();\n    else await loadMaint();", 1)
    assert poisoned != good, "投毒锚点没命中,这条负对照什么都没证明"
    rc, err = check(poisoned)
    assert rc != 0
    assert "else" in err.lower()
