# schedule-reminder, Design Philosophy

> One test governs every change: **does it fix the framing, or just patch a symptom?**

schedule-reminder is a **T0 infrastructure base**: four other skills (email-monitor, daily-hotspots,
demand-mining, promotion-assistant) will write reminders into it and read task progress out of it.
That single fact dictates every decision below. A base is judged not by what it can do, but by what
it can never break.

## P1, A base is the *contract*, not the storage

- **Symptom patch:** let each downstream skill read/write the database directly (markdown files, a
  shared JSONL, raw SQL). Fast to start.
- **Root cause:** the moment four skills couple to the storage layout, the storage can never change ,
  every schema tweak is a breaking change across the fleet, and concurrent writers corrupt shared
  files.
- **Decision it produced:** three layers, SQLite (private) → `store.py` (in-process) →
  `reminder.py <verb> --json` (the **only** public surface). Downstream depends on a frozen CLI/JSON
  contract with an `api_version`; the engine underneath can be rewritten freely. E11 golden-tests the
  verb set, field set, and state enum so a breaking change cannot ship silently.

## P2, Correctness beats features

- **Symptom patch:** ship recurrence expansion, calendars, NLP date parsing first, the demo-able
  stuff.
- **Root cause:** a base that loses a write, corrupts under concurrency, or drops a reminder is
  worse than no base, because downstream skills trusted it. Flashy features on an unreliable base are
  negative value.
- **Decision it produced:** v0.1 invests entirely in the hard guarantees, concurrency-safe writes
  (BEGIN IMMEDIATE + optimistic CAS + in-process lock + BUSY back-off), crash-safe persistence
  (SQLite WAL), a guarded state machine, idempotent writes, and at-least-once delivery with dedupe.
  Recurrence/RRULE expansion and a richer alarm model are deliberately deferred to the roadmap.

## P3, Reconcile, don't trigger

- **Symptom patch:** create one OS scheduled trigger per event so each fires "on time".
- **Root cause:** OS triggers explode in number, can't model state, and silently skip when the
  machine sleeps, `cron`/`schtasks`/`anacron` catch up *once* at best.
- **Decision it produced:** the OS runs a *single* PT5M heartbeat; a stateless `tick` reconciles the
  local table each run. Missed fires are caught up idempotently and repeatedly from durable state,
  not from any OS catch-up feature. The due trigger is the interval `now >= due_at - lead`, never the
  `now == due_at` equality that always misses.

## P4, Be unbreakably backward-compatible

- **Symptom patch:** parse only the fields you know; drop the rest.
- **Root cause:** if the base drops a field it doesn't recognise, the first time it rewrites a record
  it silently erases the extension data a downstream skill stored there, destroying the very
  extensibility a base exists to provide.
- **Decision it produced:** unknown fields are **MUST-PRESERVE**, round-tripped through an `ext`
  container (`x_<skill>_*` namespace), exactly like iCalendar X-PROP / Taskwarrior UDA. Schema
  evolves additively only (`PRAGMA user_version`); records carry a tolerant `schema_version`. E12
  is a red-line regression gate.

## P5, Prove it, don't trust it

- **Symptom patch:** "looks correct", ship it.
- **Root cause:** concurrency and crash-recovery bugs are invisible until they cost data; a base must
  *demonstrate* its guarantees.
- **Decision it produced:** 13 evaluation signals (E1-E13) drive the frozen CLI via subprocess and
  assert JSON, CRUD, the full transition table (legal + illegal), invariants, due/idempotency/
  missed-fire/retry, concurrent writes with `PRAGMA integrity_check`, concurrent read/write, idempotent
  dedupe, the API golden, unknown-field preservation, and health. E8/E9/E11/E12 are merge-blocking
  red lines.
