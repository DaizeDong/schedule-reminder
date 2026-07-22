# Roadmap

Current: **v0.4.2**

## v0.4.2 (current), self-contained egress
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
