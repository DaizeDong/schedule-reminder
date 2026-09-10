# Roadmap

Current: **v0.7.0**

## v0.7.0 (current), the console, and the rule that unchecked never looks like passing

`scripts/task_console/` is a local single-page app for looking at a Windows install: its scheduled
tasks, the git repositories under one root, and whatever skill, memory and transcript directories
the operator points it at. It had shipped and grown for weeks without appearing in this file at
all, which is its own instance of the defect it exists to fight: a whole subsystem that the
project's own record of itself said nothing about.

What it is: a loopback-only Python server (`server.py`) that mints a token per start and never
writes it down, a page (`console.html`) that reads seventeen keys off each payload, an out-of-band
ingester (`console_ingest.py`) that pays the 109-second cost of reading the Windows event log so
page loads do not, and a database (`console_store.py`, `schema.sql`) that resolves through the
companion repo and refuses to fall back into this one. Every path it reads comes from a
`TASK_CONSOLE_*` variable; the tool ships no defaults outside its own namespace.

The invariant the whole thing is built around: **an unchecked source must never render as a passing
one.** Every panel reports `available` plus a reason in the operator's own language, `selfcheck.py`
counts every configured source into its own denominator so that a check that skipped a source cannot
print full marks, and each of the following was a real case where the two had become
indistinguishable on screen.

- **Freshness is judged, not printed.** The last-ingest time used to be a grey timestamp, so an
  ingester stopped for two weeks looked like one that had just run: heatmap drawn, health
  percentages specific, all of them frozen. `console_store.ingest_verdict` states the age as a
  verdict, judges the oldest pipeline rather than the newest, and separates "never ingested" from
  "fine" because a judge fed nothing prints the same green as a judge that checked. Its thresholds
  measure loss risk: the Windows run log is a rolling buffer that wraps in five to eight days, so
  past four days the unread history is gone rather than late.
- **The visibility view had never worked.** It compared filesystem paths against a table keyed by
  `owner/repo`, so it matched nothing, drew no badge on any repository, and said nothing about it,
  which is exactly what "the table has no row for these repos" also looks like. Matching is now by
  owner and repo, the panel reports how many of the loaded rows matched, and a table that loads but
  matches zero says so.
- **Repositories are grouped by observed type**, with each companion configuration repo nested in
  its host's card rather than placed adjacently in a grid that reflows. Adjacency is not a
  relationship once the column count changes.
- **The README's environment table is reconciled against what the code reads**, in both directions,
  because the launcher that sets those variables lives outside this repo and cannot be tested from
  here. Its upstream can be: a variable missing from the table is a variable missing from every
  launcher anyone writes from it. That gate immediately found three.
- **One source of truth per fact**: a single `$TaskNames` parser (`allowlist.py`), one set of
  declared-OK exit codes (`freshness.py`), health entries merged to the strictest when a name is
  declared more than once and the duplication reported rather than silently resolved.
- **Every gate is poisoned before it is trusted**, with a positive control alongside, since a
  poison that does not change behaviour reads as a judge that cannot catch anything. Suite 541 →
  694.

Note that the ingester is a separate program. Nothing in this repo schedules it, so on any install
where it has not been registered the database advances only when someone runs it by hand, and the
freshness verdict above is what says so.

## v0.6.0, one reader and one writer
- **One enumeration of which channels are read** (`ingest.channels()`): registered streams, plus
  every other readable text channel in the guild, plus the operator DM. This replaced a second
  reader that swept the guild on its own timer with its own cursors. The two disagreed about which
  channels exist, and a message in one list and not the other was consumed by the reader that could
  not act on it and never seen by the one that could, leaving no error and no record.
- **The invariant that comes with it**: a message the bus reads is either claimed by a handler or
  written to an inbox. Never neither.
- **A registry of deterministic command handlers** (`commands.py`), tried per message before the
  judgment chain. Declaring a trigger and an `exec` is how a tool gets a Discord front end; writing
  a poller is not.
- **The egress can finally do what forced callers to fork it**: attachments and an explicit channel,
  over a bot transport, chosen automatically from what the caller asks for.

## v0.5.0, the bus can act
- **Execution tier** (`agent_task.py` / `agent_run.py` / `agent_tick.py`): a reply that asks for
  something to HAPPEN becomes a work order on this same pool, drained by its own PT2M task
  (`AgentCenterWorkTick`) into a detached runner. A round is act, verify, review, decide; the agent
  must hand back a check that could have failed, this side runs it, and only a passing check reaches
  an independent reviewer on another provider. No progress rotates the approach instead of ending the
  order, and nothing is ever reported done on the agent's own say-so.
- **`dispatch` gains `agent` and `stop`**, and its prompt now separates a change to the record from a
  change to the world. Inbound text replies are owner-only and fail closed.
- Checks run in PowerShell on Windows with exact exit-code propagation; the first live run proved cmd
  was rejecting correct work. Suite 99 to 151, all seventeen mutations caught, and CI finally runs it.

## v0.4.2, self-contained egress
- **Native Big Brother DM** (`bigbrother.py`, stdlib): the base opens the operator DM and posts
  itself, reading `reader.bot_token` / `big_brother.user_id` from the registry, no more shelling to
  the legacy DM notifier script. `relay._big_brother` + `notify.py` fallback use it; fixes the
  digest/unknown-stream mis-route (now reaches the DM, not the `#reminders` channel). Bot writes send
  the official `DiscordBot (...)` UA (browser UA is WAF-403'd on message-create). `ingest.py` reads
  the token only from the registry; `store.py` health probes `relay.py`. Suite 86 → 93.

## v0.4.1, relay polish
- **Suppress Discord link-preview cards** (`_post_webhook` sets `flags=4` SUPPRESS_EMBEDS by
  default; callers can opt back in with `flags=0`). The relay is content-only by design.

## v0.4.0, Agent Center two-way bus
- **Inbound poller** (`ingest.py`): the mirror of `relay.py`. Polls every stream channel for new
  **user replies** (neither bot nor webhook), advances per-stream cursors, writes inboxes; first
  contact arms a stream (no history replay).
- **Judge-then-execute dispatcher** (`dispatch.py`): asks the LLM chain for a JSON action plan, then
  a **deterministic** executor runs it via `reminder.py`, acting only on ids shown to the model
  (anti-hallucination). Per-stream handlers: `mail`→email-monitor pool, `reminders`→done/snooze,
  others→generic follow-up.
- **Cost-ordered LLM chain** (`llm_chain.py`): `codex → cc → claude`, read-only, first non-empty wins,
  deterministic no-op if all down. The reusable primitive for every headless judgement in this skill.
- **Scheduled** `ingest_tick.py` via Windows task `AgentCenterIngestTick` (PT10M); supersedes the
  retired mail-only `AgentCenterMailTick`. +16 tests → 86. Reminder contract api_version unchanged
  (1.0.0, the bus adds no verbs/fields).

## v0.3.0, Agent Center backend
- **Unified relay** (`relay.py`): the single Discord egress for every skill, multi-stream webhooks
  with per-stream identity, registry-driven (registry file via `AGENT_CENTER_CONFIG`), Big-Brother fallback,
  mandatory User-Agent.
- **Daily digest aggregator** (`digest.py`): one daily task assembles every installed skill's
  当日总结段 into a single Big-Brother summary; pluggable contributors, fail-soft per section.
- See `reference/agent-center.md`. (reminder contract api_version unchanged → 1.0.0.)

## v0.2.0
- **RRULE rolling recurrence** (§2.4): fired `recurrence` items roll to the next future occurrence
  and re-arm; minimal stdlib `FREQ`/`INTERVAL`/`UNTIL` subset + `exdate` skip; series never
  materialised. (E14)
- **Per-alarm lead times** (§4.5): `alarms[]` (`{"lead":secs}` / iCal `{"trigger":"-PT15M"}`) fire an
  item before `due_at`; applied by `due` + `tick`. (E15)
- Concurrency hardening: exclusive `claimed_at` claim + stale reclaim → no concurrent-tick
  double-fire. `progress` 0-100 enforced on every write (`ERR_BAD_PROGRESS`).
- New additive `add` flags (`--recurrence`/`--rdate`/`--exdate`/`--alarms`); contract still
  `api_version 1.0.0` (item field + verb sets unchanged). 41-test suite (E1-E15).

## v0.1.0
- T0 base: SQLite (WAL) storage with `store.py` engine.
- Unified `item` model (event/task), immutable UUIDv7 keys, append-only `events` audit stream.
- Guarded state machine (pending/doing/done/blocked/cancelled) + write-time invariants.
- Frozen CLI/JSON contract `reminder.py <verb>` (`api_version 1.0.0`).
- Concurrency safety: BEGIN IMMEDIATE writes, optimistic CAS, in-process write lock, BUSY back-off.
- Idempotent writes (UPSERT on idempotency_key); MUST-PRESERVE unknown fields via `ext`.
- Due reminders: single PT5M heartbeat + stateless `tick` reconciliation, at-least-once + dedupe,
  exponential retry back-off then `blocked` + alert; pluggable notify channel (Discord relay).
- `install.ps1` (DB + scheduled task + junction + health); 35-test acceptance suite (E1-E13).

## Planned
- **v0.2.x**, RRULE `BYDAY`/ordinal (`1FR`/`-1SU`) + `COUNT`; `rdate` extra-occurrence merge;
  `VACUUM INTO` backup helper + scheduled cold-snapshot task; idle WAL checkpoint.
- **v0.5**, optional MCP wrapper over the same `store.py` (only if a cross-client/remote need
  appears); richer `query` filters (tag/project/priority ranges).
- **v0.5**, agent-skills-eval lift (G1) + held-out trigger-rate optimization (G2) wired into CI.
- **Backlog**, archival/cold-store job for aged done/cancelled items; alternate channels
  (feishu/email) behind the same `notify()` seam.
