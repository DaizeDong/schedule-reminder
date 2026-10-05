# schedule-reminder

Track todos, events and progress in a crash-safe SQLite store; fire due reminders via Discord through one stable CLI/JSON API.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](README_CN.md)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.6.0-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Design Philosophy

schedule-reminder is a **T0 infrastructure base**: other skills write reminders into it and read task
progress out of it. Downstream integrations use the versioned CLI/JSON surface (with an
`api_version`) and the documented read-only Task Console linkage seam, never database internals.
v0.1 focused on concurrency-safe + crash-safe persistence, a
guarded state machine, idempotent writes, at-least-once delivery, and MUST-PRESERVE unknown fields,
not on flashy features.

A stable contract requires stricter input and state checks, at the cost of refusing
ambiguous writes. Due reminders retain at-least-once retry behavior. Identified business-event
notifications use payload-bound receipts and preserve uncertain sends for reconciliation;
uncertainty does not authorize an automatic resend. Task registration, health reports and source
inspection do not alone prove delivery.

📜 **[Read the full design philosophy -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## What it is (and isn't)

**Is:** a persistent, queryable schedule + memo store with a `pending/doing/done/blocked/cancelled`
state machine, due-reminder dispatch via the local Discord relay, and a stable
`reminder.py [--actor NAME] <verb>` API for both humans and other skills.

**Isn't:** a one-shot notifier (that's the relay), a calendar UI, or a cloud service. If nothing
needs to *persist, be queried, or be reminded*, you don't need this.

## Install

```
/plugin install github:DaizeDong/schedule-reminder
```

Or clone manually:

```bash
git clone --recurse-submodules https://github.com/DaizeDong/schedule-reminder.git ~/.claude/plugins/schedule-reminder
```

Set `SCHEDULE_REMINDER_CONFIG` to an initialized PRIVATE versioned companion repository before
writing runtime data. Preview the selected capabilities, then run the idempotent installer:

```powershell
pwsh -File skills/schedule-reminder/scripts/install.ps1 -Capabilities store,remind -Plan
pwsh -File skills/schedule-reminder/scripts/install.ps1 -Capabilities store,remind
```

The default selection is store plus remind. Empty selection is a no-op; ingest and work are
optional. Installation returns nonzero until selected readiness is measured, even after successful
task registration. `health` still returns a successful JSON report when readiness is incomplete;
inspect `health.readiness.ready`. See [deployment](skills/schedule-reminder/reference/deployment.md)
for PRIVATE storage, task readback, worker receipts, and the unmeasured ingest/work boundary.

## Quick start

```bash
cd skills/schedule-reminder/scripts
python reminder.py init
python reminder.py add --title "Reply to recruiter" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source me --idempotency-key me:1
python reminder.py list --active
python reminder.py creation-preflight --title "Prepare Acme report" --source my-skill --idempotency-key my-skill:report-1
python reminder.py transition --id <ID> --to doing --progress 30
python reminder.py done --id <ID>
python reminder.py tick --now 2026-06-28T17:00:00Z   # the scheduler runs this every 5 min
```

## How to invoke

The skill fires when the user wants to track a todo / event / deadline / progress / reminder / memo
(任务 / 提醒 / 进度 / 备忘), or when another skill needs to write a reminder or read task progress.

## Example output

See [examples.json](examples.json) for synthetic output reproduced by tools/make_fixtures.py.

## Architecture (three layers)

```
SQLite (WAL) single file        <- private storage, downstream NEVER touches it
  store.py (typed functions)    <- in-process; trusted skills may import
    reminder.py [--actor NAME] <verb>   <- versioned CLI contract (api_version 1.0.0)
    reminder_linked_items.py           <- read-only, reviewed Task Console linkage
[Windows task: PT5M heartbeat] -> reminder.py tick -> reconcile due -> Discord relay (out)
[Windows task: PT10M ingest]   -> ingest_tick -> poll every readable channel -> a registered
                                  command handler, else dispatch (LLM judge)              (in)
```

The OS task is only a heartbeat. `tick` reconciles the durable table, so a slept/off machine catches
up **all** missed reminders on the next run (idempotent, at-least-once + dedupe), no per-event OS
triggers, no silent skips.

- **Contract:** [`skills/schedule-reminder/reference/contract.md`](skills/schedule-reminder/reference/contract.md)
- **Deployment:** [`skills/schedule-reminder/reference/deployment.md`](skills/schedule-reminder/reference/deployment.md)
- **Integration (for downstream skills):** [`skills/schedule-reminder/reference/integration.md`](skills/schedule-reminder/reference/integration.md)
- **Delivery and work recovery:** [operations.md](skills/schedule-reminder/reference/operations.md).
- **Creation and follow-ups:** [integration.md](skills/schedule-reminder/reference/integration.md) and [dispatch identity](docs/dispatch-identity.md).
- **Work feed and completion:** [manual completion](docs/manual-completion.md) and [reviewed task linkage](skills/schedule-reminder/reference/linkage-review.md).
- **Identified notifications:** [business-event receipts](skills/schedule-reminder/reference/notification-receipts.md).
- **Source layout and storage retention:** [storage.md](docs/storage.md) and [storage.contract.json](storage.contract.json).

## Test coverage and evidence

15 acceptance signals (E1-E15) drive the frozen CLI via subprocess and assert JSON: CRUD, the full
transition table (legal + illegal), write invariants, due trigger / idempotent tick / missed-fire
catch-up / retry back-off, concurrent writes with `PRAGMA integrity_check`, concurrent read/write,
idempotent dedupe, API golden, unknown-field preservation, health, RRULE rolling recurrence, and
per-alarm lead times. Plus hermetic module tests for the Agent Center bus, relay egress, daily
digest, heartbeat survival, notify routing, and the two-way ingest/dispatch. E8/E9/E11/E12 are
merge-blocking red lines.

```bash
python -B -m pytest skills/schedule-reminder/tests/ -q -p no:cacheprovider
```

The existing notification-route checks also need the canonical `notification_language.py` code.
Set `SCHEDULE_TEST_LANGUAGE_RULE` to that file when running them. The test harness copies the code
unchanged into its synthetic profile; it does not load the operator's configuration or credentials.
Without the dependency, those checks report its absence. Live scheduled-task checks remain separate
from offline capability tests.

Creation, action-receipt and linkage tests cover synthetic retries and concurrent changes.
Set `SCHEDULE_TEST_TASK_CONSOLE_ROOT` to a canonical Task Console checkout to also exercise its
compiler and work API against the real reminder CLI. Those consumer checks explicitly skip when
the checkout is not provided; no Scheduler task is run by them.

## Limitations

- **SQLite ≥ 3.51.3 recommended.** Earlier versions carry a WAL-reset multi-writer corruption bug;
  `health` warns (does not hard-fail). Upgrade path without changing Python: `pip install
  pysqlite3-binary` (auto-detected). The bundled test suite verifies `integrity_check` stays `ok`
  under concurrency on the host SQLite.
- **Recurrence uses rolling expansion.** The supported RRULE subset advances the master row to the next future occurrence; it does not materialize an infinite series. See the contract for supported fields.
- **Windows-first deployment** (scheduled task via `install.ps1`); cron line provided for Unix.
- **DB must stay on local NTFS**, never a OneDrive/GDrive/network path (WAL lock + sync corruption).
- **Work completion needs llmcall execution evidence.** Missing process containment, cleanup
  confirmation, model-family identity or review evidence leaves the run unresolved. Offline
  controls do not establish that an installed llmcall version provides those capabilities.

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).

JSON output is unconditional. Place global options such as --db and --actor before the verb; do not pass --json.

## Synthetic verification

`verify_expired_email_active.py --data-dir <PRIVATE-directory>` generates disposable synthetic records in the private companion. It refuses a source-contained database. The utility does not prove live worker readiness.

Historical counts do not establish current runtime or installed readiness; use the execution report for the exact source revision.
