# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [Unreleased]

### Configuration lifecycle
- Declare native registry settings explicitly and add a non-overwriting initializer plus a local schema/private-storage doctor. Empty registries stay NOT READY.
- Match inbound ownership requirements and the runtime readers' UTF-8 format; missing owner identity and BOM registries remain NOT READY without rewriting settings.
- Preserve independent AGENT_CENTER_CONFIG, companion and DATA/database override semantics; configuration readiness is separate from database migration, Scheduler registration and delivery receipts.
- Align current entry and philosophy guidance with the documented read-only linkage seam and Windows installer.


### Added
- `tools/llmcall_contract.py` and its tests check every `llmcall.call` site against
  `inspect.signature(llmcall.call)` of the installed package, read in a clean child interpreter.
  A synthetic call in the retired shape (`cwd=`, `cancel=`, `requirements=`) is the negative control.
- Expose revision-bound work feeds, manual completion, reviewed Task Console links and durable action receipts through the owner CLI.
- Add creation preflight and `ensure` for obligation reuse, explicit follow-ups and occurrence-aware request identities.
- Persist notification claims under a stable business-event identity. Uncertain sends require reconciliation, and retries preserve the prepared target and payload.

### Changed
- `relay.py send --idempotency-key K [--receipt-adapter NAME]` prints one JSON receipt for a caller
  that must prove delivery: `confirmed` with the Discord message IDs, `not_applied` only when nothing
  can have been sent (no Big Brother fallback in this mode), otherwise `uncertain`. Plain `send` is
  unchanged.
- The live PRIVATE visibility check no longer depends on which gh account is active: it asks the
  pinned Guards kit, which tries the owner's stored account, every other stored account and gh's
  default before refusing. Switching the active gh account no longer fails the proof.
- Each work runner now starts suspended inside its own named Job Object (breakaway allowed, no
  kill-on-close) and is resumed only after it is a member. A console stop terminates that job and
  releases the serial slot once the job reports no active process (`verified-job-kill` receipt), so
  an unrelated process born during the stop no longer holds the slot. Processes that left the job
  by breakaway while their parent was still a member are reported, not killed. Without a job (it
  could not be created or opened) the stop uses the process-tree check below, unchanged.
- A console stop whose `taskkill` is confirmed by a fresh process snapshot (every recorded runner
  descendant and every later descendant gone) now releases the serial work slot itself with a
  `verified-tree-kill` receipt. An already-exited runner, a snapshot failure, any survivor, a live
  process born after the recording whose parent cannot be identified, or llmcall's own report of
  unconfirmed cleanup keeps the reservation for reconcile or `recover-cleanup` as before.
- Install the Guards runtime from the reviewed submodule during dependency setup, and check dependency consistency before the offline suite.
- Declare narrow diagnostic output families with private owner/selection manifests,
  conditional source-copy and bundle holds, rebuildable validation, and historical
  private design-document holds. Existing ownership, protected paths and budgets stay unchanged.
- Keep unidentified state producers and action containers as explicit gaps. Metadata
  declarations do not establish runtime success, complete owner recovery or authorize retirement.
- Resolve default configuration through the pinned companion discovery API. Inbox state, work runs and digest records use the companion root; DATA overrides remain independent and durable dispatch records retain their DATA state directory. Uninitialized reads stay inert and writes still require PRIVATE proof.
- Declare narrow PRIVATE storage ownership for replay state and work records, with conditional retirement holds for historical console and upgrade groups. Arbitrary action outputs need owner-linked selected-output evidence; unknown paths remain failed coverage checks.
- Set the aggregate storage review threshold to 64 MiB. Required tasks, receipts and recovery evidence remain protected when capacity review fails.
- Migrate storage additively to schema 6 for operation generations and receipts. Agent workers preserve initial evidence and cleanup reservations; missing execution or model-family metadata prevents automatic completion without replaying the actor. Linux and Windows CI exercise the supported paths.
- Reconcile package metadata and README/ROADMAP version displays with the existing 0.6.0 history entry. The channel enumeration, deterministic command routing, channel-ID cursors, and attachment-capable relay remain in current source. This corrects metadata drift; it does not create a new release or alter the historical release date.
- Document current PRIVATE runtime storage, capability-selected installation and readiness evidence, owner-bound inbound staging, and reviewed work recovery. Source availability and successful health JSON remain separate from measured external readiness.

### Fixed
- Stopping a running action from the Task Console no longer reports "未收到提交确认" (`owner_reply_unknown`) after a successful stop. The stop logged `stopped <id> (...)` to stdout in front of the JSON reply, so the console could not parse it. `agent_tick` now logs to stderr, and the owner verbs (`work-action`, `work-action-result`, `work-action-stop`) hold any library output and write it to the stream that is not carrying the reply. A stop still terminates the runner tree without waiting for its cooperative cancellation, so the stopped llmcall call writes no ledger row; `reference/agent-center.md` says why a grace period was not added.
- A reviewer that answers `DONE` and then explains it completes the order instead of ending `review_unavailable`. The verdict is read from the first non-empty line: a bare `DONE` (one trailing full stop allowed) with no second verdict line, or an answer that opens with `CONTINUE:` and a reason. `DONE, but ...`, a verdict after a preamble and `DONE` followed by a `CONTINUE:` line are still refused.
- One process reuses a successful PRIVATE proof of an unchanged companion instead of proving it again for the database, the action workspace, the run directory, every lock and every record. `work-action` proved the same companion 22 times (about 64 s on a copy of the real companion and database) and `work-action-stop` 3 times (about 9 s), against the Task Console's 30 s action budget; on the same copy they now take 4.5 s and 2.9 s with one live visibility query each. A reused proof is keyed on the companion root and Git administration, its configuration (remote URLs), HEAD and the ref it names, the global and system Git configuration, the SSH client configuration, the visibility receipt, the environment and the proof implementation; any change proves in full, a refusal is never reused, ignore status is still checked per destination, an entry expires after 60 s so a long-lived tick re-proves, and there is no reuse under `GIT_CEILING_DIRECTORIES` or below a bare-looking directory.
- `work-feed` with `SCHEDULE_ACTION_WORKSPACE` set proves the action workspace once per read, on a thread that overlaps the database proof, instead of once per tracked todo. Each PRIVATE proof runs about 55 git queries and a live `gh repo view`, so a real database with about a hundred eligible todos made the read take minutes and the Task Console reader (20 s budget) reported `work_reader_failed`. Action writes still prove the workspace themselves.
- Stop a revoked or lost work order's running model call. The order already ran inside
  `llmcall.process.execution_scope(cancel=...)`; llmcall 0.3.1 polls that token while a call runs.
  Pollers now get a `PolledCancellation` that reuses the ownership answer for
  `AGENT_EXEC_CANCEL_POLL_SECONDS` (default 30 s) and latches once set, because each answer costs a
  PRIVATE proof (measured 4 to 5 s, about 54 git subprocesses and one `gh` query) and the pollers
  ask every fraction of a second. This also ends the back-to-back proofs that `verify` commands
  ran through `llmcall.process.run`. Decision points between phases keep the exact check.
- Give agent work orders their full phase budgets again under llmcall 0.3.0 (act 1800 s, review
  420 s, overridable through `AGENT_EXEC_ACT_TIMEOUT` and `AGENT_EXEC_REVIEW_TIMEOUT`). The 180 s
  llmcall default ended agent work mid-task. Reconciled from the unbranched installed line
  (`rescue/c886f69`).
- Derive work-order execution evidence from the installed llmcall 0.3.0 `Result`: model family from
  `llmcall.rung_group` of the answering rung, start and cleanup from each attempt's reason and
  tree-ownership note. Untyped results stay unresolved. Without this every 0.3.0 call was journaled
  as unknown cleanup and no work order could complete.
- Refuse explicit per-call execution `requirements` before launch, because llmcall 0.3.0 cannot
  express them, instead of widening them to the installed agent policy.
- Start the work-order runner with `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` and no
  `DETACHED_PROCESS`, which made Windows ignore `CREATE_NO_WINDOW` and gave every console child of
  the runner its own visible window. The `gh` visibility query behind PRIVATE proof also starts
  without a window.
- Accept the apply sender's `allow_drop` reload argument during route verification while keeping the probe's configuration writes and reloads replaced.
- Verify JavaScript model-mapping senders through their real settings and notification entrypoints. Absolute per-sender directory overrides support source installations while language-rule checks still run before delivery.
- Recheck transient zero-link filesystem observations during runtime path admission, up to three observations. Concurrent SQLite sidecar removal can resolve to a missing or ordinary file; persistent zero links, symlinks, reparse points and hardlinks still refuse. Opened-file inode checks remain strict.
- Manual clone instructions initialize the pinned guard and style submodules required by runtime storage checks.
- Companion restoration guidance explicitly wires a nondefault config location and retains runtime DATA under version control, with SQLite-aware backups.

## [0.6.0] - 2026-08-12
### Fixed
- **A message could be read by the bus and then seen by nobody.** An instruction typed in the
  server's own default channel sat unnoticed for three days. Nothing failed: the channel was not in
  the registry, so the inbound bus never polled it, while a second bot that swept the whole guild on
  its own timer DID read it, found no command prefix it recognised, skipped it, and advanced its own
  cursor past it. Two readers with two channel lists and two sets of cursors, neither aware of the
  other, and a message that fell between them left no error, no inbox entry and no log line. The
  root cause was not a missing registry entry. It was that "which channels do we read" had two
  answers.
  - **One enumeration** (`ingest.channels()`): every registered stream, plus every other readable
    text channel in the guild, plus the operator's DM. A channel created next month is read without
    anyone remembering to register it. Opting out is explicit (`inbound: false`) and survives
    discovery, so the sweep cannot add back a channel the owner excluded.
  - **One invariant**, now stated in the code and locked by tests: a message the bus reads is either
    claimed by a handler or written to an inbox, never neither. `poll_stream` writes the cursor and
    the inbox in one place; `commands.route` returns `(claimed, remaining)` and the two must add up.
  - **Cursors are keyed on the channel id**, not the stream name, since a discovered channel's name
    is whatever a human typed and a rename would orphan the cursor and replay that channel's
    history. The two older name-keyed schemes are adopted once, taking the NEWEST position, and
    whatever the merge stepped over is written to `<key>.migrated.inbox` for a human instead of
    being dispatched: repeating an action already taken is worse than reporting a gap.

### Added
- **`commands.py`, a registry of deterministic command handlers** tried per message BEFORE the
  judgment chain. A command is now a registration rather than a service: declare a trigger regex and
  an `exec` in `registry.commands`, and the bus hands over the message on stdin as UTF-8 json and
  reads the exit code. Writing a second poller is no longer how a tool gets a Discord front end.
  - Nothing is passed on argv (Windows PowerShell mangles non-ASCII argv, and commands get typed in
    Chinese); the child's `PYTHONIOENCODING` is pinned for the same reason.
  - A bare `python` in `exec` is rewritten to the running interpreter, because a scheduled task's
    PATH on Windows routinely holds only the WindowsApps alias, a stub that resolves and runs
    nothing. A handler that silently never executes looks exactly like a bus that stopped reading.
  - A failing handler still CLAIMS its message and the failure is reported in the channel. Feeding a
    broken command to a model that will file it as a to-do is a second wrong answer, not a recovery.
- **`relay.send()` and a bot transport**: file attachments and an explicit `channel_id`, so the one
  egress can finally do the two things that kept forcing callers to write their own Discord client
  (a webhook is bound to one channel and cannot carry a file). Transport is chosen from what the
  caller asks for, never configured. The frozen `relay(stream, content, username)` surface is
  unchanged and still uses the webhook, keeping each stream's identity.
  - A bot send has no Big Brother fallback and returns False: it is addressed at one specific
    channel, and rerouting the answer to "what you just typed in #here" into a DM is worse than a
    visible failure.
- Confirmations now go back to the channel a message came from even when that channel has no
  webhook. They used to fall back to a DM, which reads as the bot ignoring you.

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
