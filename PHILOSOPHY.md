# schedule-reminder, Design Philosophy

schedule-reminder provides persistent state and a stable integration contract for downstream
skills, including email-monitor, daily-hotspots, demand-mining and promotion-assistant.
The v0.1 decisions and E1-E13 evidence below explain the original design; current capabilities
and verification requirements are documented in [ROADMAP.md](ROADMAP.md) and the references.

## P1, A base is the *contract*, not the storage

Direct database access couples every consumer to the storage layout, making schema changes
breaking changes across integrations. The design separates SQLite storage, trusted in-process
`store.py` functions and the versioned `reminder.py <verb>` CLI/JSON surface. Task Console also
uses a documented read-only linkage seam; direct database mutation remains unsupported.
The `api_version` contract permits engine changes without changing consumers. E11 golden-tests
the verb set, field set and state enum to detect incompatible changes.

## P2, Correctness beats features

Lost writes, concurrency corruption and dropped reminders invalidate downstream assumptions.
v0.1 therefore prioritized short `BEGIN IMMEDIATE` writes, optimistic CAS, an in-process lock
and BUSY back-off; SQLite WAL for crash recovery; guarded state transitions; idempotent writes;
and at-least-once delivery with deduplication. Recurrence and richer alarms were deferred at
that stage and added in v0.2, as recorded in [CHANGELOG.md](CHANGELOG.md).

## P3, Reconcile, don't trigger

One OS trigger per event scales the trigger count and cannot represent reminder state. Sleeping
machines can miss triggers, while OS catch-up behavior alone cannot reconcile durable work.
A single PT5M heartbeat runs a stateless `tick` against the local table. It catches up missed
fires idempotently from persisted state. The due condition is `now >= due_at - lead`, rather
than an exact-time equality that can miss an event between ticks.

## P4, Be unbreakably backward-compatible

Dropping unrecognized fields would erase downstream extension data during ordinary rewrites.
Unknown fields are therefore MUST-PRESERVE through the `ext` container and `x_<skill>_*`
namespace, following iCalendar X-PROP and Taskwarrior UDA. Schema changes are additive
(`PRAGMA user_version`); records carry a tolerant `schema_version`. E12 blocks regressions
in unknown-field preservation.

## P5, Prove it, don't trust it

The original 13 evaluation signals (E1-E13) exercised the frozen CLI through subprocesses
and JSON assertions. They covered CRUD, legal and illegal state transitions, write invariants,
due/idempotency/missed-fire/retry behavior, concurrent writes with `PRAGMA integrity_check`,
concurrent reads and writes, deduplication, the API golden, unknown-field preservation and health.
E8/E9/E11/E12 remain merge-blocking checks. Current acceptance adds E14-E15 for recurrence and
alarms; a source test result still does not establish installed readiness or external delivery.
