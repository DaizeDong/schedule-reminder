# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.1.0] - 2026-06-25
### Added
- T0 schedule/memo base: SQLite (WAL) storage engine (`store.py`).
- Unified `item` model (event/task) with immutable UUIDv7 keys and an append-only `events` audit
  stream written in the same transaction as the projection.
- Guarded state machine (pending/doing/done/blocked/cancelled) with write-time invariants
  (done → end_at + progress=100; depends-on enforcement; blocked needs blocker/reason).
- Frozen external contract `reminder.py <verb> --json` (`api_version 1.0.0`): add/get/list/query/
  update/transition/done/block/snooze/due/tick/events/health.
- Concurrency safety: BEGIN IMMEDIATE writes, optimistic CAS, in-process write lock, bounded BUSY
  back-off; PRAGMA WAL / busy_timeout=10000 / synchronous=NORMAL / foreign_keys=ON.
- Idempotent writes (UPSERT on idempotency_key); MUST-PRESERVE unknown fields via the `ext` container
  (`x_<skill>_*` namespace).
- Due-reminder dispatch: single PT5M heartbeat + stateless `tick` reconciliation (free missed-fire
  catch-up), at-least-once delivery + dedupe, exponential retry back-off then `blocked` + self-alert;
  pluggable `notify()` channel (default Discord relay, swappable via `SCHEDULE_RELAY_CMD`).
- `install.ps1` idempotent installer (DB init + scheduled task + junction + health).
- 35-test acceptance suite (E1-E13) driving the CLI via subprocess; E8/E9/E11/E12 red-line gates.

### Known limitations
- Recommends SQLite ≥ 3.51.3 (WAL-reset bug); `health` warns rather than hard-failing on older hosts.
- Recurrence/RRULE is stored but not yet expanded (roadmap v0.2).
