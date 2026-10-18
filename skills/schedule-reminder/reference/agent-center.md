# Agent Center, unified relay (frozen surface)

Agent Center provides the reminder state contract and outbound Discord delivery (`relay.py`).
Downstream skills call these interfaces through subprocesses;
transport and scheduling remain owned by the base. Keeping webhook, bot and registry handling
behind the relay allows routing changes without changing each caller.

## Topology

```
OUT:  each skill  --(relay.py send --stream X)-->  Agent Center #X channel  (per-stream webhook + identity)
      schedule-reminder tick  --(relay.py send --stream reminders)-->  #reminders
IN:   user writes in #X --(ingest_tick: poll)--> commands.py claims it?  --yes--> handler answers in #X
                                                                        --no --> dispatch (LLM judge)
                                                                                 -> reminder.py mutations
                                                                                 --(relay confirm)--> #X
```

Streams (Agent Center server): `mail · hotspots · demand · promotion · support · crypto · infra ·
reminders · general · ops`, plus `commands · guestbook · archive` which are registered but opted out
of reading. The bus is **two-way**: `relay.py` is the egress, `ingest.py`/`commands.py`/`dispatch.py` the ingress.

One channel enumeration owns inbound discovery, and one relay owns outbound transport. Separate
readers previously kept different channel lists and cursors, which could consume a message in a
reader that had no handler for it. Separate senders duplicated credential handling, multipart
encoding and User-Agent behavior. Centralizing those responsibilities keeps channel additions
and transport fixes in one place.

## relay.py, the single Discord egress

```
python relay.py send   --stream <name> (--text T | --json '{"content":..,"username":..}')
python relay.py send   --channel-id ID --text T [--file PATH ...]   # bot transport
python relay.py list                   # configured streams (NEVER prints webhook URLs)
python relay.py health                 # registry sane? (no network, no secrets)
```

- **Two transports behind one caller contract.** `files` given, or `channel_id` given → the bot
  (the canonical bot token; see **Bot token** below). A named stream uses its webhook when present; a notification-only
  stream with just `channel_id` uses that same canonical bot token.
  A configured webhook carries per-stream identity and is bound to one channel. This relay routes
  attachments and explicit-channel responses through the bot; callers keep the same interface.
- **A bot send has no Big Brother fallback and returns False.** It is addressed at one specific
  channel; silently rerouting "the answer to what you just typed in #here" into a DM is worse than
  a visible failure the caller can report in place.

- **Registry (secrets; never in THIS public repo)**: discovery = env `AGENT_CENTER_CONFIG`, else a
  registry file in the Agent Center config dir (outside this repo). Shape:
  `{"streams":{"<name>":{"webhook":"...","username":"..."}},
  "reader":{"bot_token_ref":"cv://<record>/<credential>"}, "big_brother":{...}}`. The reader block
  names the canonical Discord bot token the inbound ingest reads. That config dir is version-controlled in a **private**
  companion repo for backup + portability, secrets live there, never here. See `deployment.md`.
- **Bot token**: one resolver (`scripts/bot_credential.py`) serves ingest, the tick, relay, Big
  Brother, the admin CLI, the config doctor and the route check. Order, with no fallback after a
  failure: `AGENT_CENTER_BOT_TOKEN` in the environment (an explicit injection, for a machine without
  a vault key); else `reader.bot_token_ref`, a `cv://` reference resolved at use through the
  credvault Python library, so the value never sits in the registry file; else an inline
  `reader.bot_token` (an install without a vault). A registry carrying both registry fields is a
  configuration error, because a stale inline copy must never cover for a reference that failed.
  When a reference cannot be resolved (package not importable by the interpreter running the task,
  vault or key missing, unknown record), ingest and the admin CLI stop with
  `BotTokenUnavailable` and the cause; relay and Big Brother write one
  `bot token unavailable: <cause>` line to stderr and degrade (webhook streams keep delivering);
  the tick logs the cause and still dispatches staged work. No path prints the value.
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

## Inbound, user replies become actions (two-way)

Inbound polling reads eligible owner messages, routes them to command handlers or dispatch,
and records the resulting actions and confirmations. It uses the configured bot and channel
enumeration described below.

```
python ingest.py poll                  # read Discord; stage new replies and reactions, move cursors
python ingest.py list                  # registered streams AND what guild discovery adds
python dispatch.py --stream <name> --reply <text>   # judge one reply -> execute -> confirm
python ingest_tick.py                  # scheduled entrypoint: poll -> commands -> dispatch
python ingest_tick.py --retry-command <id>  # rearm one failed record after checking its effects
```

### Which channels are read, and the invariant that comes with it

`ingest.channels()` is the ONE answer, and it is deliberately wider than the registry: every
registered stream whose `inbound` and `listen` are not false, other readable text channels in the
guild, and the operator's DM. Explicitly excluded channels stay excluded during discovery.

A registry of outbound destinations can omit channels where the user gives instructions.
Guild discovery prevents that omission from becoming an unread command. Every message read
is staged as a durable inbound record before its cursor moves:

- `poll_stream` stages every owner message, including ones a handler is about to claim, and then
  writes the cursor, in one function: the durable trace of what was seen (`inbound/*.json`) is kept
  separately from the decision about what to act on. There is no separate inbox file.
- **One read per channel per tick.** `ingest.poll()` enumerates the channels once (the operator's
  DM channel id is cached in `owner-dm.channel`, so it is opened once, not every tick) and reads
  each channel's newest 50 messages once. The text pass takes the messages newer than the cursor
  from that page, and reads from the cursor instead only when all 50 are newer; the reaction pass
  reads the same page. A reaction whose count has not changed since the last tick is not read
  again, and the seen file is rewritten only when it changes.
- `commands.route` returns `(claimed, remaining)` and the two must add up to the input.
- **Opting out is explicit and survives discovery**: either `inbound: false` or `listen: false`
  excludes the channel from reads, and the guild sweep may not add it back.
- **Cursors are keyed on the CHANNEL ID**, not the stream name: a discovered channel's name is
  whatever a human typed, and a rename would orphan a name-keyed cursor and replay that channel's
  history.

### Commands, the deterministic half

Some messages are commands, not conversation. `commands.py` tries them per message BEFORE the
judgment chain, and a claimed message never reaches a model.

```jsonc
// registry.commands
"gradient": {
  "trigger": "^\\s*(?:gradient|bg|背景)\\b",   // python regex, matched per message
  "exec": ["python", "~/path/to/tool.py", "handle"],
  "timeout": 300      // seconds; the tick clamps it to what is left of its time budget
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
  `{actions:[{op:done|snooze|create,...}], confirm}`, then a **deterministic** executor runs it
  through the store in process (the same functions `reminder.py` wraps, with actor
  `agent-center-dispatch` and the action identity as the creation idempotency key). The executor
  only touches ids that were shown to the model; a hallucinated id is rejected, never acted on.
- **Retries reuse the plan.** The plan and its authorization snapshot are saved before anything
  runs. A retry does not ask the model again or rebuild the state listing: it reads only the saved
  targets (which authorized items are still active, which authorized orders still run) and re-runs
  only actions whose outcome failed.
- **Finished, retried or given up.** A reply is finished once its plan ran without a failed action
  and the confirmation was delivered; a rejected action is final and is listed in the
  confirmation. A failed action, an undelivered confirmation or a planner that returned no usable
  plan leaves the record pending for the next tick, at most `inbound.MAX_FAILURES` (3) attempts.
  Then the record becomes `failed` and waits for `ingest_tick.py --retry-command <id>`. Only the
  last planning attempt posts "自动解析失败" in the channel.
- **One time budget per tick.** The tick stops starting channel reads and pending records after
  60 s (the rest wait for the next tick), bounds the planner call by the remaining budget (at most
  120 s) and clamps command handlers the same way, so a tick ends inside the task's 4-minute
  limit instead of being killed mid-call. The dispatch and command layers are imported only when
  something is pending.
- **Per-stream handler** (`STREAMS` in `dispatch.py`): `mail` → reconcile the **email-monitor** task
  pool (done/snooze/create with `source=email-monitor`); `reminders` → done/snooze any active
  reminder; every other stream → generic create-a-followup + confirm (`source=agent-center:<stream>`).
- **Shared model policy.** Judge calls use `llmcall.call(prompt, mode="judge")` through the local
  `dispatch.call_chain` wrapper. Routing, model, timeout and fallback remain the installed
  interface's policy. Missing dependencies or unusable responses fail explicitly; this skill
  does not start provider CLIs or supply a provider ladder.
- **User vs bot.** `ingest.py` counts a message as a user reply only when it is neither `author.bot`
  nor a `webhook_id` post, so the skill's own relay/digest confirmations never feed back on
  themselves. Bot token: the resolver above (`AGENT_CENTER_BOT_TOKEN`, `reader.bot_token_ref` or
  `reader.bot_token`).
  Same urllib `User-Agent` gotcha as relay (Discord 403s the default).
- **Cursors** live in `<state-dir>/<channel-id>.last` under the Agent Center config dir, and the
  reaction seen sets in `<state-dir>/<key>.reactions.seen`, where `<key>` is the stream name for
  registered streams and the channel id for discovered ones. First contact with a channel **arms** the cursor (records latest
  id, processes nothing): back-processing a newly discovered channel would replay its whole visible
  history through the judgment chain and could enqueue real work from months-old messages.
- **Schedule**: Windows task **AgentCenterIngestTick** (PT5M) runs `ingest_tick.py`; it supersedes
  the retired ad-hoc `AgentCenterMailTick` (mail-only loop).
- **Owner only, fail closed.** A text reply counts only when its author is
  `registry.big_brother.user_id`, the same person the reaction path has always required. If no owner
  is configured `poll` RAISES instead of falling back to "anyone in the channel": these replies
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
- **A round is act, verify, decide.** The executor (one `mode="agent"` call) does the work and
  ends its answer with a final contract: a nonempty summary and a `verify` command, or an explicit
  `verify: null` when no executable check exists. The system runs that command itself in the
  workspace. Exit 0 completes the order; 124, 127 or 130 (timeout, unrunnable, cancelled) leaves
  `reconcile` as "verification interrupted"; any other nonzero exit starts the next round with the
  command's real output, and a failing check never completes the work. `verify: null` completes
  the order on the executor's summary. An answer without a valid contract also completes the
  order: the executor is trusted, nothing is run, and its answer (prose with any trailing JSON
  stripped, bounded) becomes the summary. No second model reviews the work.
- **Unknown execution does not replay.** Missing execution or cleanup evidence leaves
  `reconcile`. A confirmed failure before execution can be recorded as `failed`. Only a known
  verification failure continues the loop. Repeated unchanged failures rotate the prompt approach,
  while llmcall routing remains unchanged. `review_unavailable` is historical: the retired
  independent review produced it, and old orders may still carry it.
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
  equivalent: `mode="agent"` grants the installed full agent policy on every rung. The runner
  passes none.
- **Revocation reaches a running call.** `run_order` runs the whole order inside
  `llmcall.process.execution_scope(cancel=...)`. llmcall 0.3.1 and later poll that token while a
  model call runs and stop the client tree once it is set; the `verify` command runs through
  `llmcall.process.run`, which polls the same token. Both pollers ask every fraction of a second,
  so they get a `PolledCancellation`: one ownership answer (two in-process database reads) is
  reused for `AGENT_EXEC_CANCEL_POLL_SECONDS` (default 30 s) and latched once set. A revoked or
  lost order therefore stops within about 35 s. Between phases the runner asks nothing extra:
  ownership is checked once when an approach starts, and every step that commits (checkpoint,
  child receipt, finish) is a generation-fenced store write whose `False` ends the round as
  cancelled. Under llmcall 0.3.0 the token is ignored while a model call runs, so the order waits
  for that call's phase budget.
- **No workspace inspection.** The runner neither runs git nor snapshots the workspace. The
  changed-file list is the executor's self-reported `changed` list and the report labels it so.
  The stall signature hashes the check's return code, its normalized output and the current bytes
  of the reported files. Dispatch uses a requested folder that exists in place; an order with no
  usable folder (none, a missing one, the home directory or a filesystem root) gets its own
  directory under `agent-runs/workspaces/`, so its files stay out of the home directory.
- **Work-order reads and writes are in process.** `agent_task.orders`/`get` read the store
  directly, and every lifecycle write is a generation-fenced store transaction. No reminder.py
  child is started on the tick or runner path.
- **Liveness is `(pid, process creation time)`.** Windows recycles pids, so a pid-only probe reads a
  recycled number as the live holder, and `os.kill(pid, 0)` is not an existence check there at all.
  Failed identity queries remain uncertainty. Stop intent is saved before the job is terminated;
  cancellation requires a verified absent/reused identity or verified termination.
- **A stop terminates; it does not wait for the runner to cancel itself.** `agent_tick.stop` (the
  Task Console's stop and `--stop`) saves the intent and then terminates the runner's job within a
  few seconds. The runner's cooperative path (the polled token above)
  never gets to end the running llmcall call, so that call writes no ledger row; the record of
  the stop is the run's `events.jsonl` `stopped` event (`killed`, `status`) and the cancelled
  order. A grace period before termination was considered and not added: the runner notices
  revocation only after up to `AGENT_EXEC_CANCEL_POLL_SECONDS` (30 s) plus one ownership query,
  which is longer than the console's 30 s budget for the whole stop command.
- **Launch and stop exclude each other, nothing else is locked.** Every state change is a fenced
  store transaction, so the claim, the runner's own `finish`, enqueue and the reaper take no
  lock. The run-root `.lifecycle.lock` is held only from the spawn checkpoint to the PID receipt
  in `launch`, and around saving stop intent, terminating and cancelling in `stop`, so a stop
  cannot land in the window where a spawned runner has no recorded identity. Owner reports are
  sent after the lock is released.
- **Each runner lives in its own Job Object, and a stop is verified by it.** The tick creates the
  runner suspended, puts it into a job named after its recorded identity
  (`Local\schedule-reminder-runner-<item>-g<generation>-<pid>-<creation time>`), hands the runner
  one handle to that job (which keeps the name alive while the runner lives) and only then resumes
  it, so every descendant is born inside the job. A stop opens the job by that name, checks that the
  recorded runner is a member, terminates the job (`TerminateJobObject` also ends llmcall's nested
  kill-on-close jobs) and releases the slot once the job reports `ActiveProcesses == 0`, with a
  `verified-job-kill` receipt (`stopped` event `method: job`). No snapshot walk is involved, so an
  unrelated process born during the stop can no longer hold the slot. The job allows breakaway and
  never kills on close: a descendant that starts a shared process with `CREATE_BREAKAWAY_FROM_JOB`
  (llmcall's `--breakaway`, the shared MCP proxy) leaves it on purpose. The stop reports such a
  process (`runner_breakaway` event, `broke_away` in the reply and the receipt) when its parent is
  still a member at stop time, and does not kill it; one started through a helper that already
  exited cannot be attributed and is not reported. If that breakaway check itself fails, the reply
  and the receipt carry `breakaway_check: "failed: ..."` (`runner_breakaway_unchecked` event; the
  console's stop message says it) rather than an empty list. A verified job kill also settles a
  child receipt in which llmcall reported unconfirmed cleanup (`cleanup_state` `unknown`), because
  llmcall's processes and nested jobs were all members; it does not when that receipt lists
  processes that broke away (`broke_away`), when the stop found or could not check for a breakaway,
  A runner whose resume fails is ended while still suspended; once its exit is confirmed the
  launch is `not_started`: the slot is released and the order is requeued (blocked after three
  such launches). A resume that failed after a thread had already resumed, or an end that cannot
  be confirmed, stays "launch outcome unknown".
- **The job is mandatory.** A runner that cannot be adopted into its job (the job cannot be
  created, no native handle, no creation time) is ended while still suspended and its launch is
  `not_started`, the same bounded requeue as a failed resume; it never runs without containment.
  At a stop, a job that cannot be opened while a recorded runner identity is still alive leaves
  the stop pending (`stop_pending`, reservation held). When every recorded identity has already
  exited, the stop cancels the order and holds the reservation (`cleanup_held`) until the reaper
  releases it on a quiescent receipt, or `recover-cleanup` below.
- **An interrupted spawn retains its reservation.** A missing parent or unregistered child is
  insufficient cleanup evidence. Stop revokes an unregistered spawn into `reconcile` with unknown
  cleanup. Unreleased or uncertain reservations keep the serial slot even when the item has a
  terminal outcome.
- **Recovery requires a separate review.** Inspect `agent_task.py status`, establish that the
  runner's job has no live processes left, and retain that audit in the PRIVATE companion. `recover-cleanup`
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
- **Installed llmcall policy applies throughout.** The executor is the runner's only model call:
  `llmcall.call(prompt, mode="agent", timeout=ACT_TIMEOUT)`. The phase budget
  (`AGENT_EXEC_ACT_TIMEOUT`, default 1800 s) is the whole chain budget for one act. Without it
  llmcall's 180 s default ends agent work mid-task. The runner does not override `LLMCALL_AGENT_RUNNER` or pin providers, models or
  fallback. The working directory is the runner's process cwd (it changes into the workspace
  first), and it passes no `cwd`, `cancel` or `requirements` arguments, which 0.3.0 does not accept;
  cancellation travels through the execution scope instead.
- **Terminal reports carry evidence**: the executor's summary, its self-reported changed files,
  the command and its actual output, or a warning that no executable check ran. Tests prohibit
  已处理 as an unsupported completion statement.
- **Schedule**: an order launches as soon as it can. Enqueue starts a detached `agent_tick.py`
  (`pythonw`, no window, not waited for) once the order is published, and a runner does the same
  after it releases the serial slot, so the next queued order does not wait for the poll. Windows
  task **AgentCenterWorkTick** (PT2M) runs `agent_tick.py` as the backstop and the crash reaper,
  separate from the inbound tick so neither can starve the other. A tick with no unreleased
  reservation and no queued, running or stopping order returns after one read-only query.

## How a downstream skill integrates (copy-paste)

```python
import subprocess, sys, os
REMINDER_DIR = os.path.join(os.path.expanduser("~/.claude/skills/schedule-reminder"), "scripts")  # or probe
def push(stream, text, username=None):
    cmd = [sys.executable, os.path.join(REMINDER_DIR, "relay.py"), "send", "--stream", stream, "--text", text]
    if username: cmd += ["--username", username]
    return subprocess.run(cmd).returncode == 0
```

