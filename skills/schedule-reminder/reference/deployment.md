# schedule-reminder — Deployment (DB init, heartbeat task, junction)

> One idempotent installer: `scripts/install.ps1`. It creates the DB (WAL + schema), registers a
> single Windows scheduled task as a **PT5M heartbeat**, junctions the skill into `~/.claude/skills`,
> and runs `health`. Re-running it is safe.

## What gets created

| Thing | Where | Notes |
|---|---|---|
| DB (3 files) | `the local reminder DB` (+`-wal`, `-shm`) | **local NTFS only** — never OneDrive/GDrive/network (WAL file-lock + sync corruption) |
| Heartbeat task | Scheduled Task `ScheduleReminderTick` | runs `pythonw reminder.py tick` every 5 min |
| Live skill | junction `~/.claude/skills/schedule-reminder` → repo `skills/schedule-reminder` | edits flow both ways |

## Install

```powershell
pwsh -File scripts/install.ps1            # idempotent: safe to re-run
pwsh -File scripts/install.ps1 -NoTask    # DB + junction only (skip scheduler)
```

## The scheduling model (why a single heartbeat, not per-event triggers)

The OS task is **only a heartbeat**. It does not decide "is this due" — `tick.py` (via
`reminder.py tick`) reconciles the local table each run:

1. select items with `due_at <= now`, active, not yet notified, past any retry/wait gate;
2. atomically claim each (`UPDATE ... WHERE notified_at IS NULL`);
3. push **outside the write transaction** via the relay;
4. on success mark `notified_at` + audit; on failure exponential back-off, then `blocked` + alert.

This makes **missed-fire catch-up free**: if the machine slept/was off, the next tick dispatches all
overdue-and-unnotified items at once — reconciliation is idempotent and can catch up *multiple*
missed fires, unlike schtasks `StartWhenAvailable` / cron / anacron (one late catch-up at best).

Task XML knobs (hardened): `StartWhenAvailable=true`, `UseUnifiedSchedulingEngine=true`,
`Repetition Interval=PT5M Duration=P1D`, `MultipleInstancesPolicy=IgnoreNew` (no overlap),
`DisallowStartIfOnBatteries=false`, `StopIfGoingOnBatteries=false`.

## cron (macOS / Linux)

```
*/5 * * * * cd <repo>/skills/schedule-reminder/scripts && python reminder.py tick >/dev/null 2>&1
```

## Notification channel

Default = the local Discord relay. Swap without touching logic:

- `SCHEDULE_RELAY_CMD` — any command; reminder text is appended as the final argv (also the test seam).
- `SCHEDULE_RELAY_SEND` — path to `discord_relay/send.py` (default `the legacy DM notifier script`).

## SQLite version (read this)

Recommended **SQLite ≥ 3.51.3** (earlier versions carry a WAL-reset multi-writer corruption bug
that this exact workload can trigger). `health` reports `sqlite_version` + `sqlite_version_ok` and
**warns** rather than hard-failing, so the skill stays usable on older hosts. To upgrade without
changing Python: `pip install pysqlite3-binary` (auto-detected, preferred when present).

## Backup (do NOT naively copy the .db)

A WAL DB is three files; a live raw copy can be inconsistent. Export a cold snapshot instead:

```bash
sqlite3 the local reminder DB "VACUUM INTO 'backup.sqlite3'"
```

Keep the DB and its `-wal`/`-shm` out of any real-time sync/backup snapshot.

## Secrets

The relay reads its **own** bot token from its **own** config; this skill never reads, logs, or
echoes any token. `.gitignore` covers `db.sqlite3*`, `config.json`, and `*credentials*`. If the
relay token was ever exposed, reset it in the Discord developer portal before relying on the channel.
