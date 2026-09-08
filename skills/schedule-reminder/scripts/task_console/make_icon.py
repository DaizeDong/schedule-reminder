#!/usr/bin/env python3
"""从 icon.svg 的同一份配色生成一个 .ico,给 Windows 快捷方式用。

为什么不把 .ico 直接放进仓库:那是一份没人能审的二进制。SVG 是文本,改了看得见 diff;
.ico 是它的派生物,装机时现生成。两者的配色写在同一个地方,改一处不会漂。

不依赖 SVG 渲染器:这九个方块用几个矩形就画出来了,引一个渲染库只为画九个方块不划算,
而且那会给一个「零构建」的工具加一条构建依赖。
"""

from __future__ import annotations

import os
import sys

# 和 icon.svg 里逐字一致的九格。改这里就要同步改那边,所以两处都写着这句话。
BG = (14, 26, 29, 255)
CELLS = [
    (0, 0, (44, 122, 85)), (1, 0, (44, 122, 85)), (2, 0, (14, 106, 117)),
    (0, 1, (14, 106, 117)), (1, 1, (171, 49, 35)), (2, 1, (44, 122, 85)),
    (0, 2, (44, 122, 85)), (1, 2, (140, 98, 16)), (2, 2, (44, 122, 85)),
]
SIZES = (16, 24, 32, 48, 64, 128, 256)


def render(size: int):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 64.0
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=max(2, int(13 * s)), fill=BG)
    for cx, cy, color in CELLS:
        x0 = (12 + cx * 14.5) * s
        y0 = (12 + cy * 14.5) * s
        d.rounded_rectangle([x0, y0, x0 + 11 * s, y0 + 11 * s],
                            radius=max(1, int(2.5 * s)), fill=color + (255,))
    return img


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("用法: make_icon.py <输出.ico>", file=sys.stderr)
        return 2
    out = os.path.expanduser(argv[0])
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        # 没有 PIL 就明说,不生成一个半成品文件。快捷方式没有图标是小事,
        # 一个损坏的 .ico 会让快捷方式整个显示成空白。
        print("需要 Pillow 才能生成 .ico;跳过", file=sys.stderr)
        return 3
    imgs = [render(s) for s in SIZES]
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    imgs[-1].save(out, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"写出 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
