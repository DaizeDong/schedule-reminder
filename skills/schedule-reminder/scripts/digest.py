#!/usr/bin/env python3
"""schedule-reminder — daily digest aggregator (the single "当日总结" cron does this).

DESIGN (from skill todo.md: "联动每日的固定定时任务，里面可以增加当日X的总结")
    One daily task. Each *installed* skill registers a "section contributor": a command that prints
    its 当日总结段 (markdown) to stdout. This aggregator runs every enabled contributor, assembles
    one summary, and delivers it via Big Brother DM (relay.digest). Skills that aren't installed are
    simply absent from the contributor list — fully pluggable, exactly as the original design intended.

CONTRIBUTORS FILE (discovery: env AGENT_CENTER_DIGEST, else a digest file in the Agent Center config dir)
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
import private_data

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass



def _path() -> str:
    return os.environ.get("AGENT_CENTER_DIGEST") or str(private_data.config_root()/"digest.json")



class DigestConfigError(ValueError):
    """An existing contributor configuration is unavailable or invalid."""


def _load(*, strict=False) -> dict:
    try:
        with open(_path(), encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError("digest configuration must be an object")
        contributors = value.get("contributors", [])
        if not isinstance(contributors, list) or any(not isinstance(row, dict) for row in contributors):
            raise ValueError("digest contributors must be an array of objects")
        value.setdefault("contributors", [])
        return value
    except FileNotFoundError:
        return {"contributors": []}
    except Exception:
        raise DigestConfigError("existing digest configuration is unreadable or invalid") from None


def _save(d: dict) -> None:
    private_data.prepare_parent(_path())
    tmp = _path() + ".tmp"
    with private_data.open_for_write(tmp, "w", encoding="utf-8") as fh:
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
    """Run enabled contributors and report sections, failures and the complete roster.
    
    The roster distinguishes a successful empty contribution from a failed or disabled source. A host can embed the result without treating absent contributors as a quiet day."""
    d = _load()
    everyone = d.get("contributors", [])
    contribs = [c for c in everyone if c.get("enabled", True)]
    off = [c.get("name", "?") for c in everyone if not c.get("enabled", True)]
    sections, problems, silent = [], [], []
    for c in contribs:
        text, err = _run_contributor(c, now)
        title = c.get("title", c.get("name", "?"))
        if err:
            problems.append("%s: %s" % (c.get("name", "?"), err))
        elif text:
            sections.append("**%s**\n%s" % (title, text))
        else:
            silent.append(c.get("name", "?"))
    roster = {"registered": len(everyone), "enabled": len(contribs),
              "spoke": len(sections), "silent": silent, "disabled": off,
              "failed": [p.split(":", 1)[0] for p in problems]}
    return contribs, sections, problems, roster


def roster_line(roster: dict) -> str:
    """One line naming what was expected and what actually answered.

    Always emitted when anything is registered, including on a perfectly quiet day, because a line
    that only appears when something is wrong trains people to read its absence as good news; the
    absence would be equally consistent with the aggregator not having run at all.
    """
    if not roster.get("registered"):
        # An empty roster is visible output, not an omitted aggregator.
        return "来源 0 —— 没有任何已注册的当日总结贡献者,所以这一段永远是空的。"
    bits = ["来源 %d" % roster["registered"], "有内容 %d" % roster["spoke"]]
    if roster["silent"]:
        bits.append("明确无内容 %d(%s)" % (len(roster["silent"]), "、".join(roster["silent"])))
    if roster["failed"]:
        bits.append("⚠ 没答上来 %d(%s)" % (len(roster["failed"]), "、".join(roster["failed"])))
    if roster["disabled"]:
        bits.append("已停用 %d(%s)" % (len(roster["disabled"]), "、".join(roster["disabled"])))
    return " · ".join(bits)


def run(now: str | None = None, dry_run: bool = False) -> int:
    """Assemble + deliver the standalone 当日总结 to Big Brother (used if NOT folded into another push)."""
    date = (now or "").split("T")[0] if now else None
    header = "📋 当日总结" + (" · " + date if date else "")
    contribs, sections, problems, roster = _assemble(now)
    if not contribs:
        body = header + "\n\n（暂无已注册的当日总结贡献者。skill 安装时会自动注册。）"
    elif not sections:
        body = header + "\n\n（今日各来源无内容。）"
    else:
        body = header + "\n\n" + "\n\n".join(sections)
    # The roster goes in the MESSAGE, not only to #infra. A failure reported on another channel is
    # a failure the reader of this message does not see, and this message is the one that is meant
    # to answer "did everything report today".
    body += "\n\n" + roster_line(roster)

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
    """Print sections and the contributor roster for embedding in a host summary.
    
    Contributor failures are reported to the infrastructure stream; the roster remains visible even when no section has content."""
    _contribs, sections, problems, roster = _assemble(now)
    # ALWAYS write the roster line, even with nothing to report. The host embeds this output
    # conditionally (`if ($skillDigest)`), so printing nothing makes the whole section vanish from
    # the nightly message, and a vanished section is indistinguishable from an aggregator that was
    # never run. Emitting one line means the section is always present and always says what it
    # checked, which is the difference this whole file is about.
    parts = list(sections)
    parts.append(roster_line(roster))
    sys.stdout.write("\n\n".join(parts))
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
    d = _load(strict=True)
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
    d = _load(strict=True)
    before = len(d.get("contributors", []))
    d["contributors"] = [c for c in d.get("contributors", []) if c.get("name") != args.name]
    _save(d)
    print(json.dumps({"ok": True, "removed": before - len(d["contributors"])}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="digest.py", description="Agent Center daily digest aggregator")
    sub = ap.add_subparsers(dest="op", required=True)  # NB: not "cmd", would collide with register --cmd
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
    try:
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
    except DigestConfigError:
        print(json.dumps({"ok": False, "error_code": "ERR_DIGEST_CONFIG",
                          "error": "existing digest configuration is unreadable or invalid"}))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
