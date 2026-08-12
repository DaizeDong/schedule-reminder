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
IN:   user writes in #X --(ingest_tick: poll)--> commands.py claims it?  --yes--> handler answers in #X
                                                                        --no --> dispatch (LLM judge)
                                                                                 -> reminder.py mutations
                                                                                 --(relay confirm)--> #X
```

Streams (Agent Center server): `mail · hotspots · demand · promotion · support · crypto · infra ·
reminders · general · ops`, plus `commands · guestbook · archive` which are registered but opted out
of reading. The aggregated daily summary goes to **Big Brother DM**, not a channel. The bus is
**two-way**: `relay.py` is the egress, `ingest.py`/`commands.py`/`dispatch.py` the ingress.

**One reader, one writer.** Both halves are single points on purpose, and both were once forked:

- Reading forked when a second bot did its own guild sweep on its own timer with its own cursors.
  The two readers kept different channel lists, and a message in one list and not the other was
  consumed by the reader that could not act on it and never seen by the one that could. It left no
  error, no inbox entry and no log line. See *Inbound*.
- Writing forked every time a job needed something the webhook could not do (an attachment, or
  answering in whichever channel asked). Three separate hand written Discord clients grew that way,
  each re-solving the credentials, the multipart encoding and the 403 on a default User-Agent.

Adding a capability to the one egress retires a fork out there; adding a channel to the one
enumeration means nothing has to remember it separately.

## relay.py, the single Discord egress

```
python relay.py send   --stream <name> (--text T | --json '{"content":..,"username":..}')
python relay.py send   --channel-id ID --text T [--file PATH ...]   # bot transport
python relay.py digest --text T        # aggregated summary -> Big Brother DM
python relay.py list                   # configured streams (NEVER prints webhook URLs)
python relay.py health                 # registry sane? (no network, no secrets)
```

- **Two transports, chosen from what the caller asks for, never configured.** `files` given, or
  `channel_id` given → the bot (`registry.reader.bot_token`). Otherwise → the stream's webhook.
  A webhook carries the per-stream identity and needs no permissions, but it is bound to one
  channel and cannot carry a file, so answering where a command was typed and posting an image are
  both impossible on it. Callers never have to know which transport they are on.
- **A bot send has no Big Brother fallback and returns False.** It is addressed at one specific
  channel; silently rerouting "the answer to what you just typed in #here" into a DM is worse than
  a visible failure the caller can report in place.

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

The mirror of `relay.py`: when the user **writes in any channel the bus can read**, that message is
polled, routed, and turned into pool mutations or a rendered answer, then confirmed back. No
separate bot, no new dependency.

```
python ingest.py poll                  # advance each channel's cursor, write <key>.inbox (read-only)
python ingest.py list                  # registered streams AND what guild discovery adds
python dispatch.py --stream <name>     # judge one stream's inbox -> execute -> confirm (--no-post = dry)
python ingest_tick.py                  # scheduled entrypoint: poll -> commands -> dispatch
```

### Which channels are read, and the invariant that comes with it

`ingest.channels()` is the ONE answer, and it is deliberately wider than the registry: every
registered stream whose `inbound` is not false, **plus every other readable text channel in the
guild**, plus the operator's DM (where the digest lands, so where replies to it get typed). A
channel created next month works without anyone remembering to register it.

That width is the fix for a real incident. The registry listed the channels the system PUSHES to;
it never listed the server's own default channel. An instruction typed there was read only by the
backdrop bot, which understood one command prefix, skipped anything else, and advanced its cursor
past it anyway. The bus never looked. The message was not mishandled, it was never SEEN, and that
leaves nothing behind to notice.

So the bus now holds an invariant, and the code is arranged to keep it true:

> **A message the bus reads is either claimed by a handler or written to an inbox. Never neither.**

- `poll_stream` writes the cursor and the inbox in one function, and the inbox records the whole
  batch including messages a handler is about to claim: the durable trace of what was seen is kept
  separately from the decision about what to act on.
- `commands.route` returns `(claimed, remaining)` and the two must add up to the input.
- **Opting out is explicit and survives discovery**: `inbound: false` means the bus does not read
  the channel at all and the guild sweep may not add it back (an archive channel; a reference
  channel full of example commands). `listen: false` means read it, but run no command handlers.
- **Cursors are keyed on the CHANNEL ID**, not the stream name: a discovered channel's name is
  whatever a human typed, and a rename would orphan a name-keyed cursor and replay that channel's
  history. Cursors from the two older name-keyed schemes are adopted once, taking the NEWEST of
  them; whatever the merge stepped over is written to `<key>.migrated.inbox` for a human rather
  than dispatched, because repeating an action already taken is worse than reporting a gap.

### Commands, the deterministic half

Some messages are commands, not conversation. `commands.py` tries them per message BEFORE the
judgment chain, and a claimed message never reaches a model.

```jsonc
// registry.commands
"gradient": {
  "trigger": "^\\s*(?:gradient|bg|背景)\\b",   // python regex, matched per message
  "exec": ["python", "~/path/to/tool.py", "handle"],
  "timeout": 300
}
```

- The bus writes one UTF-8 json object to the handler's **stdin**
  (`{text, channel_id, stream, message_id, timestamp}`) and reads its **exit code**: 0 means it
  answered in the channel, anything else means the bus reports the failure there instead. Nothing
  goes on argv, because Windows PowerShell mangles non-ASCII argv and commands get typed in Chinese.
  For the same reason the child's `PYTHONIOENCODING` is pinned to utf-8.
- A bare `python` in `exec` is rewritten to the running interpreter: a scheduled task's PATH on
  Windows routinely holds only the WindowsApps alias, a stub that resolves and then runs nothing.
- **A failing handler still claims its message.** Re-routing a broken `gradient x3` into a model
  that will file it as a to-do is not a recovery, it is a second wrong answer.
- Registering a command is how a tool gets a Discord front end now. Writing a second poller is not.

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
- **Cursors & inboxes** live in `<state-dir>/<channel-id>.last` and `<state-dir>/<key>.inbox` under
  the Agent Center config dir, where `<key>` is the stream name for registered streams and the
  channel id for discovered ones. First contact with a channel **arms** the cursor (records latest
  id, processes nothing): back-processing a newly discovered channel would replay its whole visible
  history through the judgment chain and could enqueue real work from months-old messages.
- **Schedule**: Windows task **AgentCenterIngestTick** (PT10M) runs `ingest_tick.py`; it supersedes
  the retired ad-hoc `AgentCenterMailTick` (mail-only loop).
- **Owner only, fail closed.** A text reply counts only when its author is
  `registry.big_brother.user_id`, the same person the reaction path has always required. If no owner
  is configured `poll_all` RAISES instead of falling back to "anyone in the channel": these replies
  can start real execution, so an unresolvable owner has to close the gate rather than open it.

## Execution, when a reply asks for something to HAPPEN

The ops above all change a RECORD. `agent` and `stop` change the WORLD, and exist because a bus
without them answers "make X stop" with a to-do titled "make X stop". That is not hypothetical: a
misrouted daily poster survived three objections over four days that way, each one dutifully filed.

```
python agent_tick.py                # scheduled: reap dead runs, then launch at most one
python agent_tick.py --stop <id>    # cancel + kill a tree ('*' = whichever is running)
python agent_run.py --id <id>       # run one order to a terminal state (--no-post = dry)
python agent_task.py list           # the queue, no secrets
```

- **A work order is an ordinary pool item** (`source=agent-center:work`, `due_at` NULL so a running
  order never trips the reminder notifier). `ext` holds only short fixed `x_agent_exec_*` fields;
  the request text, prompts, per-round transcripts and check output are files in a run directory
  under the Agent Center config dir, because `--ext` arrives as a process argument and Windows caps
  a command line near 32767 characters.
- **Judge, then hand off.** `dispatch` decides between record and world, emits
  `{"op":"agent","request":...}` or `{"op":"stop","id":...}`, and the deterministic executor
  enqueues or cancels. `agent` carries no item id, so the anti-hallucination rule simply does not
  apply to it; `stop` is checked against the orders that are actually running. The channel
  confirmation appends the dispatched ids itself, so a vague model summary cannot hide a live agent.
- **A round is act, verify, review, decide.** The agent must return a command that exits non-zero
  when the job is NOT done; `agent_run` executes that command and records the real output. Only a
  passing check reaches an independent reviewer, on a different provider, which answers DONE or
  CONTINUE. A task that genuinely cannot be checked by a command falls back to review alone, and the
  terminal report says so rather than looking like the stronger case.
- **No progress rotates the approach, it does not stop the order.** Three rounds sharing one
  signature (normalized check output plus the content hashes of the changed files) mean the attempt
  is not moving. The next approach gets a fresh directory, a different provider, and the problem
  plus the current state of the world WITHOUT the reasoning that failed. Two rotations that both
  stall end it as stalled. There is no round or wall-clock ceiling; evidence ends a run.
- **Liveness is `(pid, process creation time)`.** Windows recycles pids, so a pid-only probe reads a
  recycled number as the live holder, and `os.kill(pid, 0)` is not an existence check there at all.
  A run whose process is gone is REPORTED and never silently requeued. Kills use the process TREE.
- **The runner is detached** (`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`),
  measured to outlive both a normal parent exit and the scheduler terminating the parent at its
  execution time limit, which is what lets a 2 minute tick own an hours-long job. Do NOT add
  `CREATE_BREAKAWAY_FROM_JOB`: inside a task it raises access denied. Serial by design, one runner at
  a time, because two agents in one working tree corrupt it.
- **Three llmcall deviations, each a measured hazard, all in `agent_run.py`'s header.** Point
  `LLMCALL_AGENT_RUNNER` at the shim BEFORE importing llmcall (it freezes the path at import, and the
  machine default re-runs codex inside the delegate, doubling every side effect). Act on a SINGLE
  provider so the reported provider is the true one. Never use `schema=`/`extract=` with
  `mode="agent"`, since a parse miss re-invokes the provider and re-does the work.
- **Terminal reports carry evidence**, the changed files, the command, its actual output, and the
  reviewer's verdict. The word 已处理 is banned from them by test; it is the word that made four days
  of doing nothing look like four days of handling it.
- **Schedule**: Windows task **AgentCenterWorkTick** (PT2M) runs `agent_tick.py`, separate from the
  inbound tick so neither can starve the other.

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
