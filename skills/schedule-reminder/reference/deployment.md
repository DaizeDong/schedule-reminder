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
all publication routes and current GitHub visibility. Within one process a successful proof is
reused for at most 60 s while the companion root, its Git configuration and HEAD, the global and
system Git configuration, the SSH client configuration, the visibility receipt and the environment
are unchanged; ignore status is still checked per destination and a refusal is never reused.
Configuration reached only through include directives, visibility changed on GitHub and the
receipt ageing past its limit are noticed when the 60 s run out. The reuse is off under
`GIT_CEILING_DIRECTORIES` and below a directory Git would take for a bare repository. Linked worktrees are supported. A nested PUBLIC
repository, unknown visibility, Git metadata, unmanaged storage, or public source destination is
refused before output creation. Permission denial is reported separately from policy refusal.

Initialize or additively upgrade the chosen database with `reminder.py init`; reads and action
entrypoints do not run migrations. `PRAGMA user_version` follows `store.SCHEMA_USER_VERSION`.
Back up an existing database before an explicit upgrade. List reads may return empty for absent
storage; work-feed and health report unavailable, while creation preflight requires initialized
storage. DATABASE, cursors, inboxes, work records, action and notification receipts, digest records
and readiness evidence belong in the PRIVATE companion and remain under version control.
Create database backups with SQLite's backup API or a cold checkpointed snapshot inside PRIVATE
storage; do not copy a live WAL database as an ordinary single file.

Install the complete owner scripts together, including creation, action, feed, linkage,
notification-receipt and artifact modules. `SCHEDULE_ACTION_WORKSPACE` must name an existing
absolute directory inside the admitted PRIVATE data directory; each agent action creates its
owner-issued work UUID directory there. Prepared command notification files temporarily use
`notification-payloads` beside the admitted database. Neither location falls back into source.
The `.console.json` declarations also include digest; that declaration does not add a fourth
scheduled task to the capability installer.

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

Business-event notification receipts are a separate contract from `readiness.json`. They bind
the event, producer, target and payload and retain uncertain sends for reconciliation. A replayed
business receipt alone does not refresh worker readiness. See [notification receipts](notification-receipts.md).

## Inbound work and model policy

Ingest excludes `inbound:false` and `listen:false` channels both from registry enumeration and guild
discovery. Each inbound message retains its ID through dispatch. Work order insertion is atomic
on a stream-and-message key; repeated delivery preserves state and creates one durable run record.
All model calls use installed `llmcall.call`, with agent mode for execution and judge mode for
decisions. Model, routing, timeouts, fallback and runner policy remain owned by llmcall.
Missing llmcall is unavailable; the worker does not install a provider ladder.

Work additionally requires llmcall process containment and truthful execution/cleanup receipts.
Missing cleanup confirmation keeps the writer reservation; a stop whose tree kill is confirmed by a
fresh process snapshot releases it itself. Completion review requires actor and
reviewer model-family identities plus the original workspace baseline and actual change evidence.
Offline synthetic tests exercise those checks but do not establish support in an installed
llmcall version. `agent_task.py status` observes reservations; `recover-cleanup` accepts an explicit,
generation-bound operator review, not a retry of the work. See [Agent Center](agent-center.md).

Configuration setup and the local registry doctor are specified in [CONFIG.md](../../../CONFIG.md).
They do not initialize or migrate the database or register tasks. The shipped installer targets
Windows Task Scheduler; no maintained Unix cron installer is provided.
