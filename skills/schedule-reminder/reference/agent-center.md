# Agent Center, unified relay + daily digest (frozen surface)

Agent Center provides the reminder state contract, outbound Discord delivery (`relay.py`) and
daily aggregation (`digest.py`). Downstream skills call these interfaces through subprocesses;
transport and scheduling remain owned by the base. Keeping webhook, bot and registry handling
behind the relay allows routing changes without changing each caller.

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

One channel enumeration owns inbound discovery, and one relay owns outbound transport. Separate
readers previously kept different channel lists and cursors, which could consume a message in a
reader that had no handler for it. Separate senders duplicated credential handling, multipart
encoding and User-Agent behavior. Centralizing those responsibilities keeps channel additions
and transport fixes in one place.

## relay.py, the single Discord egress

```
python relay.py send   --stream <name> (--text T | --json '{"content":..,"username":..}')
python relay.py send   --channel-id ID --text T [--file PATH ...]   # bot transport
python relay.py digest --text T        # aggregated summary -> Big Brother DM
python relay.py list                   # configured streams (NEVER prints webhook URLs)
python relay.py health                 # registry sane? (no network, no secrets)
```

- **Two transports behind one caller contract.** `files` given, or `channel_id` given → the bot
  (`registry.reader.bot_token`). A named stream uses its webhook when present; a notification-only
  stream with just `channel_id` uses that same canonical bot token.
  A configured webhook carries per-stream identity and is bound to one channel. This relay routes
  attachments and explicit-channel responses through the bot; callers keep the same interface.
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
- **Fallback**: an unknown stream or missing registry routes to Big Brother DM with a `[stream]`
  prefix. Callers must still inspect the delivery result.
- **Gotcha (encoded in code)**: Discord/Cloudflare 403s the default urllib User-Agent, `relay.py`
  always sends a real `User-Agent`.
- **Test seam**: `AGENT_CENTER_RELAY_DRYRUN=1` skips the network.

### Dedicated notification channels

Low-volume, machine-specific alerts belong under one Discord category with one text channel per
notification type. `agent_center_admin.py` reconciles that shape, records a bot-backed stream in the
private registry, opts the channel out of inbound command processing, and can prove the normal
`relay.py send --stream` path with a real test post:

```
python agent_center_admin.py ensure-notification \
  --stream model-mapping --category specific-notifications --channel model-mapping \
  --skill cc-model-refresh --description "cc model mapping changes" \
  --test-text "Agent Center model-mapping route verified"

python agent_center_admin.py check-notification \
  --stream model-mapping --category specific-notifications --channel model-mapping --probe
```

The command is idempotent: it creates missing resources, moves one same-named text channel under the
category when needed, and refuses duplicate names rather than guessing. It never prints the bot
token or webhook URLs. Notification-only streams use the registry's canonical bot token and need no
additional webhook secret.

Provisioning a channel verifies the relay route. To verify a sender migration, run
`scripts/verify_model_mapping_route.py`: it calls the configured sender and reads back the message
from that notification type's own channel. This command sends real notifications. It checks the
configured language rule before sending; a missing rule fails without delivery.

Set `SCHEDULE_ROUTE_SCRIPTS_DIR` to the absolute directory containing `notification_language.py`.
For source installations, set `SCHEDULE_ROUTE_CC_SENDER_DIR` to the absolute calibration directory
containing `apply.py`, and `SCHEDULE_ROUTE_CODEX_SENDER_DIR` to the absolute source directory
containing `model-refresh.js` and `gateway.js`. Without an override, sender directories resolve
relative to `SCHEDULE_ROUTE_SCRIPTS_DIR` using the verifier's `ROUTES` table. A supplied override
must be absolute; an empty or invalid override fails before delivery.

The Python apply-map probe replaces only the configuration writer and proxy reloader. The
JavaScript probe runs the sender's real `settings()` and `notify()` in a separate Node process.
Neither changes the model selection. A `notify` probe supplies its own message, so it proves the
transport and channel binding; sender wording still needs tests of the sender's message functions.
Each sender runs separately so modules with the same name cannot share imported state.

Notification bodies follow the configured language rule. Model identifiers and quoted error text
remain verbatim. Callers should return delivery success or failure and use the relay's base64 text
option for multiline messages that need to cross Windows command arguments.

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

Inbound polling reads eligible owner messages, routes them to command handlers or dispatch,
and records the resulting actions and confirmations. It uses the configured bot and channel
enumeration described below.

```
python ingest.py poll                  # read Discord; persist each channel's cursor and inbox
python ingest.py list                  # registered streams AND what guild discovery adds
python dispatch.py --stream <name>     # judge one stream's inbox -> execute -> confirm
python ingest_tick.py                  # scheduled entrypoint: poll -> commands -> dispatch
```

### Which channels are read, and the invariant that comes with it

`ingest.channels()` is the ONE answer, and it is deliberately wider than the registry: every
registered stream whose `inbound` and `listen` are not false, other readable text channels in the
guild, and the operator's DM. Explicitly excluded channels stay excluded during discovery.

A registry of outbound destinations can omit channels where the user gives instructions.
Guild discovery prevents that omission from becoming an unread command. Every message read
must be claimed by a handler or written to an inbox:

- `poll_stream` writes the cursor and the inbox in one function, and the inbox records the whole
  batch including messages a handler is about to claim: the durable trace of what was seen is kept
  separately from the decision about what to act on.
- `commands.route` returns `(claimed, remaining)` and the two must add up to the input.
- **Opting out is explicit and survives discovery**: either `inbound: false` or `listen: false`
  excludes the channel from reads, and the guild sweep may not add it back.
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
  actionable state (active pool items as `id | title`), asks the installed `llmcall` judge
  for a compact JSON *action plan*
  `{actions:[{op:done|snooze|create,...}], confirm}`, then a **deterministic** executor runs it via
  `reminder.py`. The executor only touches ids that were shown to the model, a hallucinated id is
  silently skipped, never acted on.
- **Per-stream handler** (`STREAMS` in `dispatch.py`): `mail` → reconcile the **email-monitor** task
  pool (done/snooze/create with `source=email-monitor`); `reminders` → done/snooze any active
  reminder; every other stream → generic create-a-followup + confirm (`source=agent-center:<stream>`).
- **Shared model policy.** Judge calls use `llmcall.call(prompt, mode="judge")` through the local
  `dispatch.call_chain` wrapper. Routing, model, timeout and fallback remain the installed
  interface's policy. Missing dependencies or unusable responses fail explicitly; this skill
  does not start provider CLIs or supply a provider ladder.
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

`agent` enqueues execution and `stop` requests termination. These actions have distinct ownership
and evidence requirements because recording a request as a todo does not execute or stop it.

```
python agent_tick.py                    # reconcile exited runs, then launch at most one
python agent_tick.py --stop <id>        # persist stop intent and verify termination
python agent_run.py --id <id> --generation <n>  # enter an already-reserved generation
python agent_task.py list               # active work orders; --all includes terminal orders
python agent_task.py status             # unreleased reservations and queued item IDs
python agent_task.py recover-cleanup --id <id> --generation <n> --evidence-file <private-audit.json>
```

`--no-post` on dispatch, tick or runner suppresses channel reporting. It still permits model
calls, command execution and database changes. It is not a dry run. See
[operations.md](operations.md) for command and installation boundaries.

- **A work order is an ordinary pool item** (`source=agent-center:work`, `due_at` NULL so a running
  order never trips the reminder notifier). `ext` holds only short fixed `x_agent_exec_*` fields;
  the request text, prompts, per-round transcripts and check output are files in a run directory
  in the PRIVATE companion. Enqueue atomically saves the request and its integrity digest before
  publishing the item. Reusing an action identity preserves the original request.
- **Each claim owns a generation.** One transaction reserves the serial writer and publishes the
  item state, execution metadata and operation generation. The runner must claim that generation
  before executing; parent registration, checkpoints and completion cannot overwrite a newer
  generation. Requests and results also carry their run and attempt identities.
- **Judge, then hand off.** `dispatch` selects record updates or execution actions and emits
  `{"op":"agent","request":...}` or `{"op":"stop","id":...}`, and the deterministic executor
  enqueues work or requests a stop against the saved authorized work IDs. Console actions bind
  stop intent and final cancellation to the observed generation; a stale request cannot stop a
  replacement generation. A confirmation distinguishes requested, stopped and unresolved outcomes.
- **A round is act, verify, review, decide.** The actor returns a final verification contract with
  a nonempty summary and a command, or an explicit `verify: null` when no executable check exists.
  A failing check cannot complete the work. Completion additionally requires the actual actor and
  reviewer model identities, different reported model families, a `DONE` verdict and unchanged
  before/after review evidence. A review-only completion is identified in the report. The verdict
  is the first non-empty line of the answer and lines after it are explanation
  (`agent_run.review_verdict`): `DONE` must be the bare token (one trailing full stop allowed) and
  no later line may itself read as a verdict; `CONTINUE:` must open the answer with a reason.
  Anything else (`DONE, but ...`, a verdict after a preamble, `DONE` followed by a `CONTINUE:`
  line) is no verdict and leaves `review_unavailable`.
- **Unknown evidence does not replay execution.** Missing execution or cleanup evidence leaves
  `reconcile`; an invalid final contract or unavailable independent review leaves
  `review_unavailable`. A confirmed failure before execution can be recorded as `failed`.
  Only known verification failures or an explicit `CONTINUE:` verdict may continue the loop.
  Repeated unchanged failures rotate the prompt approach, while llmcall routing remains unchanged.
- **Execution evidence from llmcall 0.3.0.** The installed `Result` has no typed outcome,
  cleanup or model-id fields, so `agent_run.execution_evidence` derives them from llmcall's own
  public records and from nothing else (never from response text):
  - model family is `llmcall.rung_group(provider)` of the rung that answered. llmcall refuses a
    model from another catalogue on a rung, so the group is the family; `effective_model` names
    the rung and `model_source` is `llmcall-rung`, because 0.3.0 does not report a model id;
  - a rung skipped for budget or an already-refused group never started, a rung that answered
    started, and every other launched rung is unknown;
  - cleanup is confirmed only when no rung reported `process_cleanup_failed` and every rung that
    ran was tree-owned (no `supervision` note). Anything else stays unconfirmed and the order goes
    to `reconcile`.
  Only a genuine installed `llmcall.Result` is translated; any other untyped result stays
  unresolved and cannot complete. `tools/llmcall_contract.py` pins the field and reason names
  this relies on against the installed package.
- **What 0.3.0 cannot express.** Per-call `requirements` (tool or permission limits) have no
  equivalent: `mode="agent"` grants the installed full agent policy on every rung. A caller that
  passes explicit requirements to `agent_run._llm` is refused before launch
  (`capability_unavailable`), never widened silently.
- **Revocation reaches a running call.** `run_order` runs the whole order inside
  `llmcall.process.execution_scope(cancel=...)`. llmcall 0.3.1 and later poll that token while a
  model call runs and stop the client tree once it is set; commands (`verify`, git evidence) run
  through `llmcall.process.run` with the same token. Both pollers ask every fraction of a second,
  and one ownership answer costs a PRIVATE proof plus a CLI read (the proof is a full one, measured
  at 4 to 5 s with about 54 git subprocesses and one `gh` visibility query, at most once per 60 s
  for an unchanged companion, and a memo hit otherwise), so they get a `PolledCancellation`: the answer is
  reused for `AGENT_EXEC_CANCEL_POLL_SECONDS` (default 30 s) and latched once set. A revoked or
  lost order therefore stops within about 35 s. The runner's own decision points between phases
  still ask the exact `OperationCancellation`. Under llmcall 0.3.0 the token is ignored while a
  model call runs, so the order waits for that call's phase budget.
- **Liveness is `(pid, process creation time)`.** Windows recycles pids, so a pid-only probe reads a
  recycled number as the live holder, and `os.kill(pid, 0)` is not an existence check there at all.
  Failed identity queries remain uncertainty. Stop intent is saved before tree termination;
  cancellation requires a verified absent/reused identity or verified termination.
- **A stop terminates; it does not wait for the runner to cancel itself.** `agent_tick.stop` (the
  Task Console's stop and `--stop`) saves the intent and then ends the runner's process tree with
  `taskkill /T /F` within a few seconds. The runner's cooperative path (the polled token above)
  never gets to end the running llmcall call, so that call writes no ledger row; the record of
  the stop is the run's `events.jsonl` `stopped` event (`killed`, `status`) and the cancelled
  order. A grace period before termination was considered and not added: the runner notices
  revocation only after up to `AGENT_EXEC_CANCEL_POLL_SECONDS` (30 s) plus one ownership query,
  which is longer than the console's 30 s budget for the whole stop command, and `stop` holds the
  run-root lifecycle lock that the runner's own `finish` needs, so the runner could not finish
  inside the wait anyway.
- **An interrupted spawn retains its reservation.** A missing parent or unregistered child is
  insufficient cleanup evidence. Stop revokes an unregistered spawn into `reconcile` with unknown
  cleanup. Unreleased or uncertain reservations keep the serial slot even when the item has a
  terminal outcome; legacy running and stop-pending work also blocks another launch.
- **Recovery requires a separate review.** Inspect `agent_task.py status`, establish that the
  relevant process tree is gone, and retain that audit in the PRIVATE companion. `recover-cleanup`
  requires the exact terminal generation plus a nonempty `note` and `evidence_sha256` in the JSON
  file. It validates those fields, rechecks recorded runner/launcher identities and compares the
  reservation before release. The digest records the operator's audit; the command does not
  independently prove its contents. Recovery releases cleanup ownership without replaying work
  or marking the task successful.
- **The runner is started with `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`** under `python.exe`
  (never `pythonw.exe`), and writes output into its private run directory. `DETACHED_PROCESS` is
  deliberately absent: Windows ignores `CREATE_NO_WINDOW` when both are set, which left the runner
  with no console, so each console program it started opened a visible window. The runner now has
  one hidden console that its children inherit. Native process tests and installed scheduler
  validation are separate from source review.
- **Installed llmcall policy applies throughout.** Actor calls use `llmcall.call(prompt,
  mode="agent", timeout=ACT_TIMEOUT)`; reviewer calls use judge mode with
  `timeout=REVIEW_TIMEOUT` and the observed actor family as `avoid`. The phase budgets
  (`AGENT_EXEC_ACT_TIMEOUT`, default 1800 s; `AGENT_EXEC_REVIEW_TIMEOUT`, default 420 s) are the
  whole chain budget for one act or one review. Without them llmcall's 180 s default ends agent work
  mid-task. The runner does not override `LLMCALL_AGENT_RUNNER` or pin providers, models or
  fallback. The working directory is the runner's process cwd (it changes into the workspace
  first), and it passes no `cwd`, `cancel` or `requirements` arguments, which 0.3.0 does not accept;
  cancellation travels through the execution scope instead.
- **Terminal reports carry evidence**: changed files, the command, its actual output and the
  reviewer's verdict. Tests prohibit 已处理 as an unsupported completion statement.
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
