# schedule-reminder, Frozen external contract (`api_version 1.0.0`)

> Call `reminder.py <verb>` via
> subprocess and parse stdout JSON (JSON is always emitted, there is no `--json` flag). **Never**
> read the `.db` file, build SQL, or import internal tables. Everything below is additive-only within
> `api_version 1.x`; any delete/rename/semantic change bumps `api_version` and runs a dual-version
> transition period.

Task Console also has the separately documented, read-only `reminder_linked_items` owner seam;
its reviewed authority and versioning are described in [linkage review](linkage-review.md).

Contents: [Invocation](#invocation) · [Verbs](#verbs) · [Item fields](#item-fields) ·
[States](#states--transitions) · [Error codes](#error-codes) · [Idempotency](#idempotency) ·
[Time](#time) · [Unknown fields](#unknown-fields-must-preserve) · [Versioning](#versioning)

## Invocation

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

## Verbs

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
| `tick` | dispatch due reminders (scheduler) | `--now`, `--lead`, `--dry-run` | `{dispatched[], retried[], blocked[], skipped[], now}` |
| `events` | audit trail of an item | `--id` | `{events[]}` |
| `health` | self-check | `--check-task`, `--capabilities` | `{health{...}}` |
| `work-feed` | read-only work/action projection | `--limit` (1-10000), `--event-limit` (0-1000) | `{schemaVersion:1, available, items[], events[], sources[], coverage, queue}` |
| `work-action` | accept an offered action | bounded JSON stdin, below | `{schemaVersion:1, status, action, wakeup, dispatch?}` |
| `work-action-stop` | persist a stop request | bounded JSON stdin, below | action receipt |
| `work-action-result` | record linked-task controller result | bounded JSON stdin, below | action receipt |

`--actor NAME` (global) records who acted in the audit stream, pass your skill name.

## Item fields

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

## States & transitions

`pending → doing|blocked|done|cancelled` · `doing → done|blocked|pending|cancelled` ·
`blocked → doing|pending|done|cancelled` · `done → pending` (reopen) ·
`cancelled → pending` (reopen). `done`/`cancelled` are protected terminals.

Write-time invariants (enforced at the store, not the caller): `done` requires all `depends-on`
targets done and sets `end_at`+`progress=100`; `cancelled` sets `end_at`; `blocked` needs an unmet
blocker or a `reason`; state changes go through `transition`/`done`/`block` (never `update`).
`block --blocker-id` commits the dependency relation and blocked state together. A rejected state
change or failed audit-event write leaves both the item and its prior events unchanged.

## Error codes

`ERR_NOT_FOUND` · `ERR_BAD_INPUT` · `ERR_BAD_KIND` · `ERR_BAD_STATE` · `ERR_BAD_PROGRESS` ·
`ERR_BAD_FIELD` · `ERR_BAD_TIME` · `ERR_BAD_JSON` · `ERR_ILLEGAL_TRANSITION` (carries `current`,
`to`, `allowed[]`) · `ERR_STATE_CONFLICT` (carries `current`, `expected`) · `ERR_DEPENDENCY_UNMET`
(carries `unmet[]`) · `ERR_BLOCK_REASON_REQUIRED` · `ERR_USE_TRANSITION` · `ERR_BUSY` ·
`ERR_INTERNAL` · `ERR_DATA_POLICY` · `ERR_UNINITIALIZED` · `ERR_PERMISSION` · `ERR_CONFLICT` ·
`ERR_CREATION_REVIEW` (carries candidate IDs and revisions). Action errors use their own safe
codes, including `stale_recommendation`, `request_conflict`, `another_action_active` and
`action_schema_upgrade_required`.

## Idempotency

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

## Work projection and action requests

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
one `dispatch` instruction for Task Console; replay does not issue another instruction.
`work-action-result` accepts `{action_id: <receipt ID>, result: <controller result>}` and updates
only a receipt still awaiting dispatch. The action saves the reviewed task ID, exact controller
name and review revision before dispatch; a result for another controller cannot satisfy it.
A canonical acknowledged `run_requested`, or observed
`already_running`, releases the handoff reservation as `task_requested` and leaves the todo state
unchanged. Incomplete acknowledgement or uncertain cleanup remains `reconcile`; Task Console owns
task runtime and stop control.
Historical task receipts without that identity binding remain `reconcile`.

`work-action-stop` accepts the same four request fields, with `action_id` set to the current agent
receipt ID. The action revision includes the observed execution generation; stopping passes that
captured generation to the runtime so a replacement run cannot inherit the old stop request.
Stop intent revokes preparation and runner ownership before external cleanup. A queued
order that never started is cancelled atomically. Started or uncertain work remains reserved until
the matching stop result confirms it; a process death alone is insufficient. Stale revisions need
a fresh observation. See [manual completion](../../../docs/manual-completion.md).

## Time

UTC RFC3339 with microsecond precision everywhere (string order == time order). `tick`/`due`
compare in UTC; trigger uses `now >= due_at - lead` (an interval; never `now == due_at`). Inject a
clock with `--now`/`SCHEDULE_NOW`.

## Recurrence & alarms

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

## Unknown fields (MUST-PRESERVE)

Fields the base does not recognise are round-tripped verbatim through `ext`. Namespace downstream
extensions as `x_<skill>_*` (e.g. `x_promotion_campaign_id`). The base never drops them, even when
it rewrites other fields, this is what keeps the base safely extensible.

## Versioning

| Layer | Version | Rule |
|---|---|---|
| DB schema | `PRAGMA user_version` | additive migrations only; never rename/drop/retype |
| record | per-row `schema_version` | tolerant read; unknown fields preserved in `ext` |
| contract | `api_version` (this doc) | additive-only; delete/rename/semantic change bumps it + dual-run |

**Freeze first, integrate second.** This contract is frozen at `api_version 1.0.0`. Downstream
skills (#2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant) integrate
against it; the regression suite (E11) golden-compares verbs + item fields + state enum on every
change.

## Atomic finalization and delivery recovery

`transition` and `done` accept optional `--ext JSON`. State and metadata commit together; errors roll both back. Calls without `--ext` retain same-state no-op behavior. Unknown ext fields remain preserved.

Exhausted delivery is excluded from later ticks until rearmed, independently of an ordinary task's blocked state. Explicit `snooze` resets retry bookkeeping, and moving an idempotent reminder's due time forward rearms exhausted delivery. A dry-run tick makes no persistent item, event or watchdog changes. These are repaired recovery rules, not evidence that earlier versions implemented them. See [operations.md](operations.md).
