---
name: schedule-reminder
description: Persistent store for todos, events, deadlines and progress with pending/doing/done states; fires due reminders via Discord; stable CLI/JSON API other skills call.
---

# schedule-reminder, the T0 schedule/memo base

> Governing principle (full text in `PHILOSOPHY.md`): **a base is the contract, not the storage.**
> Downstream skills use the versioned CLI/JSON surface and documented read-only linkage seam,
> never database internals. Preserve concurrency, crash recovery and compatibility.

## When to use / when to stop

- **Use** when the user wants to track a todo / event / deadline / 进度 / 提醒 / 备忘, or when another
  skill (email-monitor, daily-hotspots, demand-mining, promotion-assistant) needs to **write a
  reminder** or **read task progress**.
- **Stop / route elsewhere:** a one-off "send me a Discord message now" with nothing to persist is
  just the relay, not this. This base is for state that must survive, be queried, and be reminded.

## How it works (one screen)

```
SQLite (WAL) single file          <- private storage, NEVER touched by downstream
  store.py  (typed functions)     <- in-process, trusted skills MAY import
    reminder.py <verb>            <- versioned CLI contract (JSON always); call via subprocess
    reminder_linked_items.py      <- reviewed read-only Task Console linkage
[Windows task: PT5M heartbeat] -> reminder.py tick -> reconcile due items -> Discord relay (out)
[Windows task: PT10M ingest]   -> ingest_tick    -> poll every readable channel -> commands.py
                                                    (deterministic) or dispatch (LLM judge)  (in)
[Windows task: PT2M work]      -> agent_tick     -> reap the dead, launch one work order (do)
```

The Agent Center bus is **two-way**: `relay.py`/`digest.py` push out; `ingest.py`/`commands.py`/
`dispatch.py` pull user messages back in. A message matching a registered command is answered
deterministically by that handler; everything else uses the installed `llmcall.call` judge policy
and becomes pool mutations or an idempotent work order. Both halves are single points on purpose: one
enumeration of which channels are read, one egress for everything sent. See
`reference/operations.md`.

A reply may also ask for something to **happen** rather than be recorded. That path is a third
scheduled task and a queue of work orders on this same pool, with a runner that has to hand back a
check which could have failed. Details in the same shard.

The OS task is only a heartbeat; `tick` reconciles the local table, so a slept/off machine catches
up **all** missed reminders on the next run (idempotent, at-least-once + dedupe).

## Installation and measured readiness

Initialize a PRIVATE versioned companion and set `SCHEDULE_REMINDER_CONFIG` to its root before writes.
`SCHEDULE_REMINDER_DATA_DIR` optionally selects DATA inside another proven PRIVATE repository.
An explicit `--db` takes precedence over `SCHEDULE_DB_PATH`. Reads do not initialize storage:
list reads may return empty, work-feed reports unavailable, and creation preflight requires an
initialized database. Writes fail until the PRIVATE database is explicitly initialized.

`scripts/install.ps1 -Capabilities store,remind -Plan` emits four capability rows without effects.
Omitting selection chooses store plus remind; explicit empty selection is a no-op. Ingest and work
are optional, with their own tasks and llmcall dependency. The installer fails when selected
readiness is incomplete, including after successful task registration.

`health` keeps its report-success JSON and exit zero. Inspect `health.readiness.ready` for readiness.
Selection uses `SCHEDULE_CAPABILITIES` or `health --capabilities`. Readiness requires PRIVATE store,
actual task readback, imports under the selected interpreter, and recent identity-bound worker
receipts. A path, static file, process exit, or synthetic receipt cannot prove external delivery.
The reminder worker writes normal private evidence after a confirmed relay delivery; health only
reads it. Selected ingest/work remain unmeasured until their own success evidence is available.
See `reference/deployment.md` for the task and evidence contract.

## Command cheat-sheet

```bash
python scripts/reminder.py init
python scripts/reminder.py add --title "买牛奶" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source my-skill --idempotency-key my-skill:42 --ext '{"x_my_skill_id":"42"}'
python scripts/reminder.py get  --id <ID>
python scripts/reminder.py list --active --source my-skill --limit 50
python scripts/reminder.py creation-preflight --title "Prepare Acme report" --source my-skill --idempotency-key my-skill:report-1
python scripts/reminder.py work-feed --limit 100 --event-limit 50
python scripts/reminder.py transition --id <ID> --to doing --progress 30
python scripts/reminder.py done --id <ID>
python scripts/reminder.py block --id <ID> --blocker-id <OTHER> --reason "waiting"
python scripts/reminder.py snooze --id <ID> --until 2026-07-01T09:00:00Z
python scripts/reminder.py due  --now 2026-06-28T17:00:00Z          # read-only
python scripts/reminder.py tick --now 2026-06-28T17:00:00Z          # scheduler calls this
python scripts/reminder.py health
```

Success -> JSON on stdout with `api_version`, `schema_version`, `ok:true`. Failure -> JSON on stderr
with `error_code` + exit 1. Inject a clock with `--now`/`SCHEDULE_NOW`; isolate state with
`--db`/`SCHEDULE_DB_PATH`.

## Item fields (essentials)

`id` (immutable UUIDv7) · `kind` (task|event) · `title` · `state`
(pending/doing/done/blocked/cancelled) · `progress` 0-100 · `priority` 0-9 · `due_at` (RFC3339 UTC) ·
`tags[]` · `source` · `idempotency_key` · `relations[]` (depends-on/...) · `recurrence` (RRULE,
`tick` rolls to the next occurrence) · `alarms[]` (per-alarm lead, e.g. `[{"lead":3600}]` /
`[{"trigger":"-PT15M"}]`) · `ext` (**unknown fields preserved**, namespace `x_<skill>_*`). Full
table -> `reference/contract.md`.

## Hard rules

1. **Downstream never reads the DB**. Use the CLI or the separately documented read-only linkage seam.
2. **DB stays on local NTFS**, never OneDrive/GDrive/network (WAL lock + sync = corruption).
3. **State changes go through `transition`/`done`/`block`**, never `update` (state machine guarded).
4. **Before creating, compare existing obligations, sources and occurrence dates** with
   `creation-preflight`. Use `ensure` to reuse an equivalent item or explicitly review a similar
   candidate. Append a follow-up using `--reuse-id`, its `--expected-revision` and `--note` under a
   new request identity. Reuse that identity and exact payload for retries. Different occurrences
   need different identities. `add` defaults to updating a keyed item; `--if-exists return` preserves it.
5. **Unknown fields are MUST-PRESERVE**, put extras in `--ext` as `x_<skill>_*`; the base round-trips
   them.
6. **All time is UTC RFC3339**; due trigger is the interval `now >= due_at - lead`, never `==`.

## Progressive loading

This `SKILL.md` is the only always-loaded file. Load one shard on demand:

- `reference/contract.md`, frozen verbs, fields, states, error codes, idempotency, versioning.
- `reference/deployment.md`, DB init, heartbeat task, SQLite version, backup, secrets.
- `reference/integration.md`, copy-paste examples for downstream skills.
- `reference/operations.md`, inbound commands, explicit retry, atomic work finalization,
  notification recovery and the installed llmcall interface.
- `reference/linkage-review.md`, exact Task Console linkage and its read-only owner seam.
- `reference/notification-receipts.md`, business-event identity, delivery receipts and uncertainty.

`work-action` consumes an owner-issued offer and revision from `work-feed`. Manual completion
uses the existing dependency/state guards and requires no executor. Execution and task handoff
have separate receipts; a Scheduler acknowledgement does not complete the todo. See the contract
for stdin schemas and [manual completion](../../docs/manual-completion.md).

CLI output is always JSON. Global --db and --actor options precede the verb; --json is not supported.
