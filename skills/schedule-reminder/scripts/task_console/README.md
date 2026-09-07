# task-console

A local, operable maintenance console for one machine. Scheduled tasks, artifact freshness, git
repositories, skills, the memory pool, plugins, and disk, on one screen, with a button on every
row that needs one.

It exists because a report that only tells you something is dead makes you go and find it
yourself. This lets you act on what you just read.

Every panel obeys the same rule: **"nothing wrong" and "never checked" are different outputs.**
A source that cannot be read says so, in its own colour, instead of rendering an empty green
table. The self-check strip at the top exists to make that visible for the page as a whole:
it lists every path the console tried, whether it got there, and how fresh it was.

## Run it

```
python server.py                 # opens http://127.0.0.1:8787/ in your browser
python server.py --port 9000     # different port
python server.py --no-browser    # start the server, open it yourself
```

Windows only. It reads the Windows Task Scheduler; there is nothing to read anywhere else.

## What is on the page

| Panel | Reads | You can |
|---|---|---|
| Self-check | every configured path | see which sources are `ok` / `stale` / `missing` / `unset` |
| Artifact freshness | the health manifest plus each declared artifact | see five states, worst first |
| Repositories | every git repo under one root | spot unpushed commits and dirty trees; run `fetch` |
| Tasks | the Windows Task Scheduler | run, stop, enable, disable, retire |
| Skills | a skills directory | archive one out of the way, or restore it |
| Memory pool | a memory directory | see index headroom and broken links; archive an entry |
| Plugins | `claude plugin list` | enable or disable one |
| Disk | a plugin cache and a session directory | delete abandoned clone staging directories |

Five states, not two, for freshness. `LastTaskResult` is an HRESULT, not an exit code: one value
means "currently running" and another means "has never run". Treating non-zero as failure marks
healthy tasks red, so `up` / `grace` / `down` / `never` / `running` / `paused` / `unknown` stay
separate, and `unknown` is drawn so it can never be mistaken for `up`.

A fresh artifact does **not** clear a bad exit code by default. Log-shaped artifacts are usually
written on the crash path too, so a task can fail for days while its artifact stays fresh. Only an
artifact declared `artifact_written_only_on_success` earns that power.

## Configure it

Copy `categories.example.json` to `~/.task-console/categories.json` and replace the synthetic
names with your own tasks. Everything else is optional.

| Variable | What it points at | If unset |
|---|---|---|
| `TASK_CONSOLE_CATEGORIES` | your category map | `~/.task-console/categories.json`; if that is missing, every task lands in one group called uncategorized, and the page says so |
| `TASK_CONSOLE_HEALTH` | a health watch list (`task-health.json` shape) | no default; unset means the health-coverage column reads NOT CHECKED |
| `TASK_CONSOLE_ALLOWLIST` | a PowerShell file containing a `$TaskNames = @(...)` backup allow-list | no default; unset means the backup-coverage check reports NOT CHECKED rather than passing |
| `TASK_CONSOLE_HISTORY` | a poll-observation log | unset means the observation columns read NOT CHECKED |
| `TASK_CONSOLE_SKILLS` | a skills directory | unset means the skills panel reads NOT CHECKED |
| `TASK_CONSOLE_SKILL_ARCHIVE` | where archived skills go | unset means the archive button is not offered at all |
| `TASK_CONSOLE_MEMORY` | a memory directory | unset means the memory panel reads NOT CHECKED |
| `TASK_CONSOLE_MEMORY_ARCHIVER` | the existing archiver script | unset means the archive button is not offered |
| `TASK_CONSOLE_REPOS` | a directory holding git repos | unset means the repository panel reads NOT CHECKED |
| `TASK_CONSOLE_VISIBILITY` | a map of repo path to PUBLIC/PRIVATE | unset means visibility shows as unknown, never guessed |
| `TASK_CONSOLE_PLUGIN_CACHE` | a plugin cache directory | unset means the cache row reads NOT CHECKED and nothing can be deleted |
| `TASK_CONSOLE_SESSIONS` | a session transcript directory | unset means that row reads NOT CHECKED |
| `TASK_CONSOLE_CLAUDE` | the CLI used for plugin actions | falls back to PATH; not found means the plugin panel reads NOT CHECKED |
| `TASK_CONSOLE_ALLOWED_HOSTS` | extra Host header values to accept | only the three loopback spellings are accepted |

The tool defaults only into its own namespace. Pointing it at whatever else a machine keeps its
watch-list and allow-list in is the launcher's job, and the launcher belongs on that machine.

That last row is the important one. **Unknown and pass are never rendered the same.** A console
that showed a green backup-coverage column because it had nothing to compare against would be
worse than one that showed nothing at all.

A task that appears in no category still shows up, in a group called uncategorized. It is never
dropped: the task nobody categorised is the one nobody is watching.

## What it will not do

It cannot create a task. Creating one correctly means naming it so the backup drift gate can see
it, choosing its settings deliberately, generating its launcher, and registering it in three
places. A button that skipped those steps would manufacture exactly the untracked task that
procedure exists to prevent.

It can **retire** one, which is the opposite operation and is safe to automate precisely because
it is subtractive: disable, write the reason into the description, drop it from the backup
allow-list, drop it from the health manifest. All three, or none. A task that is only disabled is
worse than one that was never touched, because an exported task definition does not record the
disabled state: a backup that still lists it will faithfully reinstall it, enabled.

There is no push button anywhere. `fetch` is offered because it only reads; pushing is an outward
action that cannot be taken back, and a one-click version of it would eventually be clicked while
nobody was looking.

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
4. **A Host header allowlist.** Binding to loopback stops the network; it does not stop DNS
   rebinding, and rebinding is the attack that matters here, because the token is substituted into
   the page at `/`: anything that can make a same-origin request to `/` simply reads it out of the
   HTML. Only the three loopback spellings are accepted, a missing Host is rejected, and `"*"` is a
   separate branch rather than an entry in the list, so no hostname can be spelled in a way that
   turns the check off. The class default is an empty set, so a handler that never went through
   startup refuses everything: forgetting to configure it and deliberately allowing everything must
   not be the same state.
5. **Maintenance actions live on a separate closed table** from the task verbs. One shared table
   would mean that adding a skill action silently widens the task surface, and nobody reviewing the
   diff would notice. Every argument passes a narrow character class, and any path must resolve to
   a direct child of its configured root; failing either refuses the whole action rather than
   sanitising the input, because sanitised input looks safe without anyone knowing what was removed.

Some tasks are owned by SYSTEM or registered at `RunLevel=Highest` and cannot be touched from a
normal user session. The console says so instead of reporting a bare access-denied.

## Files

| File | What it is |
|---|---|
| `server.py` | the HTTP layer: routing, auth, host allowlist, the two write endpoints |
| `console.html` | the whole page, no build step and no framework |
| `collect.ps1` / `act.ps1` / `runlog.ps1` | the Windows side |
| `evtlog.py` | the fast event log reader (EvtQuery), with the PowerShell path as fallback |
| `freshness.py` | five-state artifact freshness, a pure function of timestamps |
| `selfcheck.py` | every configured source, probed and reported one by one |
| `repos.py` | git repository scan, concurrent, one timeout per repo |
| `maint.py` | the closed maintenance action table and its argument gate |
| `memops.py` | memory pool diagnosis; archiving is delegated, not reimplemented |
| `sysinfo.py` | disk, cache size, abandoned clone staging directories |
| `retire.py` | the three-place deregistration, planned first and then written |
| `console_store.py` / `console_ingest.py` / `history.py` / `timeline.py` | the run history layer |

## Data boundary

This directory ships in a public repo and holds no real state. No snapshot is cached to disk, the
category map is read from a path outside the repo, and `categories.example.json` contains only
synthetic names. Real task names are real-run data and belong in the private machine config.
