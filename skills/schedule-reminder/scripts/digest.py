#!/usr/bin/env python3
"""schedule-reminder — daily digest aggregator (the single "当日总结" cron does this).

DESIGN (from skill todo.md: "联动每日的固定定时任务，里面可以增加当日X的总结")
    One daily task. Each *installed* skill registers a "section contributor": a command that prints
    its 当日总结段 (markdown) to stdout. This aggregator runs every enabled contributor, assembles
    one summary, and delivers it via Big Brother DM (relay.digest). Skills that aren't installed are
    simply absent from the contributor list — fully pluggable, exactly as the original design intended.

CONTRIBUTORS FILE (discovery: env AGENT_CENTER_DIGEST, else ~/.agent-center/digest.json)
    {"contributors":[
       {"name":"email","title":"📬 当日邮件","cmd":["python","<abs>/em_summary.py","--section"],
        "timeout":120,"enabled":true},
       ...
    ]}
    A contributor command MUST print its section to stdout and exit 0. Empty stdout => section skipped.
    Failure/timeout/nonzero => that section is skipped and reported to the #infra stream (never aborts
    the whole digest).

CLI
    digest.py run [--now ISO] [--dry-run]   assemble + deliver (dry-run prints, no delivery)
    digest.py list                          show contributors (no secrets)
    digest.py register --name N --title T --cmd 'argv...' [--timeout S] [--disabled]
    digest.py unregister --name N

ROBUSTNESS / SECRETS
    Missing contributors file => no-op with a clear note. Never reads or prints any webhook/token.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

_DEFAULT = os.path.join(os.path.expanduser("~"), ".agent-center", "digest.json")


def _path() -> str:
    return os.environ.get("AGENT_CENTER_DIGEST") or _DEFAULT


def _load() -> dict:
    try:
        with open(_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            return {"contributors": []}
        d.setdefault("contributors", [])
        return d
    except FileNotFoundError:
        return {"contributors": []}
    except Exception as e:
        sys.stderr.write("digest: contributors file unreadable (%s)\n" % e)
        return {"contributors": []}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(_path()), exist_ok=True)
    tmp = _path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, _path())


def _relay():
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import relay  # noqa: E402  (local sibling)
    return relay


def _run_contributor(c: dict, now: str | None) -> tuple[str, str | None]:
    """Return (section_text, error). section_text='' means nothing to contribute."""
    cmd = c.get("cmd")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd, posix=(os.name != "nt"))
    if not cmd:
        return "", "no cmd"
    env = dict(os.environ)
    if now:
        env["SCHEDULE_NOW"] = now
    # Force child Python stdio to UTF-8 so emoji/Chinese sections survive piping on Windows (GBK hosts).
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=c.get("timeout", 120), env=env,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return "", "timeout"
    except Exception as e:
        return "", str(e)
    if r.returncode != 0:
        return "", "exit %d: %s" % (r.returncode, (r.stderr or "").strip()[:160])
    return (r.stdout or "").strip(), None


def _assemble(now: str | None):
    """Run every enabled contributor; return (sections, problems). No I/O side effects."""
    d = _load()
    contribs = [c for c in d.get("contributors", []) if c.get("enabled", True)]
    sections, problems = [], []
    for c in contribs:
        text, err = _run_contributor(c, now)
        title = c.get("title", c.get("name", "?"))
        if err:
            problems.append("%s: %s" % (c.get("name", "?"), err))
        elif text:
            sections.append("**%s**\n%s" % (title, text))
    return contribs, sections, problems


def run(now: str | None = None, dry_run: bool = False) -> int:
    """Assemble + deliver the standalone 当日总结 to Big Brother (used if NOT folded into another push)."""
    date = (now or "").split("T")[0] if now else None
    header = "📋 当日总结" + (" · " + date if date else "")
    contribs, sections, problems = _assemble(now)
    if not contribs:
        body = header + "\n\n（暂无已注册的当日总结贡献者。skill 安装时会自动注册。）"
    elif not sections:
        body = header + "\n\n（今日各来源无内容。）"
    else:
        body = header + "\n\n" + "\n\n".join(sections)

    relay = _relay()
    if dry_run:
        print(body)
        if problems:
            print("\n[problems -> would报到 #infra] " + "; ".join(problems))
        return 0
    ok = relay.digest(body)
    if problems:
        relay.relay("infra", "每日总结聚合：部分来源失败 -> " + "; ".join(problems), username="digest")
    return 0 if ok else 1


def collect(now: str | None = None) -> int:
    """Print ONLY the assembled skill sections (no header, no send) for embedding into an existing
    daily push (e.g. sync-config-to-backup.ps1's single merged Notify). Empty output if nothing to
    contribute, so the host push can conditionally include it. Contributor failures are reported to
    #infra (non-fatal) just like run()."""
    _contribs, sections, problems = _assemble(now)
    if sections:
        sys.stdout.write("\n\n".join(sections))
    if problems:
        try:
            _relay().relay("infra", "当日总结 collect：部分来源失败 -> " + "; ".join(problems), username="digest")
        except Exception:
            pass
    return 0


def _cmd_list() -> int:
    d = _load()
    out = [{k: v for k, v in c.items()} for c in d.get("contributors", [])]
    print(json.dumps({"path": _path(), "contributors": out}, ensure_ascii=False, indent=2))
    return 0


def _cmd_register(args) -> int:
    d = _load()
    cmd = shlex.split(args.cmd, posix=(os.name != "nt")) if isinstance(args.cmd, str) else args.cmd
    entry = {"name": args.name, "title": args.title, "cmd": cmd,
             "timeout": args.timeout, "enabled": not args.disabled}
    cons = [c for c in d.get("contributors", []) if c.get("name") != args.name]
    cons.append(entry)
    d["contributors"] = cons
    _save(d)
    print(json.dumps({"ok": True, "registered": args.name, "count": len(cons)}, ensure_ascii=False))
    return 0


def _cmd_unregister(args) -> int:
    d = _load()
    before = len(d.get("contributors", []))
    d["contributors"] = [c for c in d.get("contributors", []) if c.get("name") != args.name]
    _save(d)
    print(json.dumps({"ok": True, "removed": before - len(d["contributors"])}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="digest.py", description="Agent Center daily digest aggregator")
    sub = ap.add_subparsers(dest="op", required=True)  # NB: not "cmd" — would collide with register --cmd
    p_run = sub.add_parser("run")
    p_run.add_argument("--now", default=None)
    p_run.add_argument("--dry-run", action="store_true")
    p_col = sub.add_parser("collect", help="print assembled sections only (for embedding), no send")
    p_col.add_argument("--now", default=None)
    sub.add_parser("list")
    p_reg = sub.add_parser("register")
    p_reg.add_argument("--name", required=True)
    p_reg.add_argument("--title", required=True)
    p_reg.add_argument("--cmd", required=True)
    p_reg.add_argument("--timeout", type=int, default=120)
    p_reg.add_argument("--disabled", action="store_true")
    p_unreg = sub.add_parser("unregister")
    p_unreg.add_argument("--name", required=True)
    args = ap.parse_args(argv)
    if args.op == "run":
        return run(args.now, args.dry_run)
    if args.op == "collect":
        return collect(args.now)
    if args.op == "list":
        return _cmd_list()
    if args.op == "register":
        return _cmd_register(args)
    if args.op == "unregister":
        return _cmd_unregister(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
