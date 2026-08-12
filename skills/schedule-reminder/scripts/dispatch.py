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

TWO OPS DO NOT TOUCH THE POOL AT ALL. 'agent' enqueues a work order for the execution tier
(agent_task/agent_run/agent_tick) and 'stop' cancels a running one. They exist because a bus that
can only mutate records answers "make X stop" with a to-do titled "make X stop", which is what
happened for four days while the thing kept running. 'agent' carries no item id, so it cannot
hallucinate one; 'stop' is validated against the orders that are actually running, the same
allowlist discipline as done/snooze.

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
import agent_task  # noqa: E402
import agent_tick  # noqa: E402
import relay       # noqa: E402

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


def get_work():
    """Work orders the 'stop' op may target. Deliberately NOT filtered by stream: the user says
    "stop" in whichever channel they happen to be reading."""
    try:
        return agent_task.running()
    except Exception:
        return []


def build_prompt(stream, cfg, reply, items, work=None):
    listing = "\n".join("  %s | %s" % (it["id"], it["title"]) for it in items) or "  (none)"
    running = "\n".join("  %s | %s" % (it["id"], it.get("title") or "") for it in (work or [])) \
        or "  (none)"
    return (
        "You process a user's reply in the Agent Center Discord channel '%s' (%s).\n"
        "The user writes natural-language updates. Decide an ACTION PLAN.\n\n"
        "Active items you MAY act on (reference each by its EXACT id):\n%s\n\n"
        "Agent work orders currently RUNNING (the only ids 'stop' may target):\n%s\n\n"
        "User reply:\n%s\n\n"
        "FIRST decide what kind of thing the reply asks for.\n"
        "  A change to the RECORD (this item is handled, postpone it, remember to do this later)\n"
        "    -> 'done' / 'snooze' / 'create'.\n"
        "  A change to the WORLD (make it stop, fix that bug, turn it off, go do it, 别发了,\n"
        "    停掉, 把这个修了, 去做) -> 'agent'. This runs a real agent on this machine.\n"
        "  CRITICAL: answering 'make X stop' or 'fix this bug' with a to-do item is WRONG. Those\n"
        "  are the world, not the record. A to-do is a note that nobody will execute; 'agent' is\n"
        "  the only op that makes something actually happen. When in doubt between 'create' and\n"
        "  'agent' for a request phrased as an instruction, choose 'agent'.\n\n"
        "Rules:\n"
        "- 'done' an item when the reply says it is handled/confirmed/cancelled/ignore/不用管/不急/搞定/已确认.\n"
        "- 'snooze' with an ISO8601 UTC 'until' when the reply asks to postpone/reschedule (推迟/改期).\n"
        "- 'create' a new task ONLY when the reply records something to remember, not something to\n"
        "  do now; 'title' in Simplified Chinese starting with '需回复:' or '待办:'. Never duplicate.\n"
        "- 'agent' when the reply asks for work to be performed. 'request' must restate the ask in\n"
        "  full, with enough context that someone who never read this channel could act on it;\n"
        "  quote the concrete symptom if the reply refers to one. Optional 'workspace' is an\n"
        "  absolute directory path when you know which repository the work belongs in.\n"
        "- 'stop' when the reply asks to abort work in progress (停/别跑了/取消/stop). Its 'id' MUST\n"
        "  come from the running list above; use \"*\" to mean whichever order is running.\n"
        "- Only 'done'/'snooze' items whose id appears in the list above, using the exact id. If a\n"
        "  reply line has no clear matching item, do nothing for it (mention it in confirm).\n"
        "- A line may map to several items only if the user clearly means all of them.\n"
        "Return ONLY compact JSON (no prose, no code fence):\n"
        '{"actions":[{"op":"done","id":"..."},{"op":"snooze","id":"...","until":"2026-..Z"},'
        '{"op":"create","title":"需回复:...","due_at":null},'
        '{"op":"agent","request":"完整复述用户要做的事","workspace":null,"why":"一句话"},'
        '{"op":"stop","id":"..."}],'
        '"confirm":"中文一句话:完成N项(简述)、推迟M项、新建K项、派活K项;未动:…"}\n'
        % (stream, cfg["desc"], listing, running, reply.strip())
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


def execute(stream, cfg, plan, items, log=None, work=None):
    allowed = {it["id"] for it in items}
    running_ids = {it["id"] for it in (work or [])}
    done = snoozed = created = 0
    enqueued, stopped, skipped = [], [], []
    for act in (plan.get("actions") or []):
        op = (act.get("op") or "").lower()
        if op == "agent":
            # No id to validate: an 'agent' op names work, not an existing record, so there is
            # nothing for the model to hallucinate. What IS checked is that it asked for something.
            request = (act.get("request") or "").strip()
            if not request:
                skipped.append("agent?empty")
                continue
            item = agent_task.enqueue(stream, request, workspace=act.get("workspace"))
            if item.get("_err"):
                skipped.append("agent?%s" % str(item["_err"])[:24])
            else:
                enqueued.append(item["id"])
            continue
        if op == "stop":
            iid = (act.get("id") or "").strip()
            if iid != "*" and iid not in running_ids:
                skipped.append("stop?%s" % iid[:8])
                continue
            for r in agent_tick.stop(iid, note="用户在频道里要求停止"):
                stopped.append(r["id"])
            continue
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
        log("execute[%s]: done=%d snooze=%d create=%d agent=%d stop=%d skip=%s"
            % (stream, done, snoozed, created, len(enqueued), len(stopped), skipped))
    return {"done": done, "snoozed": snoozed, "created": created,
            "enqueued": enqueued, "stopped": stopped, "skipped": skipped}


def _has_webhook(stream):
    try:
        return bool(((relay.load_registry().get("streams") or {}).get(stream) or {}).get("webhook"))
    except Exception:
        return False


def _post(stream, text, post, log, channel_id=None):
    """Confirm in the channel the reply came from.

    A registered stream keeps its webhook, which carries the per-stream identity. A channel the bus
    discovered has no webhook, so the confirmation goes over the bot to that channel id. Without
    this it fell back to a DM, and an answer arriving somewhere other than where you asked reads
    as no answer at all."""
    if not post:
        if log:
            log("[no-post] would relay -> %s: %s" % (stream, text))
        return
    if channel_id and not _has_webhook(stream):
        relay.send(text, channel_id=str(channel_id))
    else:
        relay.relay(stream, text)


def dispatch(stream, reply, chain=None, providers=None, timeout=180, log=None, post=True,
             channel_id=None):
    cfg = STREAMS.get(stream, _DEFAULT_CFG)
    items = get_state(cfg)
    work = get_work()
    prompt = build_prompt(stream, cfg, reply, items, work)
    raw = call_chain(prompt, chain=chain, providers=providers, timeout=timeout, log=log)
    plan = _extract_json(raw)
    if not plan:
        _post(stream, "收到你的回复,但自动解析失败,已留待人工处理。原文:%s" % reply.strip()[:200],
              post, log, channel_id)
        if log:
            log("dispatch[%s]: chain/plan failed -> passthrough" % stream)
        return False
    res = execute(stream, cfg, plan, items, log=log, work=work)
    confirm = (plan.get("confirm") or "").strip() or (
        "收到:完成%d、推迟%d、新建%d。" % (res["done"], res["snoozed"], res["created"]))
    # The model writes the summary, but what was DISPATCHED is appended deterministically. A vague
    # confirm must not be able to hide the fact that a real agent is now running on this machine,
    # and the id is what the user needs in order to stop it.
    if res["enqueued"]:
        confirm += "\n🤖 已派活 %d 个工作单:%s。开始执行,完成或卡住都会回这个频道报告(回「停 <id>」可中止)。" % (
            len(res["enqueued"]), ", ".join("`%s`" % i[:8] for i in res["enqueued"]))
    if res["stopped"]:
        confirm += "\n🛑 已停止:%s。" % ", ".join("`%s`" % i[:8] for i in res["stopped"])
    _post(stream, confirm, post, log, channel_id)
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
