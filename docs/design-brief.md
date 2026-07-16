# Design Brief — schedule-reminder

> Step 0 (research-first) was completed in the planning phase. Full rationale and 6-sub-study source
> coverage live in `CodesResearch/_skill-builds/03-schedule-reminder/ARCHITECTURE.md` (ARCH v1.0).
> This brief is the auditable digest.

## Best references (match-or-beat)

- **SQLite WAL concurrency** — sqlite.org (whentouse / wal.html), tenthousandmeters & berthub
  benchmarks, oldmoe, skypilot. Single-writer WAL at 70k-100k tx/s beats most "multi-writer" DBs for
  this local workload.
- **Data model** — iCalendar VTODO (RFC 5545), Taskwarrior task.md RFC, todo.txt, org-mode. Unified
  `item` record (kind ∈ event/task), immutable UUIDv7 key, iCalendar 0-9 priority, RRULE recurrence,
  STATUS mapping.
- **Delivery semantics** — microservices.io idempotent-consumer, temporal.io; at-least-once + unique
  dedupe (exactly-once is unobtainable).
- **API/contract** — Claude Skills best-practices, circleci (MCP vs CLI), zuplo/milanjovanovic
  (additive evolution), sqlite user_version. CLI+JSON is the documentable/versionable/testable
  contract; MCP deferred.

## Frontier ideas incorporated

- Single OS heartbeat + stateless reconciliation (not per-event triggers) → free, repeatable
  missed-fire catch-up.
- MUST-PRESERVE unknown fields (iCalendar X-PROP / Taskwarrior UDA) via an `ext` container → safe
  downstream extensibility.
- Append-only `events` audit stream in the same write transaction as the projection → observable
  concurrent-merge ordering without O(N) folding for point reads.

## Anti-patterns avoided (red lines)

Session cron as the reminder engine · downstream reading the DB directly · default `BEGIN`
(DEFERRED) write-after-read → instant SQLITE_BUSY · DB on a sync/network drive · long write tx with a
push inside · per-event OS triggers · dropping unknown fields · hard delete / recyclable integer ids
as foreign keys · materialising infinite recurrences · `now == due_at` equality · local-time naive
datetimes · SQLite < 3.51.3 (WAL-reset bug).

## Proof bar (acceptance gate + self-evolve signals)

E1-E15, all programmatically adjudicated via the frozen CLI (subprocess + JSON asserts), with
injected clock + relay stub. Red-line gates: E8 (concurrent no-corruption + integrity_check), E9
(concurrent read/write), E11 (API contract golden), E12 (unknown-field preservation). Status: **86
passed** (E1-E15 acceptance + hermetic relay/digest/heartbeat/notify/ingest-dispatch module tests).

## Scope & focus (one job, ≤3 modules)

One job: a persistent schedule/memo base with due reminders and a stable API. Three modules:
`store.py` (engine), `reminder.py` (CLI contract), `notify.py` (pluggable channel). v0.2 added RRULE
rolling recurrence (§2.4) and per-alarm lead times (§4.5) using the existing fields/verbs (contract
still `api_version 1.0.0`); MCP wrapping remains out of scope (roadmap v0.3).
