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


# ---------- 固定高度的元素选择器 ----------
# 这一条是为一个犯了两次的错写的:样式表里曾有 `button{height:22px}`,一条元素选择器,
# 于是任何拿 <button> 当积木的多行部件都被钉死,内容照自己的高度排、溢出去盖住下一行。
# 屏幕上看是排版错乱,不像高度被覆盖,而且各项高度的数字读起来完全「合理」。
# 第一次是概览的六个指标格,第二次是时间轴的三个缩放按钮(换成比例字体后内容涨到 30px)。
# 当时的对策是「以后记得写 height:auto」,两次都没记住。所以改成 min-height 并在这里钉住。

import re as _re


def _page_style():
    """页面里所有 <style> 块拼起来。"""
    html = open(PAGE, encoding="utf-8").read()
    blocks = _re.findall(r"<style>(.*?)</style>", html, _re.S)
    assert blocks, "页面里一个 <style> 都没读到,这条检查会因为没东西可查而打印绿色"
    css = chr(10).join(blocks)
    # 注释要剥掉:样式表里那段解释为什么不能写死高度的注释,本身就引用了 `button{height:22px}`
    # 这个反例,于是检查器把讲这条规则的话当成了这条规则。
    # 一道会被「讨论它自己」触发的闸,最后一定会被人关掉。
    return _re.sub(r"/\*.*?\*/", "", css, flags=_re.S)


# td/th 不在名单里:在表格布局里 height 本来就被当成最小值,内容多了格子会自己长,
# 所以那不是这个 bug。把它们算进来只会逼着下一个人去放宽这条检查,
# 而放宽过的检查会连真的那一类一起放过。
# `[^-]height` 写不得:它要求 height 前面必须有一个字符,于是 `button{height:22px}`
# 这种把 height 放在第一条的写法直接漏掉。这个盲区是下面那条负对照抓出来的,
# 而它正是「负对照不是走过场」的实例:第一版正则对着页面打印绿色,同时漏掉一半形状。
_FIXED_H = _re.compile(r"(?<![.#\w-])(button|input|select)\s*\{[^}]*?(?<!-)height\s*:\s*\d")


def test_no_element_selector_pins_a_fixed_height():
    css = _page_style()
    hits = [m.group(0)[:70] for m in _FIXED_H.finditer(css)]
    assert not hits, (
        "元素选择器上写死 height 会裁掉任何多行部件,用 min-height。命中: " + repr(hits))


def test_the_fixed_height_rule_can_actually_fire():
    """负对照:把那条规则投毒回去,上面的检查必须命中。
    不做这一步的话,一个正则写错的检查和一个真没发现问题的检查打印出的绿色一模一样。"""
    poisoned = _page_style() + "\nbutton{height:22px}\n"
    assert _FIXED_H.search(poisoned), "投毒后仍未命中,这条正则是空拦的"


# ---------- 装内容的容器不许写死高度 ----------
# 上一轮把 `button{height:22px}` 改成 min-height 并配了一道闸,但那道闸只盯**元素选择器**,
# 而同一个坑在 `#bar{height:26px}` 上原样留着:#bar 继承了 .navbar 的 flex-wrap,
# 窗口窄到约 760px 时五个全局计数换到第二行,然后被裁进 26px 的盒子里,
# 既看不见也几乎不可能滚到。整页最顶上的「有没有事」摘要于是在窄屏下静默消失。
#
# 想把那道闸按形状扩到 id/class 上,量过一次:对已知正样本召回 2/2,
# **但在干净文件上误报 18 处** —— 圆点、进度条、时间轴刻度这类本来就该定高的装饰。
# 一道在干净代码上报 18 次的闸门会被关掉,所以不按形状扩,改成点名。
# 名单里的每一项都是「装会变多变少的内容」的容器;新增一个容器要显式加进来,那是刻意的。
_CONTAINERS = ("#bar", "#side", "#view", ".pad", ".box", ".mt-rows", ".rp-grid", ".tiles")


def _rules_for(css, sel):
    """返回**正好是** sel 这个选择器的规则体。

    刻意不含后代:`#bar .seg{height:100%}` 是子元素填满父级的合法写法,
    第一版把它算了进来,于是这道闸对着干净文件报了一次 : 一道会误报的闸门会被关掉,
    所以宁可窄一点,名单里再加容器也是显式动作。
    """
    out = []
    for m in _re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        heads = [h.strip() for h in m.group(1).split(",")]
        if any(h == sel or h.startswith(sel + ".") or h.startswith(sel + ":")
               for h in heads):
            out.append(m.group(2))
    return out


# 只认 px。百分比和 vh 是「跟着别人走」,不会把内容裁掉;写死像素才会。
_PX_H = _re.compile(r"(?<!-)height\s*:\s*\d+(\.\d+)?px")


def test_layout_containers_do_not_pin_a_fixed_height():
    css = _page_style()
    bad = []
    for sel in _CONTAINERS:
        for body in _rules_for(css, sel):
            if _PX_H.search(body):
                bad.append(sel + " {" + " ".join(body.split())[:60] + "}")
    assert not bad, "容器写死 height 会把溢出的内容裁掉,用 min-height。命中: " + repr(bad)


def test_the_container_check_can_actually_fire():
    """负对照。这条尤其要紧:上面那个 _rules_for 只要选择器匹配写错一点,
    就会对着任何输入都返回空列表,然后永远打印绿色。"""
    # 用合成样式表,不拼真文件:拼真文件的话,这条负对照的成立与否会取决于产品此刻干不干净,
    # 而一条依赖被测对象状态的负对照,在被测对象变脏的那一刻就跟着红,分不清是谁的问题。
    def hits(css):
        return [b for sel in _CONTAINERS for b in _rules_for(css, sel) if _PX_H.search(b)]

    assert hits("#bar{height:26px}"), "投毒后仍未命中,这条检查是空拦的"
    assert hits(".mt-rows{overflow:auto;height:172px}"), "属性不在首位时也要命中"
    # 反向,四种都不该命中:百分比、视口单位、min-/max- 前缀、以及后代选择器。
    assert not hits("#view{height:100%}"), "百分比是跟着别人走,不会裁掉内容"
    assert not hits("#view{height:50vh}"), "视口单位同理"
    assert not hits("#bar{min-height:26px}"), "min-height 正是我们要的写法"
    assert not hits("#bar{max-height:26px}"), "max-height 不该被这条规则管"
    assert not hits("#bar .seg{height:100%}"), "子元素填满父级是合法写法"


def test_the_container_check_reads_a_real_stylesheet():
    """再一条:确认它真的读到了那几个容器的规则,而不是一个都没匹配上。"""
    css = _page_style()
    found = [sel for sel in _CONTAINERS if _rules_for(css, sel)]
    assert len(found) >= 5, f"只匹配到 {found},选择器写法可能和样式表对不上"
