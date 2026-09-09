#!/usr/bin/env python3
"""调色板对账:`--tblr-*-rgb` 必须和它对应的十六进制变量是同一个颜色。

页面里同一个颜色被手写了两遍:`--bad:#AB3123` 给自己的组件用,`--tblr-danger-rgb:171,49,35`
给 Tabler 的徽章用(Tabler 的组件按 rgb 三元组算半透明底色,拿不到十六进制)。
两份都没有说自己是不是权威。

现在没坏,但**没有任何东西对账**:改 `--bad` 的人没有义务知道另一处还有一份,
改完之后侧栏徽章会继续用旧红色,页面上出现两种红,两处都不说自己是哪一份。
这类缺陷不会在改动当天暴露 —— 它在下一次调色时暴露,而那时没人记得有第二处。

所以对账放在这里,而不是靠注释提醒:一条注释挡不住第二个人。
(不改成运行时从十六进制算 rgb,是因为那要等 JS 跑完才生效,首屏会闪一下颜色;
 一个每次加载都闪的页面比一条会红的测试差。)
"""
import os
import re

import pytest

PAGE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                    "scripts", "task_console", "console.html")

# Tabler 的角色名 -> 本页调色板里的同一个颜色
PAIRS = {"danger": "bad", "success": "ok", "warning": "warn", "primary": "cyan"}

_HEX = re.compile(r"--(\w+):\s*(#[0-9A-Fa-f]{6})\b")
_RGB = re.compile(r"--tblr-(\w+)-rgb:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")


def _blocks(css: str):
    """按 `{...}` 切出每个声明块。同一个块里的两份定义才该互相对账 ——
    亮色块的 --bad 和暗色块的 --tblr-danger-rgb 本来就不该相等,
    跨块比会造出一堆假失败,然后这个测试会被删掉。"""
    out, depth, start = [], 0, None
    for i, ch in enumerate(css):
        if ch == "{":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(css[start:i])
                start = None
    return out


@pytest.fixture(scope="module")
def css():
    with open(PAGE, encoding="utf-8") as f:
        s = f.read()
    i, j = s.index("<style"), s.rindex("</style>")
    return s[i:j]


def test_every_rgb_triplet_matches_its_hex(css):
    checked = 0
    for blk in _blocks(css):
        hexes = {m.group(1): m.group(2) for m in _HEX.finditer(blk)}
        for m in _RGB.finditer(blk):
            role = m.group(1)
            name = PAIRS.get(role)
            if name is None:
                pytest.fail(f"--tblr-{role}-rgb 没有登记在 PAIRS 里。"
                            f"新增一个角色就要同时登记它对账的对象,"
                            f"否则这个测试会安静地不检查它。")
            hx = hexes.get(name)
            assert hx, (f"同一个块里有 --tblr-{role}-rgb 却没有 --{name}。"
                        f"两者必须成对出现,否则对账无从谈起。")
            want = tuple(int(hx[k:k + 2], 16) for k in (1, 3, 5))
            got = tuple(int(m.group(k)) for k in (2, 3, 4))
            assert got == want, (
                f"--tblr-{role}-rgb 是 {got},而 --{name} 是 {hx} = {want}。"
                f"同一个颜色的两份手写副本已经漂了:页面上会出现两种{role}。")
            checked += 1
    # 一个被喂了空的检查器打印出的绿色,和一个真没查出问题的检查器一模一样。
    assert checked >= 12, f"只对上了 {checked} 组,预期至少 12 组(3 个主题块 x 4 个角色)"


def test_pairs_cover_every_tblr_rgb_in_the_file(css):
    """文件里出现的每一个 --tblr-*-rgb 都要在 PAIRS 里有对家。

    上面那个测试是逐块扫的,一个只出现在某个没被切出来的位置的三元组会被整个跳过。
    这一条从全文扫,专门盯「新加了一个角色但忘了登记」。
    """
    roles = {m.group(1) for m in _RGB.finditer(css)}
    assert roles, "文件里一个 --tblr-*-rgb 都没找到,这个测试没在检查任何东西"
    assert roles <= set(PAIRS), f"这些角色没有登记对家: {sorted(roles - set(PAIRS))}"
