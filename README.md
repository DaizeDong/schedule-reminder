# schedule-reminder

Track todos, events and progress in a crash-safe SQLite store; fire due reminders via Discord through one stable CLI/JSON API.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)
[![Roadmap](https://img.shields.io/badge/Roadmap-v0.4.2-purple?style=flat)](ROADMAP.md)

[English](README.md) | [中文版](README_CN.md)

---

## ⭐ Read this first, the design philosophy

schedule-reminder is a **T0 infrastructure base**: other skills write reminders into it and read task
progress out of it. So its single governing principle is **"a base is the contract, not the
storage"**, downstream depends on a frozen CLI/JSON surface (with an `api_version`), never on the
database, so the engine can be rewritten forever without breaking anyone. v0.1 spends its whole
budget on the guarantees a base must never break: concurrency-safe + crash-safe persistence, a
guarded state machine, idempotent writes, at-least-once delivery, and MUST-PRESERVE unknown fields ,
not on flashy features.

📜 **[Read the full design philosophy -> PHILOSOPHY.md](PHILOSOPHY.md)**

---

## What it is (and isn't)

**Is:** a persistent, queryable schedule + memo store with a `pending/doing/done/blocked/cancelled`
state machine, due-reminder dispatch via the local Discord relay, and a stable
`reminder.py <verb> --json` API for both humans and other skills.

**Isn't:** a one-shot notifier (that's the relay), a calendar UI, or a cloud service. If nothing
needs to *persist, be queried, or be reminded*, you don't need this.

## Install

```
/plugin install github:DaizeDong/schedule-reminder
```

Or clone manually:

```bash
git clone https://github.com/DaizeDong/schedule-reminder.git ~/.claude/plugins/schedule-reminder
```

Then run the idempotent installer (creates the DB, registers the PT5M heartbeat task, junctions the
skill, runs health):

```powershell
pwsh -File skills/schedule-reminder/scripts/install.ps1
```

## Quick start

```bash
cd skills/schedule-reminder/scripts
python reminder.py init
python reminder.py add --title "Reply to recruiter" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source me --idempotency-key me:1
python reminder.py list --active
python reminder.py transition --id <ID> --to doing --progress 30
python reminder.py done --id <ID>
python reminder.py tick --now 2026-06-28T17:00:00Z   # the scheduler runs this every 5 min
```

## How to invoke

The skill fires when the user wants to track a todo / event / deadline / progress / reminder / memo
(任务 / 提醒 / 进度 / 备忘), or when another skill needs to write a reminder or read task progress.

## Example output

```json
{"api_version":"1.0.0","schema_version":1,"ok":true,
 "item":{"id":"019f0035-f0e1-700d-95f8-020c880c543a","kind":"task","title":"Reply to recruiter",
         "state":"pending","progress":0,"priority":1,"due_at":"2026-06-28T17:00:00.000000+00:00",
         "source":"me","idempotency_key":"me:1","ext":null,"...":"..."}}
```

## Architecture (three layers)

```
SQLite (WAL) single file        <- private storage, downstream NEVER touches it
  store.py (typed functions)    <- in-process; trusted skills may import
    reminder.py <verb> --json   <- the ONLY stable contract (api_version 1.0.0)
[Windows task: PT5M heartbeat] -> reminder.py tick -> reconcile due -> Discord relay (out)
[Windows task: PT10M ingest]   -> ingest_tick -> poll channels for user replies -> dispatch (in)
```

The OS task is only a heartbeat. `tick` reconciles the durable table, so a slept/off machine catches
up **all** missed reminders on the next run (idempotent, at-least-once + dedupe), no per-event OS
triggers, no silent skips.

- **Contract:** [`skills/schedule-reminder/reference/contract.md`](skills/schedule-reminder/reference/contract.md)
- **Deployment:** [`skills/schedule-reminder/reference/deployment.md`](skills/schedule-reminder/reference/deployment.md)
- **Integration (for downstream skills):** [`skills/schedule-reminder/reference/integration.md`](skills/schedule-reminder/reference/integration.md)
- **Agent Center bus (two-way):** [`skills/schedule-reminder/reference/agent-center.md`](skills/schedule-reminder/reference/agent-center.md), the outbound relay + daily digest and the inbound reply ingest every skill shares.

## Tested-real

15 acceptance signals (E1-E15) drive the frozen CLI via subprocess and assert JSON: CRUD, the full
transition table (legal + illegal), write invariants, due trigger / idempotent tick / missed-fire
catch-up / retry back-off, concurrent writes with `PRAGMA integrity_check`, concurrent read/write,
idempotent dedupe, API golden, unknown-field preservation, health, RRULE rolling recurrence, and
per-alarm lead times. Plus hermetic module tests for the Agent Center bus, relay egress, daily
digest, heartbeat survival, notify routing, and the two-way ingest/dispatch. E8/E9/E11/E12 are
merge-blocking red lines.

```bash
python -m pytest skills/schedule-reminder/tests/ -q   # 93 passed
```

## Limitations

- **SQLite ≥ 3.51.3 recommended.** Earlier versions carry a WAL-reset multi-writer corruption bug;
  `health` warns (does not hard-fail). Upgrade path without changing Python: `pip install
  pysqlite3-binary` (auto-detected). The bundled test suite verifies `integrity_check` stays `ok`
  under concurrency on the host SQLite.
- **Recurrence/RRULE expansion is stored but not yet expanded** (roadmap v0.2); the `recurrence`
  field round-trips today.
- **Windows-first deployment** (scheduled task via `install.ps1`); cron line provided for Unix.
- **DB must stay on local NTFS**, never a OneDrive/GDrive/network path (WAL lock + sync corruption).

## Languages

English (`README.md`, authoritative) · 中文 (`README_CN.md`)

## Roadmap · Contributing · License

See [ROADMAP.md](ROADMAP.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [LICENSE](LICENSE) (MIT).
