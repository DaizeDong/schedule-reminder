# Roadmap

Current: **v0.1.0**

## v0.1.0 (current)
- T0 base: SQLite (WAL) storage with `store.py` engine.
- Unified `item` model (event/task), immutable UUIDv7 keys, append-only `events` audit stream.
- Guarded state machine (pending/doing/done/blocked/cancelled) + write-time invariants.
- Frozen CLI/JSON contract `reminder.py <verb> --json` (`api_version 1.0.0`).
- Concurrency safety: BEGIN IMMEDIATE writes, optimistic CAS, in-process write lock, BUSY back-off.
- Idempotent writes (UPSERT on idempotency_key); MUST-PRESERVE unknown fields via `ext`.
- Due reminders: single PT5M heartbeat + stateless `tick` reconciliation, at-least-once + dedupe,
  exponential retry back-off then `blocked` + alert; pluggable notify channel (Discord relay).
- `install.ps1` (DB + scheduled task + junction + health); 35-test acceptance suite (E1-E13).

## Planned
- **v0.2** — RRULE recurrence expansion (rolling next-due via dateutil; rdate/exdate); per-item alarm
  lead times (`alarms[]` triggers) instead of a global `--lead`.
- **v0.2** — `VACUUM INTO` backup helper + scheduled cold-snapshot task; idle WAL checkpoint.
- **v0.3** — optional MCP wrapper over the same `store.py` (only if a cross-client/remote need
  appears); richer `query` filters (tag/project/priority ranges).
- **v0.3** — agent-skills-eval lift (G1) + held-out trigger-rate optimization (G2) wired into CI.
- **Backlog** — archival/cold-store job for aged done/cancelled items; alternate channels
  (feishu/email) behind the same `notify()` seam.
