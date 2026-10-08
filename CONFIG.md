# Schedule Reminder configuration

The registry is native settings, separate from the SQLite task store. The explicit
[configuration contract](config.contract.json) classifies this repository as a skill with
settings; a storage-only check cannot establish configured readiness.

## Discovery and switching

`AGENT_CENTER_CONFIG` selects the registry **file**. If it is set, registry consumers use that
file even when `SCHEDULE_REMINDER_CONFIG` selects another companion for state and work records.
Keep the two selections consistent unless separate locations are deliberate and reviewed.

For the companion root, the order is `SCHEDULE_REMINDER_CONFIG`, then `AGENT_CENTER_CONFIG`
(its parent when it names a JSON file), then the pinned Guards convention:
`SCHEDULE_REMINDER_CONFIG_DIR`, sibling `schedule-reminder-config`,
`~/.schedule-reminder-config`, and `~/.schedule-reminder-data`. Explicit CONFIG and
AGENT_CENTER selections remain selected when missing. A DATA override is excluded from
registry discovery. `SCHEDULE_REMINDER_DATA_DIR` selects runtime DATA directly; otherwise DATA
is `<companion>/data`. `SCHEDULE_DB_PATH` overrides the default `<data>/db.sqlite3`, and
`reminder.py --db` overrides that variable. These independent overrides do not migrate data.

Switch by setting the root and, when used, its registry file together. Stop the relevant writers
before moving state. Run the configuration doctor again, then inspect capability health. Do not
copy old cursors or a stale database over newer work. All writes require PRIVATE versioned
storage. The native registry initializer also enforces its exact artifact declaration in
[storage.contract.json](storage.contract.json). Existing runtime write helpers check PRIVATE
version eligibility; they do not yet enforce artifact ownership for every output. Undeclared
runtime paths remain an audit failure, and universal pre-write ownership enforcement is unverified.

## Registry schema

The root is a JSON object. Existing optional and unknown fields are preserved. `schema_version`
may be absent for existing registries; if present it must be `1`.

| Field | Requirement and consumer |
| --- | --- |
| `streams` | Nonempty object keyed by nonempty stream names; used by relay and inbound discovery |
| `streams.<name>.webhook` | HTTPS URL for webhook delivery, or use the bot channel fields below |
| `streams.<name>.channel_id` | Decimal string; bot delivery also requires `reader.bot_token` |
| `streams.<name>.inbound`, `.listen` | Optional booleans controlling inbound discovery and handling |
| `reader.bot_token` | Nonempty string for bot delivery and ingest/work |
| `guild_id` | Decimal string required for ingest/work |
| `big_brother.user_id` | Decimal string identifying the owner, required before ingest/work can poll inbound messages or reactions |
| `commands`, other `big_brother` and policy fields | Optional existing interfaces in [Agent Center](skills/schedule-reminder/reference/agent-center.md) |

Credential values belong only in the PRIVATE companion. The initializer creates blank values;
it does not fabricate destinations, call Discord, or declare an empty registry ready. The doctor
checks the selected capability requirements locally and never prints credential values. A valid
URL or token-shaped string does not prove that credentials work or delivery occurred.
Save the registry as UTF-8 without a byte-order mark, matching the relay and inbound readers.
The doctor rejects a BOM and leaves the file unchanged.

## Initialize and inspect

Create or clone the PRIVATE companion with a committed HEAD and current Guards visibility proof
first. Then, from the source root:

```powershell
$env:SCHEDULE_REMINDER_CONFIG = '<private-companion>'
$env:AGENT_CENTER_CONFIG = Join-Path $env:SCHEDULE_REMINDER_CONFIG 'registry.json'
python tools/init_config.py --out $env:SCHEDULE_REMINDER_CONFIG
# Edit registry.json with the destinations selected for this installation.
python tools/verify_config.py --json
```

Initialization creates only `registry.json`, never overwrites an existing file and never creates
tasks, a database, readiness receipts or live messages. Repeating it preserves credentials and
unknown fields byte for byte. `--out` affects this invocation only. The doctor's `ready` field
has `scope: configuration-only`; it leaves `runtime_ready` unknown. Use
`--capabilities ingest,work` to check the additional registry requirements.

Database installation remains `reminder.py init`. It is the explicit additive schema-upgrade
operation; back up an existing database first. `reminder.py health` reports measured capability
readiness without migrating the database. Scheduled task registration and recent delivery
receipts have their own requirements in [deployment](skills/schedule-reminder/reference/deployment.md).

## Recovery and retention

Restore configuration from the selected PRIVATE revision and verify credential usability
separately. The companion README records the installation's chosen credential backup arrangement
and steps. Credentials may be versioned in that PRIVATE repository; do not infer that a pointer
alone backs up the credential. SQLite needs an online backup or cold, checkpointed snapshot.
Retain pending deliveries, cursors, IDs and receipt history. Follow
[storage](docs/storage.md) and the [source contract](storage.contract.json); exceeding a review
budget does not permit truncation or deletion.
