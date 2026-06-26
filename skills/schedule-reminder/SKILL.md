---
name: schedule-reminder
description: Persistent store for todos, events, deadlines and progress with pending/doing/done states; fires due reminders via Discord; stable CLI/JSON API other skills call.
---

# schedule-reminder — the T0 schedule/memo base

> Governing principle (full text in `PHILOSOPHY.md`): **a base is the contract, not the storage.**
> Downstream skills depend on a frozen CLI/JSON surface, never on the database — so the engine can
> change forever without breaking them. Correctness (concurrency-safe, crash-safe, backward-compatible)
> beats features.

## When to use / when to stop

- **Use** when the user wants to track a todo / event / deadline / 进度 / 提醒 / 备忘, or when another
  skill (email-monitor, daily-hotspots, demand-mining, promotion-assistant) needs to **write a
  reminder** or **read task progress**.
- **Stop / route elsewhere:** a one-off "send me a Discord message now" with nothing to persist is
  just the relay — not this. This base is for state that must survive, be queried, and be reminded.

## How it works (one screen)

```
SQLite (WAL) single file          <- private storage, NEVER touched by downstream
  store.py  (typed functions)     <- in-process, trusted skills MAY import
    reminder.py <verb>            <- the ONLY stable contract (JSON always); downstream calls via subprocess
[Windows task: PT5M heartbeat] -> reminder.py tick -> reconcile due items -> Discord relay
```

The OS task is only a heartbeat; `tick` reconciles the local table, so a slept/off machine catches
up **all** missed reminders on the next run (idempotent, at-least-once + dedupe).

## Command cheat-sheet

```bash
python scripts/reminder.py init
python scripts/reminder.py add --title "买牛奶" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source my-skill --idempotency-key my-skill:42 --ext '{"x_my_skill_id":"42"}'
python scripts/reminder.py get  --id <ID>
python scripts/reminder.py list --active --source my-skill --limit 50
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
`tags[]` · `source` · `idempotency_key` · `relations[]` (depends-on/...) · `recurrence` (RRULE —
`tick` rolls to the next occurrence) · `alarms[]` (per-alarm lead, e.g. `[{"lead":3600}]` /
`[{"trigger":"-PT15M"}]`) · `ext` (**unknown fields preserved** — namespace `x_<skill>_*`). Full
table -> `reference/contract.md`.

## Hard rules

1. **Downstream never reads the DB** — only `reminder.py <verb> --json`. (Lets the engine evolve.)
2. **DB stays on local NTFS** — never OneDrive/GDrive/network (WAL lock + sync = corruption).
3. **State changes go through `transition`/`done`/`block`**, never `update` (state machine guarded).
4. **Always pass `--source` + `--idempotency-key`** on writes (audit + safe retries).
5. **Unknown fields are MUST-PRESERVE** — put extras in `--ext` as `x_<skill>_*`; the base round-trips
   them.
6. **All time is UTC RFC3339**; due trigger is the interval `now >= due_at - lead`, never `==`.

## Progressive loading

This `SKILL.md` is the only always-loaded file. Load one shard on demand:

- `reference/contract.md` — frozen verbs, fields, states, error codes, idempotency, versioning.
- `reference/deployment.md` — DB init, heartbeat task, SQLite version, backup, secrets.
- `reference/integration.md` — copy-paste examples for downstream skills.
