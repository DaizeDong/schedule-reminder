# schedule-reminder, Frozen external contract (`api_version 1.0.0`)

> This is the **only** surface downstream skills may depend on. Call `reminder.py <verb>` via
> subprocess and parse stdout JSON (JSON is always emitted, there is no `--json` flag). **Never**
> read the `.db` file, build SQL, or import internal tables. Everything below is additive-only within
> `api_version 1.x`; any delete/rename/semantic change bumps `api_version` and runs a dual-version
> transition period.

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
| `init` | create/upgrade DB (idempotent) |, | `{db_path, schema_user_version}` |
| `add` | create item | `--title` (req), `--kind`, `--due-at`, `--state`, `--priority`, `--progress` (0-100), `--tags a,b`, `--source`, `--idempotency-key`, `--description`, `--ext JSON`, `--recurrence RRULE`, `--rdate JSON`, `--exdate JSON`, `--alarms JSON` | `{item}` |
| `get` | fetch by id | `--id` | `{item}` |
| `list` / `query` | filter + keyset page | `--state`, `--source`, `--kind`, `--due-before`, `--active`, `--limit`, `--cursor` | `{items[], next_cursor}` |
| `update` | patch fields (not state) | `--id`, `--set field=value` (repeatable), `--ext JSON`, `--idempotency-key` | `{item}` |
| `transition` | state move (state machine + CAS) | `--id`, `--to`, `--expect`, `--reason`, `--progress` | `{item}` or error |
| `done` | mark complete | `--id` | `{item}` (`end_at` set, `progress=100`) |
| `block` | mark blocked | `--id`, `--blocker-id`, `--reason` | `{item}` |
| `snooze` | suppress reminders until T | `--id`, `--until` | `{item}` |
| `due` | read items due now (read-only) | `--now`, `--lead` | `{items[], now}` |
| `tick` | dispatch due reminders (scheduler) | `--now`, `--lead`, `--dry-run` | `{dispatched[], retried[], blocked[], skipped[], now}` |
| `events` | audit trail of an item | `--id` | `{events[]}` |
| `health` | self-check | `--check-task` | `{health{...}}` |

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
| `notified_at`/`next_retry_at`/`retry_count`/`claimed_at` |, | delivery bookkeeping |
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

## Error codes

`ERR_NOT_FOUND` · `ERR_BAD_INPUT` · `ERR_BAD_KIND` · `ERR_BAD_STATE` · `ERR_BAD_PROGRESS` ·
`ERR_BAD_FIELD` · `ERR_BAD_TIME` · `ERR_BAD_JSON` · `ERR_ILLEGAL_TRANSITION` (carries `current`,
`to`, `allowed[]`) · `ERR_STATE_CONFLICT` (carries `current`, `expected`) · `ERR_DEPENDENCY_UNMET`
(carries `unmet[]`) · `ERR_BLOCK_REASON_REQUIRED` · `ERR_USE_TRANSITION` · `ERR_BUSY` ·
`ERR_INTERNAL`.

## Idempotency

`add`/`update` accept `--idempotency-key`. Re-issuing `add` with the same key returns the **same
item id** (UPSERT, ext merged), safe to retry. Reads (`get`/`list`/`query`/`due`) are naturally
idempotent. Compose the key from your skill + your own record id, e.g. `email-monitor:msg-8841`.

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
