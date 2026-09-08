"""对话历史:按对话创建时所在的目录分组,用对话自己的标题当标题。

标题有六个来源,优先级从高到低,而且**结果里永远带着它来自哪一个**:

  custom-title  机主用 /rename 亲手起的名字,记在一条 custom-title 记录里
  rename-args   同一件事的另一种记法:命令本身带的参数(老一点的转录里是这个形状)
  ai-title      Claude Code 自动生成的那行,也就是没改名时窗口上显示的标题
  summary       会话被压缩时写下的一句话概括
  first-message 第一条真人打进去的消息的开头
  slug          自动生成的三词代号

前两个排最前没有别的理由:那是唯一两个**人明确说过「这场对话叫这个」**的地方,
后面四个都是推断出来的。三种记录都可以出现在文件任何位置,而且**同一场对话可以改名多次,
最后一次才算数**,所以头尾窗口盖不住它们。

这三种一起做一次全文件的字节级探测。字节扫描比逐行 json.loads 便宜一个数量级,而且
绝大多数转录一个标记都没有,所以只对命中的那少数几份再逐行解析。

为什么要标出来源:一个由 slug 充数的标题和一句真概括在界面上长得一样,而它们的信息量
差着量级。看的人有权知道自己在看哪一种。

分组用的是转录里记的 cwd,不是目录名。目录名是把路径里的分隔符和点号都换成短横做出来的,
这个变换**不可逆**:同一个目录名可能对应好几条真实路径。用它来分组会把不同项目并到一起,
而且并错了完全看不出来。

扫描只读文件的头和尾。整份读要吞掉一整个吉字节,而一次网页请求等不了。代价是有些字段
可能落在中间没被看到,所以每条记录都带 `partial`:**没读完就说没读完**,不把一个只看了
两头的计数报成确定的数字。
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from pathlib import Path

# 三种「这场对话叫什么」的记录。按字节找标记比逐行 json.loads 便宜一个数量级,
# 所以先用字节扫一遍,命中了才解析。
# 只匹配**裸类型名**,不带引号和冒号:带上就等于依赖 JSON 的具体排版(冒号后有没有空格),
# 而那是写入方的自由。依赖它的后果是标记一条都匹配不到,而表现是「这些对话都没有名字」:
# 一个看起来完全正常的答案。字节扫描本来就只是廉价预筛,真正的判定在逐行那一遍。
TITLE_MARKS = (b"custom-title", b"ai-title",
               b"<command-name>/rename</command-name>")

# 分块大小。做成常量是为了让「标记跨块」这件事可测:默认值下一块四兆,
# 任何真实标记都不可能被切开,于是那条边界逻辑永远跑不到。
CHUNK = 4 << 20

# 命令那一种的参数在 content 里,而 content 里是**真实换行**,所以必须开 DOTALL:
# 不开的话这个模式在真实数据上一条都匹配不到,而表现是「这些对话都没被改过名」,
# 一个看起来完全正常的答案。
_RENAME_ARGS = re.compile(r"<command-args>(.*?)</command-args>", re.S)
_RENAME_HEAD = "<command-name>/rename</command-name>"

HEAD_BYTES = 160_000
TAIL_BYTES = 80_000

# 这些 type 不是对话内容,只是会话状态的记录,不算消息。
_META_TYPES = {"last-prompt", "mode", "permission-mode", "bridge-session",
               "queue-operation", "summary", "attachment"}


def _iter_json(chunk: str):
    for line in chunk.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            yield json.loads(line)
        except ValueError:
            continue


def _typed_text(entry) -> str | None:
    """这条 user 记录是不是**人真的打进去的字**,是就返回它。

    判据是 content 的形状:人打的字在转录里是一个纯字符串,而系统注入的东西(skill 正文、
    工具结果、提醒块)是内容块的列表。这个区分是承重的:不做的话,一个计划任务的转录里
    会有十几条「用户消息」,因为它加载的每一个 skill 正文都算一条,于是每一个无头运行都
    被判成一场多轮对话。实测按块也算时,一个纯自动化目录报出 247 场「真人对话」。

    形状还不够,还要排掉那些确实是纯字符串、但由命令回显或提醒块构成的伪消息。
    """
    m = entry.get("message") or {}
    c = m.get("content")
    if not isinstance(c, str):
        return None
    t = c.strip()
    return t or None


def _looks_injected(text: str) -> bool:
    """系统注入的伪用户消息:提醒块、命令回显、钩子输出。它们不是人打的字。"""
    t = text.lstrip()
    return t.startswith(("<system-reminder", "<command-name", "<command-message",
                         "<local-command", "Caveat:", "[Request interrupted"))


def read_titles(path: Path) -> dict:
    """这场对话叫什么。返回 {"custom":…, "renameArgs":…, "ai":…},取不到的是 None。

    两遍:先按字节整个扫一遍找三个标记(便宜),一个都没有就直接返回(绝大多数转录如此);
    命中了才逐行解析(贵)。

    分块读的时候要留重叠,否则标记正好跨在两块之间就会被漏掉,而漏掉的表现是
    「这场对话没有名字」:一个看起来完全正常的答案。

    每一种都取**最后一次**:同一场对话可以改名多次。
    """
    longest = max(len(m) for m in TITLE_MARKS)
    seen, prev = False, b""
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                buf = prev + chunk
                if any(m in buf for m in TITLE_MARKS):
                    seen = True
                    break
                # 留的是**合并后**那个缓冲区的尾巴,不是新块的尾巴。取新块的尾巴在
                # 块比标记短的时候攒不够长度:一个跨了三块的标记会从窗口里溜过去。
                # 默认块四兆时这个区别永远看不出来,所以它只能靠把块调小来测。
                prev = buf[-(longest - 1):] if longest > 1 else b""
    except OSError:
        return {"custom": None, "renameArgs": None, "ai": None}
    if not seen:
        return {"custom": None, "renameArgs": None, "ai": None}

    custom = args = ai = None
    try:
        for line in io.open(path, encoding="utf-8", errors="replace"):
            if "title" not in line and "/rename" not in line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            t = obj.get("type")
            if t == "custom-title" and isinstance(obj.get("customTitle"), str):
                custom = obj["customTitle"].strip() or custom
            elif t == "ai-title" and isinstance(obj.get("aiTitle"), str):
                ai = obj["aiTitle"].strip() or ai
            else:
                blob = obj.get("content")
                # 这条记录的 content 必须**本身就是**那条命令,不能只是提到它。
                # 不加这一条会有一类很尴尬的误命中:一份讨论这段代码的转录里写着这个模式,
                # 于是扫描器把自己的正则源码当成了对话的名字。实测发生过。
                if not isinstance(blob, str) or not blob.lstrip().startswith(_RENAME_HEAD):
                    continue
                m = _RENAME_ARGS.search(blob)
                if m and m.group(1).strip():
                    args = m.group(1).strip()
    except OSError:
        pass
    return {"custom": custom, "renameArgs": args, "ai": ai}


def read_one(path: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    st = path.stat()
    size = st.st_size
    with open(path, "rb") as fh:
        head = fh.read(HEAD_BYTES)
        if size > HEAD_BYTES + TAIL_BYTES:
            fh.seek(-TAIL_BYTES, os.SEEK_END)
            tail = fh.read()
            partial = True
        else:
            tail = b""
            partial = False
    dec = lambda b: b.decode("utf-8", "replace")

    cwd = slug = summary = first_msg = None
    first_ts = last_ts = None
    human = 0
    for entry in _iter_json(dec(head)):
        if cwd is None and entry.get("cwd"):
            cwd = entry["cwd"]
        if slug is None and entry.get("slug"):
            slug = entry["slug"]
        if summary is None and entry.get("type") == "summary":
            summary = (entry.get("summary") or "").strip() or None
        ts = entry.get("timestamp")
        if ts:
            if first_ts is None:
                first_ts = ts
            last_ts = ts
        if entry.get("type") == "user" and not entry.get("isSidechain"):
            txt = _typed_text(entry)
            if txt and not _looks_injected(txt):
                human += 1
                if first_msg is None:
                    first_msg = txt
    for entry in _iter_json(dec(tail)):
        if entry.get("timestamp"):
            last_ts = entry["timestamp"]
        if summary is None and entry.get("type") == "summary":
            summary = (entry.get("summary") or "").strip() or None
        if cwd is None and entry.get("cwd"):
            cwd = entry["cwd"]
        if slug is None and entry.get("slug"):
            slug = entry["slug"]

    named = read_titles(path)
    renamed = named["custom"] or named["renameArgs"]
    if renamed:
        title, src = renamed, "rename"
    elif named["ai"]:
        title, src = named["ai"], "ai-title"
    elif summary:
        title, src = summary, "summary"
    elif first_msg:
        title, src = " ".join(first_msg.split())[:110], "first-message"
    elif slug:
        title, src = slug, "slug"
    else:
        title, src = path.stem[:8], "id"

    return {
        "id": path.stem,
        "file": str(path),
        "cwd": cwd,
        "title": title,
        "titleFrom": src,
        # 标题和正文是两样东西:改过名之后标题不再透露这场对话在说什么,
        # 所以第一条消息单独留着,界面上两行都显示。
        "preview": (" ".join(first_msg.split())[:120] if first_msg else None),
        "renamed": renamed,
        "aiTitle": named["ai"],
        "slug": slug,
        "bytes": size,
        "mtime": st.st_mtime,
        "ageHours": round((now - st.st_mtime) / 3600.0, 1),
        "firstTs": first_ts,
        "lastTs": last_ts,
        # 只数了读到的那部分。没读完就说没读完,不把两头的计数报成确定的总数。
        "humanSeen": human,
        "partial": partial,
    }


def _load_cache(p: Path | None) -> dict:
    if not p or not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def scan(root: str | None = None, cache: str | None = None,
         now: float | None = None, limit_per_group: int = 40) -> dict:
    raw = root if root is not None else os.environ.get("TASK_CONSOLE_SESSIONS")
    if not raw:
        return {"available": False,
                "reason": "没有设 TASK_CONSOLE_SESSIONS,对话历史这一栏是「未检查」。"}
    base = Path(os.path.expanduser(raw))
    if not base.is_dir():
        return {"available": False, "reason": f"目录不存在: {base}"}

    now = time.time() if now is None else now
    cpath = Path(os.path.expanduser(cache)) if cache else (
        Path(os.path.expanduser(os.environ["TASK_CONSOLE_CONVO_CACHE"]))
        if os.environ.get("TASK_CONSOLE_CONVO_CACHE") else None)
    cached = _load_cache(cpath)
    fresh, hits, misses = {}, 0, 0

    rows, failed = [], 0
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl")):
            try:
                st = f.stat()
            except OSError:
                failed += 1
                continue
            key = str(f)
            # 缓存只在 mtime 和大小都没变时算命中。只看 mtime 会漏掉同秒内的改写。
            prev = cached.get(key)
            if prev and prev.get("_m") == st.st_mtime and prev.get("bytes") == st.st_size:
                rec = dict(prev)
                rec["ageHours"] = round((now - st.st_mtime) / 3600.0, 1)
                hits += 1
            else:
                try:
                    rec = read_one(f, now)
                except OSError:
                    failed += 1
                    continue
                rec["_m"] = st.st_mtime
                misses += 1
            fresh[key] = rec
            rows.append(rec)

    if cpath:
        try:
            cpath.parent.mkdir(parents=True, exist_ok=True)
            cpath.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    groups: dict[str, list] = {}
    for r in rows:
        # cwd 读不到时单独归一组并说明,不塞进某个看起来合理的目录里。
        groups.setdefault(r["cwd"] or "(转录里没有记录目录)", []).append(r)

    out = []
    for cwd, items in groups.items():
        items.sort(key=lambda x: -(x["mtime"]))
        out.append({
            "cwd": cwd,
            "count": len(items),
            "humanish": sum(1 for x in items if x["humanSeen"] >= 2),
            "bytes": sum(x["bytes"] for x in items),
            "newest": items[0]["mtime"],
            "shown": items[:limit_per_group],
            "truncated": len(items) > limit_per_group,
        })
    out.sort(key=lambda g: -g["newest"])

    return {
        "available": True,
        "root": str(base),
        "groups": out,
        "summary": {
            "files": len(rows),
            "groups": len(out),
            "humanish": sum(g["humanish"] for g in out),
            "bytes": sum(g["bytes"] for g in out),
            "cacheHits": hits,
            "cacheMisses": misses,
            "cached": bool(cpath),
            # 读不动的文件要报出来。悄悄跳过会让「没有这些对话」和「我没读到」变成同一个数字。
            "unreadable": failed,
        },
    }
