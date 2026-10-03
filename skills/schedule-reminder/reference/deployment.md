# Capability installation and readiness

`scripts/install.ps1 -Capabilities store,remind -Plan` prints a JSON plan without effects.
The four groups are `store`, `remind`, `ingest`, and `work`. Omitted selection chooses
`store,remind`; an explicit empty string selects nothing and is a no-op. Unknown groups fail
before files, tasks, or links are created. Remove `-Plan` to install the selection.

| Group | Task | Unbounded interval | Dependencies |
|---|---|---|---|
| store | none | none | PRIVATE initialized store |
| remind | ScheduleReminderTick | PT5M | store, relay |
| ingest | AgentCenterIngestTick | PT10M | store, relay, llmcall |
| work | AgentCenterWorkTick | PT2M | store, relay, llmcall |

All scheduled actions run `scheduler_worker.py` with explicit capability, database and registry
arguments. Installation checks the selected interpreter and imports, creates only selected tasks,
and separately reads back executable, arguments, working directory, enabled task and trigger,
interval, and absence of an end boundary or repetition duration. Matching tasks are reused.
Successful registration does not imply readiness. Installation exits nonzero until all selected
capabilities have measured readiness. `-NoTask` with omitted selection installs store only;
`-NoJunction` skips the optional skill junction.

## Private runtime storage

Set `SCHEDULE_REMINDER_CONFIG` to an initialized PRIVATE versioned companion root. Its `data`
directory is the default DATA home; `SCHEDULE_REMINDER_DATA_DIR` may select another PRIVATE
location. `AGENT_CENTER_CONFIG` selects the registry file. Explicit `reminder.py --db` overrides
`SCHEDULE_DB_PATH`; every write still proves the nearest governing Git repository PRIVATE using
its origin and current GitHub visibility. Linked worktrees are supported. A nested PUBLIC
repository, unknown visibility, Git metadata, unmanaged storage, or public source destination is
refused before output creation. Permission denial is reported separately from policy refusal.

Initialize the chosen database with `reminder.py init`. Reads of absent storage return empty
results, and health reports unavailable storage without initializing it. DATABASE, cursors, inboxes,
work records, digest records and readiness evidence belong in the PRIVATE companion and remain
under version control. Keep credentials out of configuration backups that exclude them.
Create database backups with SQLite's backup API or a cold checkpointed snapshot inside PRIVATE
storage; do not copy a live WAL database as an ordinary single file.

## Health and delivery evidence

`reminder.py health` exits zero when it produces its `api_version`, `schema_version`, `ok:true`,
`health` envelope. Read `health.readiness.ready` for the selected capability verdict. Selection
comes from `SCHEDULE_CAPABILITIES` or `health --capabilities` with the same default and empty rules.
Each group remains present with selected, status, and reasons. Store, relay and llmcall dependencies
are reported separately. Health sends nothing and writes no runtime state. An active WAL prevents
the immutable store probe from measuring readiness; retry after the writer closes.

After successful reminder delivery, the scheduler worker writes `readiness.json` next to the
PRIVATE database. The configured stream's normal relay path requests and validates Discord
message IDs. A legacy custom relay can still return a boolean, but this provides no readiness
evidence. A receipt-aware `SCHEDULE_RELAY_CMD` returns a JSON object with nonempty `kind` and
`receipt_id`, `delivered:true`, and integer `exit_code:0`. A nonzero process or worker exception
cannot publish success. Empty work ticks do not refresh a prior delivery receipt.

Evidence uses schema version 1 and a tasks object keyed by capability. Each task and its dispatch
repeat task name, interpreter, action arguments, working directory, interval seconds, adapter
SHA256 and config SHA256. The record has RFC3339 `last_success`; dispatch has RFC3339
`completed_at` plus the receipt fields. Both timestamps must be at most three task intervals old
and at most sixty seconds in the future, inclusive. Health recomputes identity and still requires
actual task readback. `SCHEDULE_READINESS_EVIDENCE` optionally overrides the read location only;
workers always write the normal PRIVATE evidence path. Users do not need to manufacture evidence.

`synthetic-local` evidence can verify the local receipt path but leaves external readiness false.
Ingest and work run their adapters but currently return explicitly unmeasured because their own
confirmed-delivery evidence is not implemented. Selecting either keeps overall readiness false.
They cannot borrow a reminder receipt.

## Inbound work and model policy

Ingest excludes `inbound:false` and `listen:false` channels both from registry enumeration and guild
discovery. Each inbound message retains its ID through dispatch. Work order insertion is atomic
on a stream-and-message key; repeated delivery preserves state and creates one durable run record.
All model calls use installed `llmcall.call`, with agent mode for execution and judge mode for
decisions. Model, routing, timeouts, fallback and runner policy remain owned by llmcall.
Missing llmcall is unavailable; the worker does not install a provider ladder.
