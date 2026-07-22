# Agent Center, unified relay + daily digest (frozen surface)

schedule-reminder is the **single backend** every other skill routes through: state (the reminder
contract), **outbound Discord** (`relay.py`), and the **daily 当日总结** (`digest.py`). Downstream
skills call these via subprocess and never re-implement transport or scheduling.

> Design law (same as the base): **the contract is the surface, not the transport.** Skills depend on
> `relay.py <stream>` / `digest.py`, never on webhooks, bots, or the registry file directly, so the
> Discord wiring can change forever without touching any skill.

## Topology

```
OUT:  each skill  --(relay.py send --stream X)-->  Agent Center #X channel  (per-stream webhook + identity)
      each skill  --(digest section contributor)-->  digest.py  --(one summary)-->  Big Brother DM
      schedule-reminder tick  --(relay.py send --stream reminders)-->  #reminders
IN:   user reply in #X  --(ingest_tick: poll -> dispatch)-->  reminder.py mutations  --(relay confirm)--> #X
```

Streams (Agent Center server): `mail · hotspots · demand · promotion · support · crypto · infra ·
reminders`. The aggregated daily summary goes to **Big Brother DM**, not a channel. The bus is
**two-way**: `relay.py` is the egress, `ingest.py`/`dispatch.py` the ingress (see *Inbound* below).

## relay.py, the single Discord egress

```
python relay.py send   --stream <name> (--text T | --json '{"content":..,"username":..}')
python relay.py digest --text T        # aggregated summary -> Big Brother DM
python relay.py list                   # configured streams (NEVER prints webhook URLs)
python relay.py health                 # registry sane? (no network, no secrets)
```

- **Registry (secrets; never in THIS public repo)**: discovery = env `AGENT_CENTER_CONFIG`, else a
  registry file in the Agent Center config dir (outside this repo). Shape:
  `{"streams":{"<name>":{"webhook":"...","username":"..."}},
  "reader":{"bot_token":"..."}, "big_brother":{...}}`. `reader.bot_token` is the canonical Discord
  bot token the inbound ingest reads. That config dir is version-controlled in a **private**
  companion repo for backup + portability, secrets live there, never here. See `deployment.md`.
- **Per-stream identity**: each message sets `username` so a stream shows its own name/avatar.
- **Fallback**: unknown stream / missing registry → delivered to Big Brother DM (prefixed
  `[stream]`) so nothing is ever silently lost.
- **Gotcha (encoded in code)**: Discord/Cloudflare 403s the default urllib User-Agent, `relay.py`
  always sends a real `User-Agent`.
- **Test seam**: `AGENT_CENTER_RELAY_DRYRUN=1` skips the network.

## digest.py, the one daily 当日总结

One daily task aggregates every *installed* skill's section into a single summary.

```
python digest.py run [--now ISO] [--dry-run]
python digest.py register --name N --title T --cmd 'argv...' [--timeout S] [--disabled]
python digest.py unregister --name N
python digest.py list
```

- **Contributors file**: discovery = env `AGENT_CENTER_DIGEST`, else a digest file in the Agent Center config dir.
- **A contributor** is a command that prints its 当日总结段 (markdown) to stdout and exits 0. Empty
  stdout → section skipped. Failure/timeout/nonzero → section skipped and reported to `#infra`
  (never aborts the whole digest). Child stdio is forced to UTF-8 (Windows GBG hosts otherwise
  mangle emoji/Chinese).
- **Pluggable**: a skill registers its contributor at install time; uninstalled skills are simply
  absent. This is exactly skill todo.md's "如果这个 skill 安装了，则联动每日的固定定时任务".

## Inbound, user replies become actions (two-way)

The mirror of `relay.py`: when the user **replies in any stream channel**, that reply is polled,
judged, and turned into pool mutations, then confirmed back, no separate bot, no new dependency.

```
python ingest.py poll                 # advance per-stream cursor, write <stream>.inbox (read-only)
python dispatch.py --stream <name>     # judge one stream's inbox -> execute -> confirm (--no-post = dry)
python ingest_tick.py                  # scheduled entrypoint: poll_all + dispatch each new reply
```

- **Judge, then execute (two-phase, anti-hallucination).** `dispatch.py` gathers the stream's
  actionable state (active pool items as `id | title`), asks the **cost-ordered LLM chain**
  (`llm_chain.py`: **codex → cc → claude**, read-only) for a compact JSON *action plan*
  `{actions:[{op:done|snooze|create,...}], confirm}`, then a **deterministic** executor runs it via
  `reminder.py`. The executor only touches ids that were shown to the model, a hallucinated id is
  silently skipped, never acted on.
- **Per-stream handler** (`STREAMS` in `dispatch.py`): `mail` → reconcile the **email-monitor** task
  pool (done/snooze/create with `source=email-monitor`); `reminders` → done/snooze any active
  reminder; every other stream → generic create-a-followup + confirm (`source=agent-center:<stream>`).
- **`llm_chain.call_chain(prompt, chain, providers)`** is the reusable primitive for **all** headless
  judgement calls in this skill: first non-empty answer wins, falls through on failure, deterministic
  no-op if the whole chain is down. codex uses `-s read-only --skip-git-repo-check` (the judge never
  needs write access). Use it, don't re-spawn models ad hoc.
- **User vs bot.** `ingest.py` counts a message as a user reply only when it is neither `author.bot`
  nor a `webhook_id` post, so the skill's own relay/digest confirmations never feed back on
  themselves. Bot token: `registry.reader.bot_token`, else the legacy notifier config file.
  Same urllib `User-Agent` gotcha as relay (Discord 403s the default).
- **Cursors & inboxes** live in `<state-dir>/<stream>.last` / `.inbox` under the Agent Center config dir. First contact with
  a stream **arms** the cursor (records latest id, processes nothing), no history replay.
- **Schedule**: Windows task **AgentCenterIngestTick** (PT10M) runs `ingest_tick.py`; it supersedes
  the retired ad-hoc `AgentCenterMailTick` (mail-only loop).

## How a downstream skill integrates (copy-paste)

```python
import subprocess, sys, os
REMINDER_DIR = os.path.join(os.path.expanduser("~/.claude/skills/schedule-reminder"), "scripts")  # or probe
def push(stream, text, username=None):
    cmd = [sys.executable, os.path.join(REMINDER_DIR, "relay.py"), "send", "--stream", stream, "--text", text]
    if username: cmd += ["--username", username]
    return subprocess.run(cmd).returncode == 0
```

Register a daily section at install:

```
python <reminder>/scripts/digest.py register --name hotspots --title "💡 当日商机" \
    --cmd "python <skill>/scripts/digest.py --section"
```
