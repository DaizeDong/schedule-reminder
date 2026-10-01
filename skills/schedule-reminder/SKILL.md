---
name: schedule-reminder
description: Persistent store for todos, events, deadlines and progress with pending/doing/done states; fires due reminders via Discord; stable CLI/JSON API other skills call.
---

# schedule-reminder, the T0 schedule/memo base

> Governing principle (full text in `PHILOSOPHY.md`): **a base is the contract, not the storage.**
> Downstream skills depend on a frozen CLI/JSON surface, never on the database, so the engine can
> change forever without breaking them. Correctness (concurrency-safe, crash-safe, backward-compatible)
> beats features.

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
    reminder.py <verb>            <- the ONLY stable contract (JSON always); downstream calls via subprocess
[Windows task: PT5M heartbeat] -> reminder.py tick -> reconcile due items -> Discord relay (out)
[Windows task: PT10M ingest]   -> ingest_tick    -> poll every readable channel -> commands.py
                                                    (deterministic) or dispatch (LLM judge)  (in)
[Windows task: PT2M work]      -> agent_tick     -> reap the dead, launch one work order (do)
```

The Agent Center bus is **two-way**: `relay.py`/`digest.py` push out; `ingest.py`/`commands.py`/
`dispatch.py` pull user messages back in. A message matching a registered command is answered
deterministically by that handler; everything else uses the current shared llmcall routing
and becomes pool mutations. Both halves are single points on purpose: one
enumeration of which channels are read, one egress for everything sent. See
`reference/agent-center.md`.

A reply may also ask for something to **happen** rather than be recorded. That path is a third
scheduled task and a queue of work orders on this same pool, with a runner that has to hand back a
check which could have failed. Details in the same shard.

The OS task is only a heartbeat; `tick` reconciles the local table, so a slept/off machine catches
up **all** missed reminders on the next run (idempotent, at-least-once + dedupe).

## Command cheat-sheet

For a managed installation, resolve its current artifact runtime through the existing local
launcher binding and use that runtime's Python and schedule-reminder resource. Verify that
`ensure --help` is available. A linked source checkout may be older; do not fall back to its
legacy `add` command when creation review is unavailable.

```bash
python scripts/reminder.py init
python scripts/reminder.py creation-preflight --title "买牛奶" --due-at 2026-06-28T17:00:00Z
python scripts/reminder.py ensure --title "买牛奶" --due-at 2026-06-28T17:00:00Z --priority 1 \
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
`tags[]` · `source` · `idempotency_key` · `relations[]` (depends-on/...) · `recurrence` (RRULE,
`tick` rolls to the next occurrence) · `alarms[]` (per-alarm lead, e.g. `[{"lead":3600}]` /
`[{"trigger":"-PT15M"}]`) · `ext` (**unknown fields preserved**, namespace `x_<skill>_*`). Full
table -> `reference/contract.md`.

## Hard rules

1. **Downstream never reads the DB**, only `reminder.py <verb>`. Responses are always JSON.
2. **DB stays on local NTFS**, never OneDrive/GDrive/network (WAL lock + sync = corruption).
3. **State changes go through `transition`/`done`/`block`**, never `update` (state machine guarded).
4. **Always pass `--source` + `--idempotency-key`** on writes (audit + safe retries).
5. **Unknown fields are MUST-PRESERVE**, put extras in `--ext` as `x_<skill>_*`; the base round-trips
   them.
6. **All time is UTC RFC3339**; due trigger is the interval `now >= due_at - lead`, never `==`.
7. **Preflight across producers, then ensure.** Run `creation-preflight` with the proposed content,
   dates, project and external identifiers. Inspect candidate content, not just titles or source.
   Use `ensure` for new obligations: equivalent active records are reused inside one transaction.
   For a semantic follow-up, pass the reviewed `--reuse-id`, `--expected-revision` and `--note`;
   only the new note is appended. Preserve different orders, dates, recipients and alarm times.
   A changed due date uses `snooze` or `update` on the existing ID. Read
   `reference/integration.md` for `ERR_CREATION_REVIEW` and distinct occurrences.
8. **Retry with the original identity and fields.** Preserve source request/action IDs. `ensure`
   replays its recorded result without overwriting edits or reviving completed items. A new
   message or another channel alone does not establish a new obligation. Keep legacy `add`
   upserts for intentional producer state/heartbeat updates, not as a fallback after failed review.

## Progressive loading

When reviewing duplicates, read content across all producers, including actionable email reminders.
Keep one current obligation and link its earlier records with `x_console_consolidation.group_under`.
Use `duplicate_of` only for reviewed superseded copies; cancel those copies through `transition`,
preserving their source messages and original content. Preserve distinct orders, billing periods,
appointments and reminder times. The current engine fires at the earliest alarm lead, so do not
replace separate preparation/departure reminders with multiple alarms and assume both will fire.
Group those reminders for display while retaining their independently scheduled records.

Recurring background automation belongs to the `automation-management` skill and the existing
task registration authority. Do not create a Windows task for every reminder or follow-up.

This `SKILL.md` is the only always-loaded file. Load one shard on demand:

- `reference/contract.md`, frozen verbs, fields, states, error codes, idempotency, versioning.
- `reference/deployment.md`, DB init, heartbeat task, SQLite version, backup, secrets.
- `reference/integration.md`, copy-paste examples for downstream skills.
- `reference/agent-center.md`, the **two-way** Agent Center bus: outbound relay (`relay.py`) + daily
  当日总结 aggregator (`digest.py`), and inbound ingest (`ingest.py`/`dispatch.py`/`llm_chain.py`) that
  turns user channel replies into pool mutations. Egress + daily summary + reply ingress in one place.
  Also the execution tier (`agent_task.py`/`agent_run.py`/`agent_tick.py`) reached by the `agent` and
  `stop` ops, including why a work order is owner-only and how a run is proved finished.
