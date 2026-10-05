#!/usr/bin/env python3
"""Plan owner requests through installed llmcall policy and execute deterministic actions.

Pool operations affect only shown IDs. Work operations enqueue or stop execution. Persisted authorization supports read-only reconciliation after a successful mutation whose outcome could not be recorded. Confirmations describe actual outcomes."""
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
def call_chain(prompt, log=None):
    """Compatibility seam returning text; policy belongs to installed llmcall."""
    import llmcall
    result = llmcall.call(prompt, mode="judge", log=log)
    return result.text if result and not getattr(result, "error", None) else None

import agent_task  # noqa: E402
import agent_tick  # noqa: E402
import relay       # noqa: E402

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REMINDER = os.path.join(_HERE, "reminder.py")
import private_data
import inbound
import store
_STATE_DIR = None

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


def _active_items(source=None, *, include_context=False):
    args = ["list", "--active"]
    if source:
        args += ["--source", source]
    items = agent_task.query_items(_rem, *args)
    if include_context:
        from creation_guard import source_role
        return [{key: it.get(key) for key in ('id', 'title', 'description', 'source', 'due_at', 'state')}
                for it in items if source_role(it.get('source'), it.get('idempotency_key'), it.get('kind')) == 'tracked_item']
    return [{"id": it["id"], "title": it.get("title") or ""} for it in items]


def get_state(cfg):
    if cfg["kind"] == "pool":
        return _active_items(source="email-monitor")
    if cfg["kind"] == "reminder":
        return _active_items()
    return _active_items(include_context=True)


def get_work():
    """Work orders the 'stop' op may target. Deliberately NOT filtered by stream: the user says
    "stop" in whichever channel they happen to be reading."""
    return agent_task.running()


def build_prompt(stream, cfg, reply, items, work=None):
    listing = "\n".join("  %s | %s | source=%s | due=%s | %s" %
                         (it['id'], it['title'], it.get('source'), it.get('due_at'),
                          str(it.get('description') or '')[:2000]) for it in items) or "  (none)"
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
        "- Compare the obligation, source and occurrence before creating. For a shown existing item,\n"
        "  use 'update' with its exact 'id' and a 'note' to append new instructions once.\n"
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
        '{"op":"update","id":"...","note":"补充要求"},'
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


def _action_identity(stream, msg_id, index, action):
    # Saved plans sort object keys; identity must survive that serialization boundary.
    encoded = json.dumps([stream, str(msg_id), index, action], ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode('utf-8')
    return "dispatch:" + hashlib.sha256(encoded).hexdigest()


def _saved_plan_identity_matches(record, keys, actions):
    """Older pending plans cannot prove the insertion order used by their first attempt."""
    outcomes = record.get('outcomes')
    if not isinstance(outcomes, dict):
        return False
    if record.get('action_identity_version') == 2:
        return record.get('action_keys') == keys and set(outcomes).issubset(keys)
    if 'action_identity_version' in record or 'action_keys' in record:
        return False
    # An exact complete legacy receipt can be displayed without executing any action.
    return all(isinstance(outcomes.get(key), dict)
               and outcomes[key].get('status') in ('succeeded', 'rejected')
               and isinstance(action, dict)
               and outcomes[key].get('op') == str(action.get('op') or '').lower()
               for key, action in zip(keys, actions))


def _existing_work_receipt(key, origin, request):
    """Reconcile an already published work request without enqueuing or changing it."""
    item = store.creation_receipt_item(agent_task.WORK_SOURCE, key)
    if not item:
        return None
    agent_task._work_item(item)
    ext = item.get('ext') or {}
    expected = hashlib.sha256(request.encode('utf-8')).hexdigest()
    if (item.get('source') != agent_task.WORK_SOURCE or not isinstance(ext, dict)
            or ext.get('x_console_origin_item') != origin
            or ext.get(agent_task.EXT_REQUEST_SHA) != expected
            or ext[agent_task.EXT_STATE] == 'preparing'):
        raise ValueError('previously authorized work receipt does not match the saved action')
    if agent_task.read_request(item) != request:
        raise ValueError('previously authorized work request is not confirmed')
    return item


def _work_generation(item):
    ext = item.get('ext') or {}
    generation = ext.get(agent_task.EXT_GENERATION, 0) if isinstance(ext, dict) else None
    return generation if type(generation) is int and generation >= 0 else None


def _stopped_work_receipt(item_id, generation):
    """Confirm the originally authorized generation without stopping a replacement."""
    current = agent_task.get(item_id)
    if not current or current.get('state') != 'cancelled':
        return False
    operation = agent_task.operation(item_id)
    if operation is not None:
        return (operation['generation'] == generation and operation['outcome'] == 'cancelled'
                and operation['cleanup_state'] == 'quiescent')
    return generation == 0 and agent_task.exec_state(current) == agent_task.STATE_FAILED


def execute(stream, cfg, plan, items, log=None, work=None, msg_id=None,
            saved_outcomes=None, save_outcome=None, inbound_id=None, authorized_ids=None,
            authorized_work_ids=None, request_id=None, channel_id=None,
            authorized_work_generations=None):
    allowed = {it["id"] for it in items}
    running_ids = {it["id"] for it in (work or [])}
    if authorized_ids is not None:
        allowed.intersection_update(authorized_ids)
    if authorized_work_ids is not None:
        running_ids.intersection_update(authorized_work_ids)
    result = {"done": 0, "snoozed": 0, "created": 0, "updated": 0, "reused": 0, "enqueued": [],
              "stopped": [], "skipped": [], "failed": [], "outcomes": []}
    for index, action in enumerate(plan.get("actions") or []):
        op = str(action.get("op") or "").lower() if isinstance(action, dict) else ""
        source_identity = (inbound_id if inbound_id is not None else
                           inbound.identity('request', channel_id or stream, request_id) if request_id is not None else msg_id)
        key = _action_identity(stream, source_identity, index, action)
        outcome = (saved_outcomes or {}).get(key)
        if not outcome or outcome["status"] == "failed":
            outcome = {"op": op, "status": "rejected", "reason": "invalid or unsupported action"}
            try:
                if op == "agent":
                    request = str(action.get("request") or "").strip()
                    origin = action.get('id')
                    if origin is not None and origin not in allowed:
                        if origin in (authorized_ids or []) and source_identity is not None and request:
                            item = _existing_work_receipt(key, origin, request)
                            outcome = ({'op': op, 'status': 'succeeded', 'id': item['id']} if item
                                       else {'op': op, 'status': 'failed',
                                             'reason': 'previously authorized work is not confirmed'})
                        else:
                            outcome['reason'] = 'agent origin was not shown to the planner'
                    elif request:
                        item = agent_task.enqueue(stream, request, workspace=action.get("workspace"),
                                                  msg_id=msg_id,
                                                  idempotency_key=key if source_identity is not None else None,
                                                  **({'origin_item_id': origin} if origin is not None else {}))
                        outcome = ({"op": op, "status": "succeeded", "id": item["id"]} if item.get("id")
                                   else {"op": op, "status": "failed", "reason": str(item.get("_err") or "missing work receipt")})
                elif op == "stop":
                    iid = str(action.get("id") or "").strip()
                    if authorized_work_ids is not None:
                        authorized = set(authorized_work_ids)
                        targets = sorted(authorized) if iid == "*" else [iid] if iid in authorized else []
                    else:
                        targets = sorted(running_ids) if iid == "*" else [iid] if iid in running_ids else []
                    if targets:
                        generations = (authorized_work_generations if authorized_work_ids is not None else
                                       {row['id']: _work_generation(row) for row in work or []})
                        stops = []
                        for target in targets:
                            generation = generations.get(target) if isinstance(generations, dict) else None
                            if type(generation) is not int or generation < 0:
                                stops.append({'id': target, 'stopped': False, 'status': 'reconcile'})
                            elif target in running_ids:
                                stops.extend(agent_tick.stop(target, note="user asked to stop",
                                                             expected_generation=generation))
                            else:
                                stops.append({"id": target,
                                              "stopped": _stopped_work_receipt(target, generation)})
                        successful = [row["id"] for row in stops if row.get("stopped") is True]
                        outcome = {"op": op, "status": "succeeded" if stops and len(successful)==len(stops) else "failed",
                                   "ids": successful, "reason": "stop remains unresolved"}
                elif op in ("done", "dismiss", "snooze"):
                    iid = action.get("id")
                    if iid not in allowed:
                        if op in ("done", "dismiss") and iid in (authorized_ids or []):
                            response = _rem("get", "--id", iid)
                            current = response.get("item") or {}
                            reconciled = (not response.get("_err") and current.get("id") == iid
                                          and current.get("state") == "done")
                            outcome = {"op": op, "id": iid,
                                       "status": "succeeded" if reconciled else "failed",
                                       "reason": "" if reconciled else "previously authorized completion is not confirmed"}
                        else:
                            outcome["reason"] = "item %s was not shown to the planner" % str(iid)[:8]
                    elif op == "snooze" and not action.get("until"):
                        outcome["reason"] = "snooze requires until"
                    else:
                        args = (["snooze", "--id", iid, "--until", action["until"]] if op == "snooze"
                                else ["done", "--id", iid])
                        response = _rem(*args)
                        item = response.get("item") or {}
                        succeeded = bool(item) and not response.get("_err")
                        if op != "snooze":
                            succeeded = succeeded and item.get("state") == "done"
                        outcome = {"op": op, "status": "succeeded" if succeeded else "failed",
                                   "id": iid, "reason": str(response.get("_err") or "mutation did not return its expected item")}
                elif op == 'update':
                    iid, note = action.get('id'), str(action.get('note') or '').strip()
                    if iid not in allowed and iid in (authorized_ids or []) and source_identity is not None and note:
                        item = store.dispatch_note_receipt(iid, note, key)
                        outcome = ({'op': op, 'status': 'succeeded', 'id': item['id']} if item
                                   else {'op': op, 'status': 'failed',
                                         'reason': 'previously authorized follow-up is not confirmed'})
                    elif iid not in allowed or not note:
                        outcome['reason'] = 'follow-up requires a shown item and a nonempty note'
                    else:
                        item = store.append_dispatch_note(iid, note, key)
                        outcome = {'op': op, 'status': 'succeeded', 'id': item['id']}
                elif op == "create":
                    title = str(action.get("title") or "").strip()
                    if title:
                        args = ["ensure" if source_identity is not None else "add", "--title", title, "--kind", "task"]
                        if source_identity is not None:
                            args += ["--idempotency-key", key, "--if-exists", "return"]
                        if cfg["kind"] == "pool":
                            args += ["--source", "email-monitor", "--ext", json.dumps({
                                "x_email_monitor_thread_key": _thread_key(title),
                                "x_email_monitor_msg_count": 1}, ensure_ascii=False)]
                        else:
                            args += ["--source", "agent-center:%s" % stream]
                        if action.get("due_at"):
                            args += ["--due-at", action["due_at"]]
                        response = _rem(*args)
                        item = response.get("item") or {}
                        outcome = {"op": op, "status": "succeeded" if item.get("id") and not response.get("_err") else "failed",
                                   "id": item.get("id"), "decision": response.get('decision', 'created'),
                                   "reason": str(response.get("_err") or "missing creation receipt")}
            except Exception as error:
                outcome = {"op": op, "status": "failed", "reason": type(error).__name__ + ": " + str(error)}
            if save_outcome:
                save_outcome(key, outcome)
        result["outcomes"].append(outcome)
        if outcome["status"] != "succeeded":
            marker = op + "?" + outcome["reason"]
            result["skipped"].append(marker)
            if outcome["status"] == "failed":
                result["failed"].append(marker)
        elif op in ("done", "dismiss"):
            result["done"] += 1
        elif op == "snooze":
            result["snoozed"] += 1
        elif op == "create":
            result['created' if outcome.get('decision', 'created') == 'created' else 'reused'] += 1
        elif op == 'update':
            result['updated'] += 1
        elif op == "agent":
            result["enqueued"].append(outcome["id"])
        elif op == "stop":
            result["stopped"].extend(outcome["ids"])
    if log:
        log("execute[%s]: %s" % (stream, json.dumps(result, ensure_ascii=False)))
    return result


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
        return True
    if channel_id and not _has_webhook(stream):
        delivered = relay.send(text, channel_id=str(channel_id))
    else:
        delivered = relay.relay(stream, text)
    if delivered is False:
        if log:
            log("confirmation delivery failed; durable work remains pending")
        return False
    return True


def _dispatch(stream, reply, log, post, channel_id, msg_id, record=None, save=None, inbound_id=None):
    cfg = STREAMS.get(stream, _DEFAULT_CFG)
    items, work = get_state(cfg), get_work()
    plan = record.get("plan") if record is not None else None
    if plan is None:
        prompt = build_prompt(stream, cfg, reply, items, work)
        plan = _extract_json(call_chain(prompt, log=log))
        if not isinstance(plan, dict) or not isinstance(plan.get("actions"), list):
            _post(stream, "自动解析失败，本次没有执行操作。", post, log, channel_id)
            return False
        if record is not None:
            record["plan"] = plan
            record["authorized_ids"] = [item["id"] for item in items]
            record["authorized_work_ids"] = [item["id"] for item in work]
            record['authorized_work_generations'] = {item['id']: _work_generation(item) for item in work}
            record['action_identity_version'] = 2
            record['action_keys'] = [_action_identity(stream, inbound_id, index, action)
                                     for index, action in enumerate(plan['actions'])]
            save()
    if record is not None:
        if not isinstance(plan, dict) or not isinstance(plan.get('actions'), list):
            _post(stream, "已保存的操作计划损坏，需要核对记录后再处理。", post, log, channel_id)
            return False
        keys = [_action_identity(stream, inbound_id, index, action)
                for index, action in enumerate(plan['actions'])]
        if not _saved_plan_identity_matches(record, keys, plan['actions']):
            _post(stream, "已保存的操作身份无法确认，需要核对已执行结果后再处理。", post, log, channel_id)
            return False
    def save_outcome(key, outcome):
        record["outcomes"][key] = outcome
        save()
    result = execute(stream, cfg, plan, items, log=log, work=work, msg_id=msg_id,
                     saved_outcomes=record["outcomes"] if record is not None else None,
                     save_outcome=save_outcome if record is not None else None,
                     inbound_id=inbound_id,
                     authorized_ids=record.get("authorized_ids", []) if record is not None else None,
                     authorized_work_ids=record.get("authorized_work_ids", []) if record is not None else None,
                     authorized_work_generations=record.get('authorized_work_generations') if record is not None else None)
    confirm = "已执行：完成%d、推迟%d、新建%d、更新%d、复用%d、排队%d、停止%d。" % (
        result["done"], result["snoozed"], result["created"], result["updated"], result["reused"],
        len(result["enqueued"]), len(result["stopped"]))
    updated = [row['id'] for row in result['outcomes'] if row['status'] == 'succeeded' and row['op'] == 'update']
    reused = [row['id'] for row in result['outcomes'] if row['status'] == 'succeeded' and row['op'] == 'create'
              and row.get('decision', 'created') != 'created']
    if updated:
        confirm += "\n已更新：" + ", ".join(updated)
    if reused:
        confirm += "\n已复用：" + ", ".join(reused)
    if result["enqueued"]:
        confirm += "\n已派活工作单：" + ", ".join(result["enqueued"])
    if result["stopped"]:
        confirm += "\n已停止：" + ", ".join(result["stopped"])
    rejected = [row["op"] + "?" + row["reason"] for row in result["outcomes"]
                if row["status"] == "rejected"]
    if rejected:
        confirm += "\n未执行（rejected/skipped）：" + "; ".join(rejected)
    if result["failed"]:
        confirm += "\n失败（failed）：" + "; ".join(result["failed"])
    confirmed = _post(stream, confirm, post, log, channel_id)
    return confirmed is not False and not result["skipped"] and not result["failed"]


def dispatch(stream, reply, log=None, post=True, channel_id=None, msg_id=None, inbound_id=None, request_id=None):
    if request_id is not None and (not isinstance(request_id, str) or not request_id.strip()):
        raise ValueError('request_id must be a nonempty string')
    if inbound_id is None and request_id is not None:
        inbound_id = inbound.identity('request', channel_id or stream, request_id)
    if inbound_id is None:
        inbound_id = (inbound.identity("text", str(channel_id), str(msg_id))
                      if channel_id is not None and msg_id is not None else msg_id)
    if inbound_id is None:
        return _dispatch(stream, reply, log, post, channel_id, msg_id)
    # Durable delivery identity scopes retries; msg_id remains the original source reference.
    with inbound.dispatch_record(stream, inbound_id) as (record, save):
        return _dispatch(stream, reply, log, post, channel_id, msg_id, record, save, inbound_id)


def main():
    ap = argparse.ArgumentParser(prog="dispatch.py")
    ap.add_argument("--stream", required=True)
    ap.add_argument("--reply", default=None, help="reply text; default reads state/<stream>.inbox")
    ap.add_argument('--request-id', help='stable identity of this source occurrence for retries')
    ap.add_argument("--no-post", dest="post", action="store_false", help="dry run: print confirm, do not relay")
    a = ap.parse_args()
    reply = a.reply
    if reply is None:
        p = os.path.join(_STATE_DIR or str(private_data.data_dir()/"state"), "%s.inbox" % a.stream)
        reply = open(p, encoding="utf-8").read() if os.path.exists(p) else ""
    if not reply.strip():
        print(json.dumps({"ok": False, "reason": "empty reply"}))
        return 1
    try:
        ok = dispatch(a.stream, reply, log=lambda m: print(m, file=sys.stderr), post=a.post,
                      request_id=a.request_id)
    except ModuleNotFoundError as error:
        if error.name != 'llmcall':
            raise
        print(json.dumps({
            'ok': False, 'status': 'unavailable', 'error_code': 'ERR_LLM_UNAVAILABLE',
            'message': 'The llmcall package is unavailable in the dispatch interpreter.',
            'action': 'Install llmcall in the interpreter running dispatch.py, then retry this reply.',
        }))
        return 1
    print(json.dumps({"ok": ok}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
