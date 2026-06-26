# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.2.0] - 2026-06-25
### Added
- **RRULE rolling recurrence** (architecture §2.4): on fire, a `recurrence` item rolls to its next
  future occurrence and re-arms instead of being permanently notified — long-overdue items catch up
  once, then re-arm. Minimal stdlib RFC5545 subset (`FREQ`/`INTERVAL`/`UNTIL`, `exdate` skip); the
  infinite series is never materialised. (E14)
- **Per-alarm lead times** (architecture §4.5): `alarms[]` entries (`{"lead":secs}` or iCal
  `{"trigger":"-PT15M"}`) make an item fire *before* `due_at`; effective lead = max(global `--lead`,
  alarm leads). Applied by both `due` and `tick`. (E15)
- New additive `add` flags: `--recurrence`, `--rdate`, `--exdate`, `--alarms` (item field set and
  verb set unchanged → `api_version` stays 1.0.0).
### Fixed
- **Concurrent-tick double-fire (MEDIUM)**: the `tick` claim is now exclusive on `claimed_at` (only
  an unclaimed-or-stale row is grabbed) and the candidate SELECT skips freshly-claimed rows, so two
  overlapping ticks (manual vs the PT5M heartbeat, or a tick running > 5 min) never both push the
  same reminder. Stale claims (`> _CLAIM_TTL`, left by a crashed tick) are reclaimed.
- **progress invariant**: `add`/`update`/`transition` now reject out-of-range progress with
  `ERR_BAD_PROGRESS` (previously `update --set progress=99999` was accepted).
- **Internal error redaction**: the `ERR_INTERNAL` fallback no longer echoes `str(e)` (which could
  embed the db path) — only the exception type name.
- Docs: dangling `ARCHITECTURE.md` section refs repointed to in-repo `docs/design-brief.md`;
  removed the misleading `--json` flag mention (JSON is always emitted); `.sie/` sandbox added to
  `.gitignore`; 9th plugin keyword added for the base-9 GitHub topics target.
### Tests
- Acceptance suite grows 35 → 41: E14 rolling (+ UNTIL stop), E15 alarm lead (+ iCal trigger),
  exclusive-claim concurrency guard, progress-bounds guard. Each verified red on the prior code.

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
