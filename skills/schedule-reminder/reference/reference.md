# schedule-reminder reference

The one on-demand reference for this skill. `SKILL.md` stays the always-loaded summary.

- [Contract](#contract)
- [Integration](#integration)
- [Agent Center](#agent-center)
- [Notification receipts](#notification-receipts)
- [Delivery and work recovery](#delivery-and-work-recovery)
- [Manual completion](#manual-completion)
- [Dispatch identity](#dispatch-identity)
- [Deployment](#deployment)

## Contract

> Call `reminder.py <verb>` via
> subprocess and parse stdout JSON (JSON is always emitted, there is no `--json` flag). **Never**
> read the `.db` file, build SQL, or import internal tables. Everything below is additive-only within
> `api_version 1.x`; any delete/rename/semantic change bumps `api_version` and runs a dual-version
> transition period.

Background Task Core also has the separately documented, read-only `reminder_linked_items` owner seam;
its reviewed authority and versioning are described in [linkage review](#background-task-core-linkage).

Contents: [Invocation](#invocation) · [Verbs](#verbs) · [Item fields](#item-fields) ·
[States](#states--transitions) · [Error codes](#error-codes) · [Idempotency](#idempotency) ·
[Time](#time) · [Unknown fields](#unknown-fields-must-preserve) · [Versioning](#versioning)

### Invocation

```
python reminder.py [--db PATH] [--actor NAME] <verb> [args...]
```

- **stdout** = one JSON object (JSON Lines) on success, with top-level `api_version`,
  `schema_version`, `ok:true`, plus the payload.
- **stderr** = one JSON object `{api_version, ok:false, error_code, message, ...}` on failure.
- **exit code**: `0` success · `1` structured error · `2` usage error.
- **db path**: `--db` or env `SCHEDULE_DB_PATH` (use a per-caller path for tests).
- **clock injection**: `--now ISO` or env `SCHEDULE_NOW` (tests / catch-up replay).
- Output is always UTF-8 regardless of host console code page.

### Verbs

| Verb | Purpose | Key args | Output |
|---|---|---|---|
| `init` | create/upgrade DB (idempotent) | none | `{db_path, schema_user_version}` |
| `add` | create or update keyed item | `--title` (req), `--kind`, `--due-at`, `--state`, `--priority`, `--progress` (0-100), `--tags a,b`, `--source`, `--idempotency-key`, `--if-exists update\|return`, `--description`, `--ext JSON`, `--recurrence RRULE`, `--rdate JSON`, `--exdate JSON`, `--alarms JSON` | `{item}` |
| `creation-preflight` | read cross-source candidates | `--title`, `--source`, `--idempotency-key`, occurrence/content fields | `{decision, matches[], scanned, complete}` |
| `ensure` | create, reuse or append reviewed follow-up | preflight fields, `--if-exists return`, `--reuse-id`, `--expected-revision`, `--note`, `--distinct-reason` | `{item, decision}` |
| `get` | fetch by id | `--id` | `{item}` |
| `list` / `query` | filter + keyset page | `--state`, `--source`, `--kind`, `--due-before`, `--active`, `--limit`, `--cursor` | `{items[], next_cursor}` |
| `update` | patch fields (not state) | `--id`, `--set field=value` (repeatable), `--ext JSON`, `--idempotency-key` | `{item}` |
| `transition` | state move (state machine + CAS) | `--id`, `--to`, `--expect`, `--reason`, `--progress`, `--ext JSON` | `{item}` or error |
| `done` | mark complete | `--id`, `--ext JSON` | `{item}` (`end_at` set, `progress=100`) |
| `block` | mark blocked | `--id`, `--blocker-id`, `--reason` | `{item}` |
| `snooze` | suppress reminders until T | `--id`, `--until` | `{item}` |
| `due` | read items due now (read-only) | `--now`, `--lead` | `{items[], now}` |
| `tick` | dispatch due reminders (scheduler) | `--now`, `--lead`, `--dry-run` | `{dispatched[], retried[], blocked[], skipped[], now, lapsed[], undelivered[]}` |
| `events` | audit trail of an item | `--id` | `{events[]}` |
| `health` | self-check | (none) | `{health{...}}` |
| `work-feed` | read-only work/action projection | `--limit` (1-10000), `--event-limit` (0-1000) | `{schemaVersion:1, available, items[], events[], sources[], coverage, queue}` |
| `work-action` | accept an offered action | bounded JSON stdin, below | `{schemaVersion:1, status, action, wakeup, dispatch?}` |
| `work-action-stop` | persist a stop request | bounded JSON stdin, below | action receipt |
| `work-action-result` | record linked-task controller result | bounded JSON stdin, below | action receipt |

`--actor NAME` (global) records who acted in the audit stream, pass your skill name.

### Item fields

Every `item` object has exactly these keys (additive-only within `api_version 1.x`):

| Field | Type | Meaning |
|---|---|---|
| `id` | string | immutable UUIDv7 (time-ordered), the only durable external reference |
| `schema_version` | int | per-record schema version (tolerant forward parsing) |
| `kind` | `task`\|`event` | record kind |
| `title` | string | summary (required) |
| `description` | string\|null | long note |
| `state` | enum | `pending`/`doing`/`done`/`blocked`/`cancelled` |
| `progress` | int | 0-100, enforced on every write (`done` forces 100; out-of-range → `ERR_BAD_PROGRESS`) |
| `priority` | int | iCalendar 0-9 (1 highest, 0 undefined) |
| `due_at` | RFC3339\|null | deadline, the reminder anchor |
| `scheduled_at`/`start_at`/`end_at`/`wait_until` | RFC3339\|null | lifecycle timestamps |
| `tz` | string\|null | original DTSTART timezone (RRULE DST math) |
| `recurrence` | string\|null | RRULE; `tick` rolls a fired item to its next future occurrence (§Recurrence & alarms) |
| `rdate`/`exdate` | array\|null | extra / excluded dates (exdate occurrences are skipped on roll) |
| `tags` | array\|null | free tags incl. `from:<skill>` |
| `project` | string\|null | dotted hierarchy (`Home.Kitchen`) |
| `relations` | array\|null | `[{type, target_id}]`, type ∈ depends-on/parent/child/blocks/related |
| `alarms` | array\|null | per-alarm lead applied by `due`/`tick`: `[{"lead":3600}]` or `[{"trigger":"-PT15M"}]` |
| `source` | string\|null | writing skill |
| `idempotency_key` | string\|null | unique dedupe key |
| `notified_at`/`next_retry_at`/`retry_count`/`claimed_at` | varies | delivery bookkeeping |
| `block_reason` | string\|null | reason when blocked |
| `created_at`/`updated_at` | RFC3339 | audit timestamps |
| `ext` | object\|null | **MUST-PRESERVE** unknown-field container |

### States & transitions

`pending → doing|blocked|done|cancelled` · `doing → done|blocked|pending|cancelled` ·
`blocked → doing|pending|done|cancelled` · `done → pending` (reopen) ·
`cancelled → pending` (reopen). `done`/`cancelled` are protected terminals.

Write-time invariants (enforced at the store, not the caller): `done` requires all `depends-on`
targets done and sets `end_at`+`progress=100`; `cancelled` sets `end_at`; `blocked` needs an unmet
blocker or a `reason`; state changes go through `transition`/`done`/`block` (never `update`).
`block --blocker-id` commits the dependency relation and blocked state together. A rejected state
change or failed audit-event write leaves both the item and its prior events unchanged.

### Error codes

`ERR_NOT_FOUND` · `ERR_BAD_INPUT` · `ERR_BAD_KIND` · `ERR_BAD_STATE` · `ERR_BAD_PROGRESS` ·
`ERR_BAD_FIELD` · `ERR_BAD_TIME` · `ERR_BAD_JSON` · `ERR_ILLEGAL_TRANSITION` (carries `current`,
`to`, `allowed[]`) · `ERR_STATE_CONFLICT` (carries `current`, `expected`) · `ERR_DEPENDENCY_UNMET`
(carries `unmet[]`) · `ERR_BLOCK_REASON_REQUIRED` · `ERR_USE_TRANSITION` · `ERR_BUSY` ·
`ERR_INTERNAL` · `ERR_DATA_POLICY` · `ERR_UNINITIALIZED` · `ERR_PERMISSION` · `ERR_CONFLICT` ·
`ERR_CREATION_REVIEW` (carries candidate IDs and revisions). Action errors use their own safe
codes, including `stale_recommendation`, `request_conflict`, `another_action_active` and
`action_schema_upgrade_required`.

### Idempotency

`add`/`update` accept `--idempotency-key`. Re-issuing `add` with the same key returns the same
item ID and, by default, updates supplied content and merges ext. `add --if-exists return`
preserves the keyed item. A stable identity prevents duplicate rows; it does not make a changed
request equivalent to the original. Compose it from the producer and source occurrence.

`creation-preflight` compares normalized content, source role and occurrence/scope across
sources. It returns `create`, `reuse` or `review`; similarity is advisory. `ensure` repeats that
check in the write transaction and returns `created`, `reused`, `merged` or `replayed`. A similar
candidate needs an exact reviewed ID/revision or an explicit distinct-obligation reason. New
follow-up text requires `--note` and a new request identity; a legacy keyed item cannot silently
consume those review options. A changed payload under an existing ensure identity is a conflict.
Reads are idempotent and never initialize or migrate a database.

### Work projection and action requests

`work-feed` adds a `schemaVersion: 1` projection inside the normal CLI envelope. It separates
tracked items, agent work and signals, reports bounded coverage and persisted queue reservations,
and exposes only owner-issued action offers. Missing storage produces `available:false`; summary
data does not prove liveness, validation or external delivery. Raw `ext` is not forwarded.

The three action verbs accept one UTF-8 JSON object on stdin, at most 131072 bytes. Duplicate JSON
keys and invalid UTF-8 are rejected. `work-action` accepts `{item_id, action_id, revision, request_id}`
or `{request: <that object>, context: <string>}` with context limited to 40000 characters. Use the
offer ID (`complete`, `agent` or `task`) and revision from the feed; request IDs contain 8-120 ASCII
letters, digits, underscores or hyphens. The same exact request replays its durable receipt.

`complete` commits its receipt and guarded transition together, without a workspace, scheduler or
notification. An agent action needs an admitted PRIVATE workspace. A reviewed task action returns
one `dispatch` instruction for Background Task Core; replay does not issue another instruction.
`work-action-result` accepts `{action_id: <receipt ID>, result: <controller result>}` and updates
only a receipt still awaiting dispatch. The action saves the reviewed task ID, exact controller
name and review revision before dispatch; a result for another controller cannot satisfy it.
A canonical acknowledged `run_requested`, or observed
`already_running`, releases the handoff reservation as `task_requested` and leaves the todo state
unchanged. Incomplete acknowledgement or uncertain cleanup remains `reconcile`; Background Task Core owns
task runtime and stop control.
Historical task receipts without that identity binding remain `reconcile`.

`work-action-stop` accepts the same four request fields, with `action_id` set to the current agent
receipt ID. The action revision includes the observed execution generation; stopping passes that
captured generation to the runtime so a replacement run cannot inherit the old stop request.
Stop intent revokes preparation and runner ownership before external cleanup. A queued
order that never started is cancelled atomically. Started or uncertain work remains reserved until
the matching stop result confirms it; a process death alone is insufficient. Stale revisions need
a fresh observation. See [Manual completion](#manual-completion).

### Time

UTC RFC3339 with microsecond precision everywhere (string order == time order). `tick`/`due`
compare in UTC; trigger uses `now >= due_at - lead` (an interval; never `now == due_at`). Inject a
clock with `--now`/`SCHEDULE_NOW`.

### Recurrence & alarms

- **Alarms (per-item lead).** Each `alarms[]` entry sets how long *before* `due_at` the item fires:
  `{"lead": <seconds>}` or an iCalendar trigger `{"trigger": "-PT15M"}` (`-P1D`, `-PT1H`, …). The
  effective lead is `max(--lead, max alarm lead)`; `due`/`tick` then fire when `due_at - lead <= now`.
  An item with no alarms behaves exactly as before (global `--lead`, default 0).
- **Recurrence (rolling).** When a `recurrence` (RRULE) item fires in `tick`, it is **not** marked
  permanently notified, it rolls forward to its next occurrence *after now* and re-arms (so a
  long-overdue daily item fires once on catch-up, then re-arms for the next day). Supported subset:
  `FREQ=DAILY|WEEKLY|MONTHLY|YEARLY` + `INTERVAL` + `UNTIL`; `exdate` occurrences are skipped. Once
  `UNTIL` is passed the rule is exhausted and the item is marked notified (no further roll). The
  infinite series is never materialised, only the master row is kept and advanced.

### Unknown fields (MUST-PRESERVE)

Fields the base does not recognise are round-tripped verbatim through `ext`. Namespace downstream
extensions as `x_<skill>_*` (e.g. `x_promotion_campaign_id`). The base never drops them, even when
it rewrites other fields, this is what keeps the base safely extensible.

### Versioning

| Layer | Version | Rule |
|---|---|---|
| DB schema | `PRAGMA user_version` | additive migrations only; never rename/drop/retype |
| record | per-row `schema_version` | tolerant read; unknown fields preserved in `ext` |
| contract | `api_version` (this doc) | additive-only; delete/rename/semantic change bumps it + dual-run |

**Freeze first, integrate second.** This contract is frozen at `api_version 1.0.0`. Downstream
skills (#2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant) integrate
against it; the regression suite (E11) golden-compares verbs + item fields + state enum on every
change.

### Atomic finalization and delivery recovery

`transition` and `done` accept optional `--ext JSON`. State and metadata commit together; errors roll both back. Calls without `--ext` retain same-state no-op behavior. Unknown ext fields remain preserved.

Exhausted delivery is excluded from later ticks until rearmed, independently of an ordinary task's blocked state. Explicit `snooze` resets retry bookkeeping, and moving an idempotent reminder's due time forward rearms exhausted delivery. A dry-run tick makes no persistent item or event changes. These are repaired recovery rules, not evidence that earlier versions implemented them. See [Delivery and work recovery](#delivery-and-work-recovery).

## Integration

> For #2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant, and any future
> skill that needs to persist a reminder or read task progress. Use the CLI contract or the
> separately documented read-only Background Task Core linkage seam, never database internals.

### The golden rules

1. Call `python <path>/reminder.py [--actor NAME] <verb> ...` via subprocess; parse stdout JSON.
2. Put global `--actor <your-skill>` before the verb. Use `--source <your-skill>` on add/list/query for filtering.
3. Bind creation to a stable source occurrence with `--idempotency-key <your-skill>:<your-record-id>`.
   Reuse the same identity and payload for a retry; a new intentional occurrence gets a new identity.
4. Put your own extra data in `--ext` under an `x_<yourskill>_*` namespace; the base preserves it.
5. Read progress with `get`/`list`/`query`; never assume internal storage.

### Example, email-monitor files a deadline reminder

Before creating a todo, compare existing active obligations across sources with
`creation-preflight`, including the source and occurrence date. Use `ensure` for transactional
create/reuse. Similar wording alone does not authorize merging separate obligations. See below
for reviewed follow-ups; use `add` when the integration intentionally owns a keyed upsert.

```bash
python reminder.py --actor email-monitor add \
  --title "Reply to recruiter (Acme Corp)" \
  --kind task --due-at "2026-06-28T17:00:00Z" --priority 1 \
  --source email-monitor \
  --idempotency-key "email-monitor:msg-8841" \
  --ext '{"x_email_monitor_uid":"8841","x_email_monitor_thread":"t-1207"}'
```

Re-running the exact command returns the same item ID. By default, `add` updates the keyed
record's supplied content; use `--if-exists return` when a replay must preserve it.

### Reuse an obligation or append a follow-up

Call `creation-preflight` with the proposed `--title`, `--source`, `--idempotency-key` and all known
occurrence/content fields. It is read-only and returns candidate items with revisions. `ensure`
accepts the same fields, repeats the comparison under the owner transaction and creates or reuses
an equivalent item. A `review` decision requires deciding whether the obligation and occurrence
are actually the same.

For a follow-up, use `ensure --reuse-id <reviewed ID> --expected-revision <revision> --note <new text>`
with a new source request identity. The note is appended once and earlier content is preserved.
For a different obligation, provide `--distinct-reason` with the reviewed reason. A stale revision
or changed request payload is rejected; refresh and review it instead of bypassing the check.
Do not reuse a legacy add key to attach new follow-up options.

Inbound dispatch keeps the original channel/message identity, authorized IDs, first plan and
outcomes. An explicit `dispatch.py` caller supplies `--request-id`; unidentified calls retain the
legacy immediate path. `--no-post` only suppresses confirmation messages and still performs actions.
New saved plans bind canonical action keys before execution. Older pending plans with unprovable
action identities require reconciliation of their original effects before replay.
See [Dispatch identity](#dispatch-identity).

### Example, promotion-assistant reads what is still open

```bash
python reminder.py list --source promotion-assistant --active --limit 100
# -> {"items":[...], "next_cursor": "..."}  (page with --cursor)
```

### Example, daily-hotspots advances progress, then completes

```bash
python reminder.py --actor daily-hotspots transition --id "$ID" --to doing --progress 40
python reminder.py --actor daily-hotspots done --id "$ID"
```

### Example, cross-skill dependency (block until a prerequisite is done)

```bash
python reminder.py block --id "$CHILD" --blocker-id "$PARENT" --reason "waiting on data pull"
# the base will refuse to `done` $CHILD until $PARENT is done (ERR_DEPENDENCY_UNMET)
```

### Handling errors

Exit 1 emits structured JSON on stderr with `error_code`. Usage errors exit 2. Handle at least:

- `ERR_STATE_CONFLICT` / `ERR_CONFLICT`, someone changed the item or request; read and review again
  before deciding whether a new operation is authorized.
- `ERR_CREATION_REVIEW`, compare returned candidates and explicitly reuse or distinguish the occurrence.
- `ERR_ILLEGAL_TRANSITION`, your state move is not allowed; read `allowed[]`.
- `ERR_DEPENDENCY_UNMET`, finish the `unmet[]` items first.
- `ERR_BUSY`, transient; retry with back-off (rare; the base already retries internally).
- `ERR_DATA_POLICY`, the selected output lies inside the public source tree or Git metadata, or is reached through a filesystem alias.
- `ERR_UNINITIALIZED`, explicitly initialize the PRIVATE database before writing.
- `ERR_PERMISSION`, storage access was denied; this is distinct from DATA policy refusal.

### Python helper pattern

```python
import json, os, subprocess, sys

def call(*args, db=None):
    env = dict(os.environ)
    if db: env["SCHEDULE_DB_PATH"] = db
    r = subprocess.run([sys.executable, REMINDER, *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(json.loads(r.stderr)["error_code"])
    return json.loads(r.stdout)

item = call("--actor", "my-skill", "add", "--title", "X", "--source", "my-skill",
            "--idempotency-key", "my-skill:42", "--if-exists", "return")["item"]
```

### Work and notification consumers

Background Task Core reads `work-feed`, then submits its owner-issued action/revision through `work-action`
using JSON stdin. Manual completion needs no executor and preserves dependency guards. Linked task
execution first requires explicit [linkage review](#background-task-core-linkage); task IDs are compiled from
declarations and private bindings, never guessed from titles. A Scheduler handoff receipt leaves
the todo's business state unchanged. Work-feed is persisted observation, not a liveness probe.

For notifications tied to a durable business event, use the [notification receipt contract](#notification-receipts).
Keep its event identity stable across retries and preserve uncertain outcomes for reconciliation.
This contract is separate from due-reminder transport retries.

### Versioning promise

The verb set, item field set, and state enum are golden-tested (E11). Within `api_version 1.x` you
get **only additive** changes. A breaking change bumps `api_version` and ships a dual-run period,
pin to the `api_version` in the envelope if you need to be strict.

## Agent Center

Agent Center provides the reminder state contract and outbound Discord delivery (`relay.py`).
Downstream skills call these interfaces through subprocesses;
transport and scheduling remain owned by the base. Keeping webhook, bot and registry handling
behind the relay allows routing changes without changing each caller.

### Topology

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

### relay.py, the single Discord egress

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

#### Dedicated notification channels

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

Provisioning a channel verifies the relay route.

Notification bodies follow the configured language rule. Model identifiers and quoted error text
remain verbatim. Callers should return delivery success or failure and use the relay's base64 text
option for multiline messages that need to cross Windows command arguments.

### Inbound, user replies become actions (two-way)

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

#### Which channels are read, and the invariant that comes with it

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

#### Commands, the deterministic half

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
- **Schedule**: the `ingest` step of `tick.py` (Windows task **ScheduleReminderTick**, PT5M) runs
  `ingest_tick.run()` within its own time budget.
- **Owner only, fail closed.** A text reply counts only when its author is
  `registry.big_brother.user_id`, the same person the reaction path has always required. If no owner
  is configured `poll` RAISES instead of falling back to "anyone in the channel": these replies
  can start real execution, so an unresolvable owner has to close the gate rather than open it.

### Execution, when a reply asks for something to HAPPEN

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
[Delivery and work recovery](#delivery-and-work-recovery) for command and installation boundaries.

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
  unresolved and cannot complete.
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
  Background Task Core's stop and `--stop`) saves the intent and then terminates the runner's job within a
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
  after it releases the serial slot, so the next queued order does not wait for the poll. The
  `work` step of `tick.py` (task **ScheduleReminderTick**, PT5M) is the backstop and the crash
  reaper; it runs first in the tick, so a slow ingest step cannot delay it. A tick with no unreleased
  reservation and no queued, running or stopping order returns after one read-only query.

### How a downstream skill integrates (copy-paste)

```python
import subprocess, sys, os
REMINDER_DIR = os.path.join(os.path.expanduser("~/.claude/skills/schedule-reminder"), "scripts")  # or probe
def push(stream, text, username=None):
    cmd = [sys.executable, os.path.join(REMINDER_DIR, "relay.py"), "send", "--stream", stream, "--text", text]
    if username: cmd += ["--username", username]
    return subprocess.run(cmd).returncode == 0
```


## Notification receipts

The owner selects a business event after deciding its outcome. Reuse the same stable run ID,
phase and condition when a notification is retried. Provider attempts, timestamps and random
retry IDs are unsuitable business identities.

`notification_receipts.Event(owner, run_id, phase, condition, stream)` requires nonempty strings.
Its ID is `notification:v1:` followed by compact JSON for `[run_id, phase, condition]`. Reusing
that ID with a different owner, stream or request payload raises an event conflict. The request
hash covers content, attachment paths, channel, username, language, verbatim declarations,
fallback and an optional command policy.

### Storage and delivery

`notification_receipts.deliver(event, content, **options)` and
`notify.notify_event(event, content, **options)` return a persisted business-event receipt.
They use the existing reminder database through `store.admitted_connection` and `store._Tx`.
Both receipt reads and writes require the resolved companion path (private_data.py);
PRIVATE publication is enforced at commit time. The producer never creates or migrates
a database. Initialize the current store before enabling callers; missing storage or schema
remains an explicit error.

The database primary key and short `BEGIN IMMEDIATE` transactions arbitrate concurrent senders.
The producer commits a claim, prepares a target and payload, then commits a send-start marker
with `synchronous=FULL` before invoking transport. Network and command execution occur outside
the write transaction. Only the current token may finish its attempt.

| State | Meaning and retry rule |
| --- | --- |
| `pending` | An attempt owns the event. Reentry returns its receipt without sending. |
| `sent` | The boolean transport confirmed delivery. Reentry returns the same receipt. |
| `failed` | Preparation failed before transport started. Only `retry_failed=True` may claim another attempt. |
| `uncertain` | Delivery may have occurred, including partial chunks, a lost response, or an expired claim with a send-start marker. Never automatically resend. |

An expired pending claim becomes `failed` if it has no send-start marker, otherwise `uncertain`.
A late acknowledgement can resolve uncertainty for the same token; it cannot replace a newer
attempt. Reconcile external evidence before creating any separately identified follow-up.
The protocol does not promise exactly-once network delivery.

Receipts store identity, hashes, safe error codes, target fingerprints, claim times, attempts,
target-change history and an optional database-relative command snapshot reference. They do not
store message text, credentials or attachment bytes. Invalid
receipt fields fail closed, including malformed digests and incomplete target history.

### Options and preparation

| Option | Behavior |
| --- | --- |
| `channel_id`, `files`, `username` | Preserve explicit channel routing, attachments and webhook identity. Attachments travel on the first bot chunk only. Explicit channel/files never fall back to a DM. |
| `fallback` | `none` by default. An owner may explicitly select `big_brother`. The initial fallback is recorded in target history. |
| `retry_failed` | Boolean, default false. Authorizes delivery-only retry of a proven pre-send failure. |
| `allow_target_change` | Boolean, default false. Separately authorizes a changed resolved target during a failed-event retry. |
| `lease_seconds` | Positive finite claim lifetime, default 300 seconds. Expiry cannot make a started send safe to retry. |
| `language` | `zh-CN` checks the maintained notification rule. `preserve` explicitly retains an owner's existing language policy. |
| `verbatim` | A list of owner-declared fragments passed to the maintained language rule. |
| `redactor` | Python-only maintained owner callable. Changed redacted content on retry is rejected. |

The prepared registry is an independent snapshot. A registry change after preparation cannot
change the recipient. Bot attachment bytes are captured before the send marker and those same
bytes are transmitted. Changed content or attachment bytes on a later attempt are rejected.
No credential-bearing registry snapshot is persisted.

Set `SCHEDULE_NOTIFICATION_LANGUAGE_POLICY` to an absolute maintained policy path, or configure
`SCHEDULE_ROUTE_SCRIPTS_DIR` containing `notification_language.py`. A Python caller may pass
`language_policy_path` explicitly. Missing policy is a pre-send failure.

### Consumer and compatibility APIs

`notification_client.submit(owner, run_id, phase, condition, stream, content, **options)` is the
thin consumer API. It accepts `TASK_RUN_ID` or `SCHEDULE_RUN_ID` if its run ID is omitted. An absent
identity or invalid event is an explicitly unpersisted refusal. Dry runs are unpersisted and do
not send. If a producer exception prevents a definitive result, the client returns `reconcile`,
`persisted: null`, and the derived event ID. It does not claim that a durable send marker is absent.

`notify.notify_occurrence(run_id, content, ...)` delivers through the in-process relay on the
selected stream, or through `SCHEDULE_RELAY_CMD` when that override is set. `notify.notify(content, run_id=...)` provides a boolean wrapper;
only `sent` returns true. Existing `notify.notify(content)` callers get the boolean in-process
relay send until their owner supplies a stable occurrence identity. Their behavior does not change merely
because a task-level run ID appears in the environment.

`relay.send` remains boolean. The separate `relay.deliver` and `notify.deliver` APIs retain their
Discord message-ID receipts. Legacy text delivery remains available.

### UTF-8 command boundary

Run `python notification_receipts.py deliver` with one UTF-8 JSON object on stdin containing
`event`, `content` and optional `delivery` fields. This preserves newlines and Unicode without
PowerShell argument conversion. An optional event ID must match the derived identity. Output
contains `api_version`, `ok`, and the receipt; exit 0 means `sent`. Pending, failed and uncertain
return exit 1 with their receipt. Invalid input or unavailable storage returns a safe error.
The `get` subcommand accepts an event ID and reads its receipt without reconciling or sending.

An optional `command` policy preserves explicitly selected standalone notifiers. It specifies
argv, payload mode (`text`, `base64`, or `at-file`), a finite process timeout and optional cwd.
This timeout governs the notifier process, not any model call. Execution uses `llmcall.process`;
no model is dispatched and no provider routing is overridden. Text/base64 adapters use the
canonical relay chunker. Custom commands do not accept bot attachments or silently fall back
to a stream. Their fingerprint identifies the resolved argv/cwd/protocol, but cannot attest a
recipient hidden in a third-party script's own configuration.

`at-file` requires `message_file` whose UTF-8 content matches the prepared text. The producer
creates a unique snapshot in the PRIVATE `notification-payloads` directory beside its admitted
database. The command receives that snapshot, so an edit to the original file cannot change the
payload after preparation. The fingerprint retains the original file's identity. The receipt's
`payload_artifact` records `notification-payloads/<random-id>.txt` relative to the database directory.
Cleanup removes the snapshot before process invocation or when `llmcall.process` explicitly
confirms that the notifier's job is empty (`cleanup_confirmed`). A lost process response or unconfirmed cleanup retains
the snapshot and leaves delivery uncertain; retries do not invoke the command again. An abrupt
producer termination can also leave a private snapshot. Reconcile the retained artifact and confirm
the notifier's job is empty before removing it. An unresolved companion path prevents snapshot
creation and sending.

## Delivery and work recovery

The registry selects inbound channels, outbound streams and deterministic command handlers. Keep it in the initialized PRIVATE companion described in [deployment.md](#deployment). The [CLI contract](#contract) defines pool state and reminder operations.

### Inbound commands

A matching handler owns its message even when it fails. The dispatcher never sends that message to a model. Before invoking the handler, the worker stores a durable command attempt. Exit zero completes the attempt; a failure or interrupted outcome remains `command_failed`. Failed commands receive no success acknowledgement and are not automatically replayed.

Inspect the handler's effects and its PRIVATE inbound record before retrying. A handler can change external state before timing out. After resolving that uncertainty, use `ingest_tick.py --retry-command <full-inbound-id>` to rearm the same message. Ordinary polling does not rearm it; completed messages cannot be retried through this command.

Unmatched messages and reactions use the durable dispatcher. Saved plans retain the item and running-work IDs shown to the planner. A retry may mutate only IDs in that original authorization that remain eligible now; it checks just those IDs instead of listing every active item again. A saved wildcard stop is expanded to those work IDs; it cannot include later work. Previously authorized completed items can be reconciled with a read before confirming success. Action identities and success receipts prevent repeated actions when a confirmation needs another attempt. A rejected action (an ID the planner was not shown, a snooze without a time) is final and is reported in the confirmation; it does not keep the message pending. A failed action, an undelivered confirmation or a planner that returned no usable plan is retried on the next tick, at most three attempts; the record then becomes `failed`, and `ingest_tick.py --retry-command <full-inbound-id>` rearms it after you have checked its effects.

A newly discovered or registered channel first records a PRIVATE baseline of owner reactions on its recent 50 messages, then advances the text cursor without dispatching old content. The original baseline survives a failed cursor write and later retries. Each sweep reads a channel once and admits it before its reactions are read. Failure to fetch reactors, or a full reactor page that cannot establish a complete snapshot, leaves the channel unadmitted. Subsequent distinct owner reaction keys and new text are processed normally. Existing text cursors retain their prior reaction behavior.

### Work completion

Work orders use agent_task.py, agent_run.py and agent_tick.py. Finalization commits execution metadata and pool state in one generation-fenced SQLite transaction (`store.advance_work`). A failed transaction leaves the prior running state available to recovery.

An order completes when the executor's own verification command, run by the runner, exits 0, when the executor declares `verify: null`, or when its answer carries no valid final contract; in the last case its answer is the summary and nothing is run. Any other failing check starts the next round with the real output; an interrupted check (exit 124, 127 or 130) leaves `reconcile`. No second model reviews the work. See [Agent Center](#agent-center).

The launch/stop lock remains required: it keeps a stop out of the window between a runner's spawn and its PID receipt. PID creation-time checks, detached process survival and Job Object termination need platform tests. Synthetic state-transition checks do not establish those properties.

Model calls use the installed llmcall interface: `llmcall.call(prompt, mode="agent")` for agent work, and its default judge mode for text decisions. Routing, models, timeouts and fallback policy belong to that interface.

### Reminder delivery

Delivery uses notify.deliver, which calls the sibling relay module in the tick's own process (no child process per reminder). A stream without a webhook or channel falls back to the Big Brother DM, whose boolean success acknowledges delivery without a message ID. `SCHEDULE_RELAY_CMD` replaces the relay with a command for tests or a custom notifier.

Failed sends use bounded backoff. At the retry limit, delivery stops until explicitly rearmed. `snooze --until <timestamp>` resets delivery retries; moving an idempotent reminder's due time forward also rearms an exhausted delivery. An ordinary task blocked on a dependency remains eligible for reminders.

`tick --dry-run` previews due work without sending or changing items or audit events. A real tick also writes nothing when nothing is due: it scans on a read-only connection and opens a write connection only to claim a due reminder or close a lapsed one. It keeps no heartbeat row; Task Scheduler records each run. The preview requires an initialized database.

[Generated examples](../../../examples.json) contain synthetic records reproduced by tools/make_fixtures.py.

## Manual completion

Active human todos expose a `complete` offer labeled 标记完成. The console submits
the owner-issued revision and a durable request ID through `work-action`. No Agent
workspace, conversation context or scheduler is needed.

Completion applies the same state machine and dependency checks as `done --id`.
The owner saves the action receipt and transition to `done` in one transaction,
sets progress to 100 and records the end time. Replaying the same request returns
its receipt without creating work, sending a notification or repeating the transition.
A different request from a stale page is rejected.

While agent execution or an unconfirmed task handoff is active, completion is disabled.
First stop or finish that work. A confirmed Scheduler handoff releases only the request
reservation; Background Task Core owns the task's runtime and stop controls, and the todo remains open.
Signal records and terminal todos have no completion offer. A dependency failure
leaves the todo and receipt store unchanged. Completed todos retain their original
content and remain available in history.

## Dispatch identity

Inbound messages are dispatched individually with their channel and message IDs. The owner saves
the first parsed plan before executing it. A retry uses that plan and the same action keys, even
if another model response would have changed the wording or order. A completed dispatch reuses
its stored outcomes. Identified creates call `store.ensure_item` in process (what
`reminder.py ensure` wraps), which preserves the original keyed record and reviews cross-source
candidates before creating another item. Concurrent creates share the same storage transaction
and unique key.

Before the first action, new saved plans persist `action_identity_version: 2` and canonical
`action_keys`. Sorting JSON fields while writing or reading a plan cannot change those keys.
An older unversioned pending plan can replay only when every exact canonical key already has
a terminal `succeeded` or `rejected` outcome. Missing, failed or mismatched old keys require
reconciliation before any mutation. Inspect the original durable effects; a new occurrence ID
is not an automatic workaround for uncertain old execution. No existing inbound files are
rewritten merely by installing this source.

An explicit CLI caller should pass `--request-id`. Reuse it after a timeout; change it for a new
intentional occurrence. Calls without an identity retain the legacy immediate-dispatch path;
they cannot guarantee replay protection, so integrations must supply the source identity.
`--no-post` suppresses confirmation messages but still changes records.

Generic channels read active obligations across sources and omit status signals. A follow-up uses `update` with an exact
listed ID and a note. The note is appended once without replacing prior details. An `agent` action
can reference the same ID so its execution remains linked to the todo. Similarity is advisory;
different deadlines and obligations must not be merged automatically. The two reviewed
market-intel watchdog keys are displayed as signals; other records from that producer remain work.

For example, a second request to add an appendix to an existing Acme report should reference the
report's ID. A separately requested report for another date should have a new request ID.

Saved plans retain their authorized item and work IDs, and retries use the existing inbound
outcome ledger. A follow-up receipt binds its item and note, so changing either under the same
receipt identity is rejected. The first attempt lists every active item for the planner; a retry
with a saved plan reads only the saved targets, checking which authorized items are still active
and which authorized work orders still run.

If an outcome write is lost after a follow-up or linked work order was committed, retry may read
the matching owner receipt for a previously authorized item even after that item becomes terminal.
It verifies the original note or full request, origin and published work state; it does not use
historical authorization to make a new mutation. Missing or inconsistent evidence is rejected.
Confirmation messages count actual updated and reused records and include their IDs.

Saved stop plans also retain `authorized_work_generations`. A stop is bound to the exact observed
work item and generation, including explicit generation zero for legacy work without an operation
row. Missing generation evidence requires reconciliation. A completed stop can be recovered
read-only only for that same cancelled generation with confirmed cleanup; it cannot authorize
stopping a replacement run.

These guarantees cover record creation and plan replay. They do not prove that a model will always
recognize differently worded follow-ups, or provide exactly-once delivery of external messages.

## Deployment

One Windows task, **ScheduleReminderTick**, runs `scripts/tick.py` every 5 minutes. It is declared in
the repository's `automation/tasks.json` (paths only through `${SKILL_ROOT}` and `${COMPANION_ROOT}`) and
installed by the task registration authority (background-task-core `install-skill`), which generates
its launcher; the companion's optional `automation/tasks.local.json` holds this machine's choices. `tick.py` runs three steps in order,
each with its own result in the JSON summary; the exit code is the number of steps that raised:

| Step | Runs | Needs |
|---|---|---|
| work | `agent_tick.run()`: reap finished agent orders, launch the next queued one | store, relay, llmcall |
| remind | `store.tick()`: fire due reminders | store, relay |
| ingest | `ingest_tick.run()`: poll every readable channel, dispatch replies (budget `TASK_LIMIT_S`) | store, relay, llmcall |

`tick.py --only remind` (or `work`, `ingest`) runs one step; `--no-post` makes reminders a dry run
and skips channel confirmations. Change the task by editing the manifest or the companion override
and running `install-skill` again; this repository ships no installer of its own.

### Private runtime storage

Set `SCHEDULE_REMINDER_CONFIG` to an initialized PRIVATE versioned companion root (`tools/init_config.py
--out <root>`, then `tools/verify_config.py --json`). Its `data` directory is the default DATA home;
`SCHEDULE_REMINDER_DATA_DIR` may select another PRIVATE location. `AGENT_CENTER_CONFIG` selects the
registry file. Explicit `reminder.py --db` overrides `SCHEDULE_DB_PATH`. A write destination inside
the public source tree or Git metadata is refused, and a write below an uninitialized companion fails
instead of creating it. There is no in-repo fallback.

Initialize or additively upgrade the database with `reminder.py init`; reads do not migrate. The
SQLite file itself is not committed to the companion: `reminder.py export-text --out <dir>` writes
one `<table>.jsonl` per table, receipts and counters included (sorted, one JSON object per line), and the nightly companion sync
commits those instead. `reminder.py import-text --from <dir>` rebuilds a missing database from them.
Do not copy a live WAL database as an ordinary file.

`SCHEDULE_ACTION_WORKSPACE` must name an existing absolute directory inside the PRIVATE data
directory; each agent action creates its work UUID directory there. The companion tracks only the
summary of a finished agent run (`request.txt`, `events.jsonl`, each approach's `last.json` and
`answer.txt`); prompts, logs and workspaces stay local.

### Health

`reminder.py health` runs SQLite `quick_check` on an immutable read of the database and reports
`relay_ok` when the relay registry exists. It starts no process and sends nothing. Task Scheduler's
last run time and last result are the record of whether the tick ran.

When the registry names the bot token by `reader.bot_token_ref`, the credvault package must be
importable by the interpreter the task uses (`pythonw.exe -B`). A tick that cannot resolve the
reference logs `BotTokenUnavailable` with the cause; it reads no other copy of the token.

### Background Task Core linkage

`reminder_linked_items.py` is the read-only seam Background Task Core uses to list reminders linked to a
task through `ext.background_task_core.task_id`. It returns results only when the database carries a valid
`background_task_core_link_review` receipt in `meta`; without one the linkage is unreviewed and nothing is
returned. Reads never create or migrate a database.
