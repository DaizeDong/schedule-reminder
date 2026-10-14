# Capability installation and health

`scripts/install.ps1 -Capabilities store,remind -Plan` prints a JSON plan without effects.
The four groups are `store`, `remind`, `ingest`, and `work`. Omitted selection chooses
`store,remind`; an explicit empty string selects nothing and is a no-op. Unknown groups fail
before files, tasks, or links are created. Remove `-Plan` to install the selection.

| Group | Task | Unbounded interval | Dependencies |
|---|---|---|---|
| store | none | none | PRIVATE initialized store |
| remind | ScheduleReminderTick | PT5M | store, relay |
| ingest | AgentCenterIngestTick | PT5M | store, relay, llmcall |
| work | AgentCenterWorkTick | PT2M | store, relay, llmcall |

Each scheduled action runs its real entrypoint under `pythonw.exe -B`: `reminder.py --db <database> tick`,
`ingest_tick.py` or `agent_tick.py`. The ingest and work entrypoints read `SCHEDULE_DB_PATH` and
`AGENT_CENTER_CONFIG` from the user environment. Installation creates only selected tasks and
separately reads back executable, arguments, working directory, enabled task and trigger, interval,
and absence of an end boundary or repetition duration. Matching tasks are reused. Installation
exits zero when the database passes its health check and, for a scheduled capability, the relay
registry file exists. `-NoTask` with omitted selection installs store only; `-NoJunction` skips the
optional skill junction.

## Private runtime storage

Set `SCHEDULE_REMINDER_CONFIG` to an initialized PRIVATE versioned companion root. Its `data`
directory is the default DATA home; `SCHEDULE_REMINDER_DATA_DIR` may select another PRIVATE
location. `AGENT_CENTER_CONFIG` selects the registry file. Explicit `reminder.py --db` overrides
`SCHEDULE_DB_PATH`. Writes run no runtime visibility proof: a destination inside the public
source tree, inside Git metadata or reached through a filesystem alias is refused before output
creation, and a write below an uninitialized companion fails instead of creating it. Keeping the
companion PRIVATE is the operator's setup step; publication is guarded at commit time by the
Guards hooks and CI. Permission denial is reported separately from policy refusal.

Initialize or additively upgrade the chosen database with `reminder.py init`; reads and action
entrypoints do not run migrations. `PRAGMA user_version` follows `store.SCHEMA_USER_VERSION`.
Back up an existing database before an explicit upgrade. List reads may return empty for absent
storage; work-feed and health report unavailable, while creation preflight requires initialized
storage. DATABASE, cursors, inbound records, work records, action and notification receipts and digest
records belong in the PRIVATE companion and remain under version control.
Create database backups with SQLite's backup API or a cold checkpointed snapshot inside PRIVATE
storage; do not copy a live WAL database as an ordinary single file.

Install the complete owner scripts together, including creation, action, feed, linkage and
notification-receipt modules. `SCHEDULE_ACTION_WORKSPACE` must name an existing
absolute directory inside the admitted PRIVATE data directory; each agent action creates its
owner-issued work UUID directory there. Prepared command notification files temporarily use
`notification-payloads` beside the admitted database. Neither location falls back into source.
The `.console.json` declarations also include digest; that declaration does not add a fourth
scheduled task to the capability installer.

## Health and delivery

`reminder.py health` exits zero when it produces its `api_version`, `schema_version`, `ok:true`,
`health` envelope. It runs SQLite `quick_check` on an immutable read of the database and reports
`relay_ok` when the relay registry file exists (or `SCHEDULE_RELAY_CMD` is set). It starts no
process, sends nothing and writes no runtime state. An active WAL prevents the immutable store
probe; retry after the writer closes. Task Scheduler's own last run time and last result are the
record of whether the scheduled ticks ran.

Reminder delivery calls the sibling `relay` module in the tick's own process, on
`SCHEDULE_RELAY_STREAM` (default `reminders`). The configured stream's normal relay path requests
and validates Discord message IDs; a stream without a webhook or channel falls back to the Big
Brother DM and returns a plain boolean. `SCHEDULE_RELAY_CMD` replaces the relay with a command that
receives the text as its last argument; a receipt-aware command prints a JSON object with
`delivered:true` and integer `exit_code:0`, and a nonzero exit counts as a failed delivery.

Business-event notification receipts bind the event, producer, target and payload and retain
uncertain sends for reconciliation. See [notification receipts](notification-receipts.md).

## Inbound work and model policy

Ingest excludes `inbound:false` and `listen:false` channels both from registry enumeration and guild
discovery. Each inbound message retains its ID through dispatch. Work order insertion is atomic
on a stream-and-message key; repeated delivery preserves state and creates one durable run record.
All model calls use installed `llmcall.call`, with agent mode for execution and judge mode for
decisions. Model, routing, timeouts, fallback and runner policy remain owned by llmcall.
Missing llmcall is unavailable; the worker does not install a provider ladder.

When the registry names the bot token by `reader.bot_token_ref`, the credvault Python package must
be importable by the exact interpreter the scheduled ticks use (they run under `pythonw.exe`
with `-B`, so check that interpreter, not whichever `python` is first on PATH). Install it for
the user rather than into a shared interpreter, for example
`<that python.exe> -m pip install --user --no-deps -e <credvault>/python`, and confirm with
`<that python.exe> -c "import credvault"`. The vault and its key are found by the library's own
defaults. A tick that cannot resolve the reference logs `BotTokenUnavailable` with the cause; it
does not read any other copy of the token.

Work additionally requires llmcall process containment and truthful execution/cleanup receipts.
Missing cleanup confirmation keeps the writer reservation; a stop whose runner job is terminated and
then reports no live member releases it itself. Completion is decided by the executor's own
verification command, which the runner executes; no second model reviews the work. Offline
synthetic tests exercise those checks but do not establish support in an installed llmcall
version. `agent_task.py status` observes reservations; `recover-cleanup` accepts an explicit,
generation-bound operator review, not a retry of the work. See [Agent Center](agent-center.md).

Configuration setup and the local registry doctor are specified in [CONFIG.md](../../../CONFIG.md).
They do not initialize or migrate the database or register tasks. The shipped installer targets
Windows Task Scheduler; no maintained Unix cron installer is provided.
