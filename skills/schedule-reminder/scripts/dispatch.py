#!/usr/bin/env python3
"""schedule-reminder — Agent Center reply DISPATCH (judge with the LLM chain, execute deterministically).

For a user reply in a stream channel, this:
  1. Gathers the stream's current actionable STATE (active pool items) as (id, title).
  2. Asks the cost-ordered LLM chain (codex -> cc -> claude, read-only) for a JSON ACTION PLAN.
  3. Executes the plan DETERMINISTICALLY via reminder.py, validating every id against the state
     (the model can only touch items it was shown -- no hallucinated ids).
  4. Posts a Chinese confirmation back to the stream channel via relay.py.

Per-stream behaviour (STREAMS): 'pool' (mail -> email-monitor task pool), 'reminder' (the
schedule-reminder base -> done/snooze), 'generic' (create a follow-up task + confirm).

CLI:  dispatch.py --stream mail            # reads mail.inbox from the Agent Center state dir
      dispatch.py --stream mail --reply "..."   # explicit reply text
Stdlib + the shared `llmcall` pip package (call_chain, str|None) + the sibling relay module.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from llmcall import call_chain  # noqa: E402  (patched in tests as dispatch.call_chain)
import relay      # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REMINDER = os.path.join(_HERE, "reminder.py")
_STATE_DIR = os.path.join(os.path.expanduser("~"), ".agent-center", "state")

# kind: pool = email-monitor task pool | reminder = any active reminder | generic = create/ack only
STREAMS = {
    "mail":      {"kind": "pool",     "desc": "重要邮件提醒(回复=对待办邮件任务的状态更新)"},
    "reminders": {"kind": "reminder", "desc": "到期提醒(回复=done/推迟/改期某条提醒)"},
    "hotspots":  {"kind": "generic",  "desc": "前沿商机卡"},
    "demand":    {"kind": "generic",  "desc": "用户需求卡"},
    "promotion": {"kind": "generic",  "desc": "推广告警/漏斗事件"},
    "support":   {"kind": "generic",  "desc": "升级给创始人的提问"},
    "crypto":    {"kind": "generic",  "desc": "链上收益扫描/风险告警"},
    "infra":     {"kind": "generic",  "desc": "健康/预检失败告警"},
}
_DEFAULT_CFG = {"kind": "generic", "desc": "Agent Center 通知"}


def _rem(*args):
    p = subprocess.run([sys.executable, REMINDER, "--actor", "agent-center-dispatch", *args],
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        return {"_err": (p.stderr or p.stdout).strip()}
    try:
        return json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        return {"_err": "unparseable: %s" % (p.stdout or "")[:200]}


def _active_items(source=None):
    items, cursor = [], None
    while True:
        args = ["list", "--active", "--limit", "100"]
        if source:
            args += ["--source", source]
        if cursor:
            args += ["--cursor", cursor]
        r = _rem(*args)
        items += r.get("items", [])
        cursor = r.get("next_cursor")
        if not cursor:
            break
    return [{"id": it["id"], "title": it.get("title") or ""} for it in items]


def get_state(cfg):
    if cfg["kind"] == "pool":
        return _active_items(source="email-monitor")
    if cfg["kind"] == "reminder":
        return _active_items()
    return []  # generic


def build_prompt(stream, cfg, reply, items):
    listing = "\n".join("  %s | %s" % (it["id"], it["title"]) for it in items) or "  (none)"
    return (
        "You process a user's reply in the Agent Center Discord channel '%s' (%s).\n"
        "The user writes natural-language updates to act on their items. Decide an ACTION PLAN.\n\n"
        "Active items you MAY act on (reference each by its EXACT id):\n%s\n\n"
        "User reply:\n%s\n\n"
        "Rules:\n"
        "- 'done' an item when the reply says it is handled/confirmed/cancelled/ignore/不用管/不急/搞定/已确认.\n"
        "- 'snooze' with an ISO8601 UTC 'until' when the reply asks to postpone/reschedule (推迟/改期).\n"
        "- 'create' a new task when the reply states a NEW to-do not already in the list; 'title' in\n"
        "  Simplified Chinese starting with '需回复:' or '待办:'. Do NOT duplicate an existing item.\n"
        "- Only 'done'/'snooze' items whose id appears in the list above, using the exact id. If a\n"
        "  reply line has no clear matching item, do nothing for it (mention it in confirm).\n"
        "- A line may map to several items only if the user clearly means all of them.\n"
        "Return ONLY compact JSON (no prose, no code fence):\n"
        '{"actions":[{"op":"done","id":"..."},{"op":"snooze","id":"...","until":"2026-..Z"},'
        '{"op":"create","title":"需回复:...","due_at":null}],'
        '"confirm":"中文一句话:完成N项(简述)、推迟M项、新建K项;未动:…"}\n'
        % (stream, cfg["desc"], listing, reply.strip())
    )


def _extract_json(text):
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        break
        start = text.find("{", start + 1)
    return None


def _thread_key(title):
    # Stable per-title key so distinct Chinese titles don't collide, and a re-dispatched
    # identical create dedups instead of duplicating in the digest grouping.
    h = hashlib.sha1((title or "").encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")[:24]
    return "manual:%s-%s" % (slug, h) if slug else "manual:%s" % h


def execute(stream, cfg, plan, items, log=None):
    allowed = {it["id"] for it in items}
    done = snoozed = created = 0
    skipped = []
    for act in (plan.get("actions") or []):
        op = (act.get("op") or "").lower()
        if op in ("done", "dismiss"):
            iid = act.get("id")
            if iid in allowed and _rem("done", "--id", iid).get("item", {}).get("state") == "done":
                done += 1
            else:
                skipped.append("done?%s" % (iid or "")[:8])
        elif op == "snooze":
            iid, until = act.get("id"), act.get("until")
            if iid in allowed and until and not _rem("snooze", "--id", iid, "--until", until).get("_err"):
                snoozed += 1
            else:
                skipped.append("snooze?%s" % (iid or "")[:8])
        elif op == "create":
            title = (act.get("title") or "").strip()
            if not title:
                continue
            args = ["add", "--title", title, "--kind", "task"]
            if cfg["kind"] == "pool":
                args += ["--source", "email-monitor",
                         "--ext", json.dumps({"x_email_monitor_thread_key": _thread_key(title),
                                              "x_email_monitor_msg_count": 1}, ensure_ascii=False)]
            else:
                args += ["--source", "agent-center:%s" % stream]
            if act.get("due_at"):
                args += ["--due-at", act["due_at"]]
            if not _rem(*args).get("_err"):
                created += 1
    if log:
        log("execute[%s]: done=%d snooze=%d create=%d skip=%s" % (stream, done, snoozed, created, skipped))
    return {"done": done, "snoozed": snoozed, "created": created, "skipped": skipped}


def _post(stream, text, post, log):
    if post:
        relay.relay(stream, text)
    else:
        if log:
            log("[no-post] would relay -> %s: %s" % (stream, text))


def dispatch(stream, reply, chain=None, providers=None, timeout=180, log=None, post=True):
    cfg = STREAMS.get(stream, _DEFAULT_CFG)
    items = get_state(cfg)
    prompt = build_prompt(stream, cfg, reply, items)
    raw = call_chain(prompt, chain=chain, providers=providers, timeout=timeout, log=log)
    plan = _extract_json(raw)
    if not plan:
        _post(stream, "收到你的回复,但自动解析失败,已留待人工处理。原文:%s" % reply.strip()[:200], post, log)
        if log:
            log("dispatch[%s]: chain/plan failed -> passthrough" % stream)
        return False
    res = execute(stream, cfg, plan, items, log=log)
    confirm = (plan.get("confirm") or "").strip() or (
        "已处理:完成%d、推迟%d、新建%d。" % (res["done"], res["snoozed"], res["created"]))
    _post(stream, confirm, post, log)
    return True


def main():
    ap = argparse.ArgumentParser(prog="dispatch.py")
    ap.add_argument("--stream", required=True)
    ap.add_argument("--reply", default=None, help="reply text; default reads state/<stream>.inbox")
    ap.add_argument("--chain", default=None)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--codex-model", default="gpt-5.6-sol", help="(ignored; model resolves from ~/.codex/config.toml)")
    ap.add_argument("--codex-reasoning", default="max", help="(ignored; effort resolves from ~/.codex/config.toml)")
    ap.add_argument("--claude-model", default="claude-opus-4-8")
    ap.add_argument("--no-post", dest="post", action="store_false", help="dry run: print confirm, do not relay")
    a = ap.parse_args()
    reply = a.reply
    if reply is None:
        p = os.path.join(_STATE_DIR, "%s.inbox" % a.stream)
        reply = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    if not reply.strip():
        print(json.dumps({"ok": False, "reason": "empty reply"}))
        return 1
    providers = {"codex": {"model": a.codex_model, "reasoning": a.codex_reasoning},
                 "cc": {"model": a.claude_model}, "claude": {"model": a.claude_model}}
    chain = [c.strip() for c in a.chain.split(",")] if a.chain else None
    ok = dispatch(a.stream, reply, chain, providers, a.timeout,
                  log=lambda m: print(m, file=sys.stderr), post=a.post)
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
