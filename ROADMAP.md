# Roadmap

Current: **v0.3.0**

## v0.3.0 (current) — Agent Center backend
- **Unified relay** (`relay.py`): the single Discord egress for every skill — multi-stream webhooks
  with per-stream identity, registry-driven (`the Agent Center registry`), Big-Brother fallback,
  mandatory User-Agent.
- **Daily digest aggregator** (`digest.py`): one daily task assembles every installed skill's
  当日总结段 into a single Big-Brother summary; pluggable contributors, fail-soft per section.
- See `reference/agent-center.md`. (reminder contract api_version unchanged → 1.0.0.)

## v0.2.0
- **RRULE rolling recurrence** (§2.4): fired `recurrence` items roll to the next future occurrence
  and re-arm; minimal stdlib `FREQ`/`INTERVAL`/`UNTIL` subset + `exdate` skip; series never
  materialised. (E14)
- **Per-alarm lead times** (§4.5): `alarms[]` (`{"lead":secs}` / iCal `{"trigger":"-PT15M"}`) fire an
  item before `due_at`; applied by `due` + `tick`. (E15)
- Concurrency hardening: exclusive `claimed_at` claim + stale reclaim → no concurrent-tick
  double-fire. `progress` 0-100 enforced on every write (`ERR_BAD_PROGRESS`).
- New additive `add` flags (`--recurrence`/`--rdate`/`--exdate`/`--alarms`); contract still
  `api_version 1.0.0` (item field + verb sets unchanged). 41-test suite (E1-E15).

## v0.1.0
- T0 base: SQLite (WAL) storage with `store.py` engine.
- Unified `item` model (event/task), immutable UUIDv7 keys, append-only `events` audit stream.
- Guarded state machine (pending/doing/done/blocked/cancelled) + write-time invariants.
- Frozen CLI/JSON contract `reminder.py <verb>` (`api_version 1.0.0`).
- Concurrency safety: BEGIN IMMEDIATE writes, optimistic CAS, in-process write lock, BUSY back-off.
- Idempotent writes (UPSERT on idempotency_key); MUST-PRESERVE unknown fields via `ext`.
- Due reminders: single PT5M heartbeat + stateless `tick` reconciliation, at-least-once + dedupe,
  exponential retry back-off then `blocked` + alert; pluggable notify channel (Discord relay).
- `install.ps1` (DB + scheduled task + junction + health); 35-test acceptance suite (E1-E13).

## Planned
- **v0.2.x** — RRULE `BYDAY`/ordinal (`1FR`/`-1SU`) + `COUNT`; `rdate` extra-occurrence merge;
  `VACUUM INTO` backup helper + scheduled cold-snapshot task; idle WAL checkpoint.
- **v0.3** — optional MCP wrapper over the same `store.py` (only if a cross-client/remote need
  appears); richer `query` filters (tag/project/priority ranges).
- **v0.3** — agent-skills-eval lift (G1) + held-out trigger-rate optimization (G2) wired into CI.
- **Backlog** — archival/cold-store job for aged done/cancelled items; alternate channels
  (feishu/email) behind the same `notify()` seam.
