"""对话历史:按对话创建时所在的目录分组,用对话自己的标题当标题。

标题有三个来源,优先级从高到低,而且**结果里永远带着它来自哪一个**:

  summary       会话被压缩时写下的一句话概括,最接近人在恢复列表里看到的那行
  first-message 第一条真人打进去的消息的开头
  slug          自动生成的三词代号

实测这台机器上的转录里 summary 型条目一条都没有(先前以为有,是因为 `"summary"` 这个词
出现在工具结果的字段里,按字符串搜会命中)。读取仍然保留,因为格式会变,而一个只认
当前格式的读取器在格式变化那天会安静地退回到 slug,没有人会注意到。

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

import json
import os
import time
from pathlib import Path

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

    if summary:
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
