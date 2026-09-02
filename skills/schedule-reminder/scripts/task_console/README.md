# task-console

A local, operable view of this machine's Windows scheduled tasks. Grouped into categories you
define, one screen, and you can enable, disable, run or stop a task from the page.

It exists because a report that only tells you a task is dead makes you go and find the name
yourself. This lets you act on what you just read.

## Run it

```
python server.py                 # opens http://127.0.0.1:8787/ in your browser
python server.py --port 9000     # different port
python server.py --no-browser    # start the server, open it yourself
```

Windows only. It reads the Windows Task Scheduler; there is nothing to read anywhere else.

## Configure it

Copy `categories.example.json` to `~/.task-console/categories.json` and replace the synthetic
names with your own tasks. Everything else is optional.

| Variable | What it points at | If unset |
|---|---|---|
| `TASK_CONSOLE_CATEGORIES` | your category map | `~/.task-console/categories.json`; if that is missing, every task lands in one group called uncategorized, and the page says so |
| `TASK_CONSOLE_HEALTH` | a health watch list (`task-health.json` shape) | no default; unset means the health-coverage column reads NOT CHECKED |
| `TASK_CONSOLE_ALLOWLIST` | a PowerShell file containing a `$TaskNames = @(...)` backup allow-list | no default; unset means the backup-coverage check reports NOT CHECKED rather than passing |

The tool defaults only into its own namespace. Pointing it at whatever else a machine keeps its
watch-list and allow-list in is the launcher's job, and the launcher belongs on that machine.

That last row is the important one. **Unknown and pass are never rendered the same.** A console
that showed a green backup-coverage column because it had nothing to compare against would be
worse than one that showed nothing at all.

A task that appears in no category still shows up, in a group called uncategorized. It is never
dropped: the task nobody categorised is the one nobody is watching.

## What it will not do

It cannot create, delete or reconfigure a task. Creating one correctly means naming it so the
backup drift gate can see it, choosing its settings deliberately, generating its launcher, and
registering it in three places. A button that skipped those steps would manufacture exactly the
untracked task that procedure exists to prevent.

## Why it is locked down

It can change system state, so three controls, none of them decorative:

1. **Binds 127.0.0.1 only.** Nothing off this machine can reach it.
2. **Every `/api/` call needs a token** minted fresh at startup and never written to disk. Without
   it, any web page you had open could POST to `http://127.0.0.1:<port>/api/act` and disable your
   backup task. Being on localhost does not prevent that; a token does.
3. **The verb list is closed** (`enable` / `disable` / `run` / `stop`), the task must be at the
   root path, and the server re-enumerates the live task list and checks membership before acting
   rather than trusting the name it received. The name reaches PowerShell through an environment
   variable, never interpolated into a command string.

Some tasks are owned by SYSTEM or registered at `RunLevel=Highest` and cannot be touched from a
normal user session. The console says so instead of reporting a bare access-denied.

## Files

| File | Role |
|---|---|
| `server.py` | the server, the merge logic, and the three config resolutions |
| `collect.ps1` | dumps live scheduler state as JSON and makes no judgement |
| `act.ps1` | performs one whitelisted verb and reports the before/after state |
| `console.html` | the page |
| `categories.example.json` | a template, synthetic names only |

`act.ps1` reports the state transition rather than the absence of an exception, because
`Enable-ScheduledTask` on an already-enabled task raises nothing and changes nothing, and
"no error" would render as success.

## Data boundary

This directory ships in a public repo and holds no real state. No snapshot is cached to disk, the
category map is read from a path outside the repo, and `categories.example.json` contains only
synthetic names. Real task names are real-run data and belong in the private machine config.
