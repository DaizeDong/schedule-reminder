# schedule-reminder

Track todos, events and progress in a crash-safe SQLite store; fire due reminders via Discord through one stable CLI/JSON API.

[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-orange?style=flat)](https://docs.anthropic.com/en/docs/claude-code)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

schedule-reminder is a base other skills build on: they write reminders into it and read task
progress out of it through the versioned CLI/JSON surface (`api_version`), never the database. It
also carries the Agent Center bus: `relay.py` is the single Discord egress, and the inbound half
turns owner replies in Discord into pool changes or agent work orders.

Use it to track a todo / event / deadline / progress / reminder / memo (任务 / 提醒 / 进度 / 备忘), or
when another skill needs to write a reminder or read task progress. For a one-shot message with
nothing to persist, call the relay directly.

## Install

```bash
git clone https://github.com/DaizeDong/schedule-reminder.git ~/.claude/plugins/schedule-reminder
python -m pip install -e "$(git config --get fleet.guards)"   # companion resolver
```

Create a PRIVATE versioned companion repository for the runtime data, then:

```powershell
$env:SCHEDULE_REMINDER_CONFIG = '<private-companion>'
python tools/init_config.py --out $env:SCHEDULE_REMINDER_CONFIG   # creates registry.json, never overwrites
# fill registry.json
python tools/verify_config.py --json                             # local shape check only
python skills/schedule-reminder/scripts/reminder.py init         # create or upgrade the database
```

The 5-minute task ships with the skill: [automation/tasks.json](automation/tasks.json) declares
`ScheduleReminderTick`, which runs `skills/schedule-reminder/scripts/tick.py`. A task registration
tool that reads this manifest installs it (on the author's machines, background-task-core's
`install-skill`); this repository ships no installer of its own. Machine choices (enabled state,
another interval, a console category) go in the companion's `automation/tasks.local.json`. One tick
runs the agent work step, fires due reminders, then polls the inbound channels.

## Configuration

`SCHEDULE_REMINDER_CONFIG` selects the companion root; without it, commands that need the companion
fail as uninitialized instead of guessing a path. `AGENT_CENTER_CONFIG` optionally selects a
different registry **file**. DATA is `<companion>/data` unless `SCHEDULE_REMINDER_DATA_DIR` names
another PRIVATE directory; `SCHEDULE_DB_PATH` overrides `<data>/db.sqlite3`, and `reminder.py --db`
overrides that. Writes into the public source tree or Git metadata are refused.

The registry (`registry.json`, UTF-8 without BOM) is a JSON object; unknown fields are preserved.

| Field | Meaning |
| --- | --- |
| `streams.<name>.webhook` | HTTPS webhook for that stream, or use `channel_id` with the bot |
| `streams.<name>.channel_id` | Decimal channel id for bot delivery |
| `streams.<name>.inbound`, `.listen` | Optional booleans controlling inbound polling |
| `reader.bot_token_ref` | `cv://<record>/<credential>` reference resolved through credvault (or `reader.bot_token` inline, never both) |
| `guild_id` | Decimal guild id, required for inbound |
| `big_brother.user_id` | The owner's user id: the only author whose replies count, and the target of `relay.py send --dm` |
| `commands` | Deterministic inbound command handlers |

`AGENT_CENTER_BOT_TOKEN` in the environment overrides both token fields. Credential values live only
in the PRIVATE companion; the doctor never prints them and never opens the vault.

The companion does not commit the SQLite file. `reminder.py export-text --out <dir>` writes one sorted
`<table>.jsonl` per table, including meta and receipts; commit those, and rebuild a missing database with
`reminder.py import-text --from <dir>`.

## Quick start

```bash
cd skills/schedule-reminder/scripts
python reminder.py add --title "Reply to recruiter" --due-at 2026-06-28T17:00:00Z --priority 1 \
       --source me --idempotency-key me:1
python reminder.py list --active
python reminder.py creation-preflight --title "Prepare Acme report" --source my-skill --idempotency-key my-skill:report-1
python reminder.py transition --id <ID> --to doing --progress 30
python reminder.py done --id <ID>
python tick.py --only remind                                   # what the 5-minute task runs, one step
python relay.py send --dm --text "hello"                       # owner DM through the bot
```

Output is always JSON. Global options such as `--db` and `--actor` go before the verb. See
[examples.json](examples.json) for synthetic output reproduced by `tools/make_fixtures.py`.

## Architecture

```
SQLite (WAL) single file        <- private storage, downstream never touches it
  store.py (typed functions)    <- in-process; trusted skills may import
    reminder.py <verb>          <- versioned CLI contract (api_version 1.0.0)
[task: every 5 min] tick.py -> work   agent_tick  (reap / launch one agent order)
                             -> remind store.tick  (due reminders -> relay.py -> Discord)
                             -> ingest ingest_tick (owner replies -> command handler or LLM judge)
```

The task is only a heartbeat: `tick` reconciles the durable table, so a machine that slept catches
up every missed reminder on the next run (idempotent, at-least-once with dedupe). The full contract,
integration examples, the Agent Center bus, notification receipts and recovery rules are in
[reference.md](skills/schedule-reminder/reference/reference.md).

## Limitations

- SQLite 3.51.3 or later is recommended (earlier versions have a WAL-reset corruption bug); `health` warns.
- Recurrence uses rolling expansion of a supported RRULE subset.
- Keep the database on a local disk, never a synced or network folder.
- Agent work needs llmcall's process containment and cleanup evidence; without it a run stays unresolved.

## License

[MIT](LICENSE)
