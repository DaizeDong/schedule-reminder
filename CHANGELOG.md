# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [0.5.0] - 2026-08-06
### Added
- **An executor for the inbound bus, so a reply can make something happen.** Until now a reply could
  only change a RECORD. When a scheduled job began posting into the wrong channel, three objections
  over four days produced two to-do items and one "no matching active item" while the job kept
  posting. Every reply was processed and nothing was done. The gap was not a missing fourth pool
  operation: judgement is synchronous, read only and bounded by a ten minute tick, while real work is
  asynchronous, write capable and unbounded in time. Those are two machines, and only one existed.
  - **`agent_task.py`**, the work order. An ordinary pool item (`source=agent-center:work`), so it
    inherits durability across reboot, the audit event stream, and the state machine whose compare
    and swap is exactly the lock a queue needs. `ext` holds only short fixed `x_agent_exec_*` fields;
    the request, prompts, transcripts and check output are files in a run directory outside this
    repo, because `--ext` reaches `reminder.py` as a process argument and Windows caps a command line
    near 32767 characters. `due_at` stays NULL so a merely-running order cannot trigger the notifier.
  - **`agent_run.py`**, one order to a terminal state: act, verify, review, decide. The agent must
    hand back a command that exits non-zero when the job is NOT done; this module runs that command
    itself, and only a passing check reaches an independent reviewer on a different provider. Three
    rounds sharing a signature (normalized check output plus the changed files' content hashes) mean
    no progress, which ROTATES THE APPROACH rather than ending the order: fresh directory, different
    provider, the problem and the current world but not the reasoning that failed. Two rotations that
    both stall end it as stalled. No round or wall-clock ceiling; evidence ends a run.
  - **`agent_tick.py`**, reap then dispatch. Liveness is `(pid, process creation time)`, because
    Windows recycles pids and both existing pid locks in this fleet read a recycled number as the
    live holder. A dead run is reported and never silently requeued. The runner is launched
    `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`, measured to survive both a
    normal parent exit and the scheduler terminating the parent at its execution time limit;
    `CREATE_BREAKAWAY_FROM_JOB` is deliberately absent because inside a task it raises access denied.
  - **`dispatch.py` gains `agent` and `stop`.** `agent` carries no item id, so it cannot hallucinate
    one; `stop` is validated against the orders actually running. The confirmation appends the
    dispatched ids deterministically, so a vague model summary cannot hide a live agent. The judging
    prompt now separates a change to the RECORD from a change to the WORLD by name, including the
    counterexample that answering "make X stop" with a to-do is wrong.
  - **Scheduled task `AgentCenterWorkTick`** (PT2M), separate from the inbound tick so an hours-long
    job cannot starve reply ingestion. Its `<Repetition>` carries no `<Duration>`, which is what stops
    a repeating task from silently dying after 24 hours.
### Changed
- **Inbound text replies are owner-only, and fail CLOSED.** `_is_user` now takes the owner and
  `poll_all` RAISES when `registry.big_brother.user_id` is unset instead of falling back to anyone
  who can post in the channel. The reaction path always required the owner; the text path did not,
  and a text reply can now start real execution on this machine.
- **Checks run in PowerShell on Windows, not cmd.** Found by the first live run: the agent produced a
  PowerShell check, `shell=True` handed it to cmd, and cmd answered `& was unexpected at this time.`,
  recording a correct fix as a failure. The direction was safe but it burns a round every time. The
  command now goes through a temp UTF-8-with-BOM `.ps1` (nested quotes survive nothing else, and a
  BOM-less file is decoded as ANSI), and the wrapper re-raises `$LASTEXITCODE` and then inspects
  `$Error`, because `powershell -Command` collapses any native nonzero to 1 while a bare cmdlet
  failure returns 0, which is the dangerous direction. The prompt states the shell.
### Fixed
- **`ingest`/`ingest_tick` acknowledge a picked-up reply** with an eyes reaction, swapped to a check
  after dispatch, so the judgement chain's minute of silence stops looking like a lost message.
  Retries on 429, since the two same-message reaction writes reliably trip the per-route limit.
### Testing
- +52 tests (`test_agent_exec.py`), suite 99 to 151. Every property above was mutation-checked:
  seventeen deliberate defects were introduced one at a time and each was caught by a failing test,
  including the negative control that poisons the check command and proves a failing check refuses to
  close a round.
- **CI now runs the acceptance suite** (`.github/workflows/tests.yml`). Three workflows guarded this
  repo and none of them ran the tests, so its most load-bearing checks were also its only unenforced
  ones. The job pins a floor on the collected count, so a suite that evaporates cannot look like a
  suite that passes, and disables the bytecode cache: a same-size source edit inside one second
  leaves a `.pyc` Python considers valid, which really did make a reverted mutant keep running.

## [0.4.2] - 2026-07-16
### Changed
- **Native Big Brother DM, the base no longer shells out to the legacy DM notifier script.**
  New `bigbrother.py` (stdlib `urllib` only) opens the bot->operator DM and posts, reading its
  token/recipient from the SAME registry as everything else (`reader.bot_token` /
  `big_brother.user_id`). `relay._big_brother` and `notify.py`'s standalone fallback now call it.
  This retires the last dependency on the ad-hoc legacy DM notifier tooling and makes the skill
  self-contained for its phone-reaching DM.
  - **Fixes a latent mis-route:** `relay.digest()` (and the unknown-stream fallback) went through
    `notify.py` and so landed in the `#reminders` *channel*, not the Big Brother *DM* the design
    (and `registry.big_brother.transport = "dm"`) called for. They now reach the DM.
  - **Gotcha encoded in code:** the Bot message-create endpoint WAF-403s a browser User-Agent (code
    40333) paired with a Bot token; `bigbrother.py` sends the official `DiscordBot (...)` UA for
    writes. (Reads/GET and webhook POSTs are unaffected, which is why `ingest.py`/`relay.py` use a
    browser UA.)
- **`ingest.py` reads the bot token only from `registry.reader.bot_token`** (dropped the
  legacy notifier config fallback, the token is now canonical in the registry).
- **`store.py` health** probes `relay.py` (the real egress) for `relay_ok` instead of the retired
  legacy DM notifier script.
- +7 tests (`test_bigbrother.py` + reworked `test_notify_routing.py`): dryrun seam, misconfig
  guards, chunking, and the native-DM fallback routing. Suite 86 → 93.

## [0.4.1] - 2026-07-16
### Changed
- **relay: suppress Discord link-preview cards by default.** `_post_webhook` now sets `flags=4`
  (SUPPRESS_EMBEDS) on every webhook message unless the caller overrides it. The relay is
  content-only by design, and Discord's auto-generated embed cards for urls in a message are pure
  noise. A caller that genuinely wants embeds can pass `flags=0` in a `--json` payload.

## [0.4.0] - 2026-07-16
### Added
- **Two-way Agent Center bus, user replies in any stream channel become pool actions.** The mirror
  of `relay.py`: previously every channel was write-only (skills pushed out, nothing read back). Four
  new scripts make the bus bidirectional, built entirely as a schedule-reminder upgrade (no new
  service, no new dependency):
  - **`llm_chain.py`**, the reusable cost-ordered headless-judgement primitive:
    `call_chain(prompt, chain=["codex","cc","claude"], providers)` returns the first non-empty answer,
    falls through on failure, deterministic no-op if the whole chain is down. codex runs
    `-s read-only --skip-git-repo-check` (a judge never needs write). **All** future headless model
    calls in this skill go through this, not ad-hoc spawns.
  - **`ingest.py`**, inbound poller mirroring `relay.py`. `poll_all()` advances a per-stream cursor
    (`<state-dir>/<stream>.last` under the Agent Center config dir) and writes `<stream>.inbox` for streams with a **new user
    reply** (neither `author.bot` nor `webhook_id`, the skill's own confirmations never feed back).
    First contact **arms** a stream (no history replay). Bot token from `registry.reader.bot_token`
    else the legacy notifier config file.
  - **`dispatch.py`**, two-phase judge-then-execute (anti-hallucination). Gathers the stream's active
    items as `id | title`, asks the chain for a JSON action plan
    `{actions:[{op:done|snooze|create,...}], confirm}`, then a **deterministic** executor runs it via
    `reminder.py`, acting only on ids that were shown to the model, silently skipping any hallucinated
    id. Per-stream handler: `mail` → reconcile the email-monitor pool; `reminders` → done/snooze any
    active reminder; others → generic create-a-followup. `--no-post` for dry runs.
  - **`ingest_tick.py`**, scheduled entrypoint: `poll_all` + dispatch each new reply, logs to
    the ingest tick log under the Agent Center state dir.
- **Scheduled task `AgentCenterIngestTick`** (PT10M) runs `ingest_tick.py`. Retires the ad-hoc
  `AgentCenterMailTick` (a mail-only loop under the legacy notifier dir), now disabled.
- Docs: `reference/agent-center.md` gains an *Inbound* section; `SKILL.md` and `deployment.md` note
  the two-way bus and the ingest task.
- +16 tests (`tests/test_ingest_dispatch.py`): the executor's **anti-hallucination guard** (a plan id
  that was not shown to the model never reaches `reminder.py`), JSON-plan extraction (fenced / prose-
  wrapped / nested-brace / garbage→None), thread-key collision avoidance for Chinese titles, per-kind
  create routing, the dispatch happy-path / unparseable-passthrough / `--no-post` dry run, and the
  `ingest._is_user` bot+webhook filter. Suite 70 → 86.

## [0.3.2] - 2026-07-13
### Fixed
- **The tick posted reminders to the Big Brother DM, not the Agent Center `#reminders` channel;
  the implementation had silently diverged from its own architecture doc.** `reference/agent-center.md`
  has said `schedule-reminder tick --(relay.py send --stream reminders)--> #reminders` since v0.3.0,
  but `notify.py` still shelled out to the legacy DM notifier script (a DM). The
  `agent-center-hub` / `push.py` exploration that was meant to unify egress was **archived and never
  adopted** (2026-07-01, decision B = "`relay.py` is the single egress"), and this last mile was
  never migrated. `notify()` now resolves: `SCHEDULE_RELAY_CMD` (override + test seam) → `relay.py
  send --stream reminders` → `send.py` (DM) only if `relay.py` is absent.
  - New env: `SCHEDULE_RELAY_PY`, `SCHEDULE_RELAY_STREAM` (default `reminders`).
  - ⚠️ **Operational note now documented in `deployment.md`:** a channel post does *not* push to a
    phone unless that channel is set to *All Messages*; a DM always does. Routing reminders to a
    channel is only safe once `#reminders` is set to notify.
- +7 regression tests (`tests/test_notify_routing.py`): the default is the `#reminders` channel and
  **not** the DM, `SCHEDULE_RELAY_CMD` still wins (or every tick test would push to real Discord),
  the stream is configurable, the DM remains the fallback when `relay.py` is missing, and a delivery
  failure returns `False` rather than raising. Suite 63 -> 70.

## [0.3.1] - 2026-07-13
### Fixed
- **The heartbeat had been dead for 17 days, every reminder due since 2026-06-26 silently never
  fired.** Two independent bugs, both of which broke the base's core promise ("a reminder you set
  will fire") *without leaving a trace in the DB*:
  - **Bounded repetition.** `install.ps1` registered the PT5M heartbeat with
    `<Duration>P1D</Duration>`, so Windows repeated it for exactly 24h and then stopped forever
    (`NextRun` empty). `StopAtDurationEnd` does **not** save you, it only decides whether a
    *running* instance is killed at the end of the duration. `<Duration>` is now omitted, which is
    what makes a repetition indefinite. (email-monitor hit the identical bug and fixed it in its own
    v0.1.3, the base was never fixed, so the thing every other skill depends on was the one left
    broken.)
  - **`sys.stdout` is `None` under `pythonw.exe`.** The task runs windowless, so CPython sets
    `sys.stdout`/`sys.stderr` to `None`. `_emit()` wrote with `sys.stdout.write()` →
    `AttributeError` → `_fail()` wrote with `sys.stderr.write()` → `AttributeError` again → escaped
    → **exit 1**. Every scheduled tick exited 1 *after already completing its work*, so a real
    failure and a cannot-print were indistinguishable and the permanently-red task got ignored.
    Output now goes through a `_write()` helper that no-ops on a missing stream: **reporting a
    result may never fail the operation that produced it**. (`print()` was always None-safe;
    `sys.stdout.write()` never was.)
- +7 regression tests (`tests/test_heartbeat_survival.py`): the registered repetition carries no
  `<Duration>`, and `_emit`/`_fail` return 0/1 instead of raising when either stream is `None`
  (while still emitting the same JSON contract when the streams exist). Suite 56 -> 63.

## [0.3.0] - 2026-06-27
### Added
- **Agent Center unified relay** (`scripts/relay.py`): the single Discord egress all skills route
  through. Multi-stream webhooks (`mail/hotspots/demand/promotion/support/crypto/infra/reminders`)
  with per-message `username` for per-stream identity; registry discovery via `AGENT_CENTER_CONFIG`
  → the registry file (its default lives outside this repo); unknown-stream/missing-registry falls back to Big Brother DM so
  no message is lost; mandatory `User-Agent` (Discord/Cloudflare 403s the default urllib UA);
  `AGENT_CENTER_RELAY_DRYRUN` test seam. `list`/`health` never print webhook secrets.
- **Daily digest aggregator** (`scripts/digest.py`): realises skill todo.md's single "每日固定定时
  任务 + 当日总结", every *installed* skill registers a section contributor; one task assembles all
  sections into one Big Brother summary. Fail-soft per contributor (timeout/nonzero → skipped +
  reported to `#infra`); child stdio forced to UTF-8; `register`/`unregister`/`list`/`run --dry-run`/
  `collect` (emit sections only, no send, for folding the skill digest into an existing daily push,
  e.g. the 22:00 config-backup wrap-up, so the user gets ONE daily summary covering everything).
- `reference/agent-center.md`: frozen relay + digest contract for downstream skills.
- Tests: `tests/test_relay.py`, `tests/test_digest.py` (hermetic, +13 cases → 54 total).
### Notes
- The reminder contract `api_version` is unchanged (**1.0.0**), this release only adds new sibling
  tools; downstream skills already on the base are unaffected.

## [0.2.0] - 2026-06-25
### Added
- **RRULE rolling recurrence** (architecture §2.4): on fire, a `recurrence` item rolls to its next
  future occurrence and re-arms instead of being permanently notified, long-overdue items catch up
  once, then re-arm. Minimal stdlib RFC5545 subset (`FREQ`/`INTERVAL`/`UNTIL`, `exdate` skip); the
  infinite series is never materialised. (E14)
- **Per-alarm lead times** (architecture §4.5): `alarms[]` entries (`{"lead":secs}` or iCal
  `{"trigger":"-PT15M"}`) make an item fire *before* `due_at`; effective lead = max(global `--lead`,
  alarm leads). Applied by both `due` and `tick`. (E15)
- New additive `add` flags: `--recurrence`, `--rdate`, `--exdate`, `--alarms` (item field set and
  verb set unchanged → `api_version` stays 1.0.0).
### Fixed
- **Concurrent-tick double-fire (MEDIUM)**: the `tick` claim is now exclusive on `claimed_at` (only
  an unclaimed-or-stale row is grabbed) and the candidate SELECT skips freshly-claimed rows, so two
  overlapping ticks (manual vs the PT5M heartbeat, or a tick running > 5 min) never both push the
  same reminder. Stale claims (`> _CLAIM_TTL`, left by a crashed tick) are reclaimed.
- **progress invariant**: `add`/`update`/`transition` now reject out-of-range progress with
  `ERR_BAD_PROGRESS` (previously `update --set progress=99999` was accepted).
- **Internal error redaction**: the `ERR_INTERNAL` fallback no longer echoes `str(e)` (which could
  embed the db path), only the exception type name.
- Docs: dangling `ARCHITECTURE.md` section refs repointed to in-repo `docs/design-brief.md`;
  removed the misleading `--json` flag mention (JSON is always emitted); `.sie/` sandbox added to
  `.gitignore`; 9th plugin keyword added for the base-9 GitHub topics target.
### Tests
- Acceptance suite grows 35 → 41: E14 rolling (+ UNTIL stop), E15 alarm lead (+ iCal trigger),
  exclusive-claim concurrency guard, progress-bounds guard. Each verified red on the prior code.

## [0.1.0] - 2026-06-25
### Added
- T0 schedule/memo base: SQLite (WAL) storage engine (`store.py`).
- Unified `item` model (event/task) with immutable UUIDv7 keys and an append-only `events` audit
  stream written in the same transaction as the projection.
- Guarded state machine (pending/doing/done/blocked/cancelled) with write-time invariants
  (done → end_at + progress=100; depends-on enforcement; blocked needs blocker/reason).
- Frozen external contract `reminder.py <verb> --json` (`api_version 1.0.0`): add/get/list/query/
  update/transition/done/block/snooze/due/tick/events/health.
- Concurrency safety: BEGIN IMMEDIATE writes, optimistic CAS, in-process write lock, bounded BUSY
  back-off; PRAGMA WAL / busy_timeout=10000 / synchronous=NORMAL / foreign_keys=ON.
- Idempotent writes (UPSERT on idempotency_key); MUST-PRESERVE unknown fields via the `ext` container
  (`x_<skill>_*` namespace).
- Due-reminder dispatch: single PT5M heartbeat + stateless `tick` reconciliation (free missed-fire
  catch-up), at-least-once delivery + dedupe, exponential retry back-off then `blocked` + self-alert;
  pluggable `notify()` channel (default Discord relay, swappable via `SCHEDULE_RELAY_CMD`).
- `install.ps1` idempotent installer (DB init + scheduled task + junction + health).
- 35-test acceptance suite (E1-E13) driving the CLI via subprocess; E8/E9/E11/E12 red-line gates.

### Known limitations
- Recommends SQLite ≥ 3.51.3 (WAL-reset bug); `health` warns rather than hard-failing on older hosts.
- Recurrence/RRULE is stored but not yet expanded (roadmap v0.2).
