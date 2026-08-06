# Agent execution tier for the inbound bus

Status: approved design, 2026-08-06.

## The failure this exists to fix

The Agent Center bus is two-way already: a reply in a stream channel is polled, judged by the
cost-ordered chain, and turned into pool mutations. But the executor behind that judgement can do
exactly three things, `done`, `snooze` and `create`. There is no path from a reply to anything
happening in the world.

So when a scheduled job started posting a daily image into the wrong channel, the owner objected
three times over four days and got, in order, a new to-do titled "check and stop the image posting",
a new to-do titled "fix the misdelivered image bug", and "no matching active item, cannot act". The
job kept posting the whole time. Every reply was processed. Nothing was done.

The gap is not a missing fourth operation. The gap is that the bus has no executor at all. Judgement
is synchronous, read-only and bounded by a ten minute tick; real work is asynchronous, write
capable and unbounded in time. Those are two machines. This spec adds the second one and connects
it to the first through a queue.

## Invariants

These hold no matter what else changes. Everything below is in service of them.

**A reply that asks for something to happen produces work, not a record of work.** Creating a to-do
in response to "make X stop" is the defect, not a partial success.

**Completion is never the agent's own word for it.** A round closes only when a deterministic
command that could have failed did not fail, and an independent reviewer that did not write the
code agrees. Where no such command can exist, the report says so in as many words.

**No terminal report may claim success without showing its evidence.** What changed, which command
was run, what that command actually printed, what the reviewer said. The phrase "handled" is banned
from terminal reports; it is the exact word that papered over four days of the incident above.

**Only the owner can cause execution.** Anything else is a claim rather than a fact.

**The queue never lies about liveness.** A work order recorded as running is either the same live
process that was launched or it is reported dead. There is no third state and no silent requeue.

## Shape

```
reply in #stream
  -> ingest (owner-gated)
  -> dispatch: judge -> action plan -> deterministic executor
       ops done|snooze|create   act on the pool, as today
       op  agent                enqueue a work order, confirm immediately
       op  stop                 cancel a running work order
  -> [queue: pool items, source agent-center:<stream>]
  -> work tick (every 2 min): reap the dead, then claim and launch one
  -> runner (detached, own process, hours if needed)
       round: act -> verify -> review -> decide
       stall -> rotate approach
  -> terminal report back to the origin channel
```

## Stage 1, the work order

**What it is.** An ordinary pool item. `kind=task`, `source=agent-center:<stream>`, `due_at` left
NULL. Title is a short Chinese summary of the ask.

**Why the pool and not a new store.** The pool is already durable, already survives reboot, already
reachable from Discord, already has an audit event stream, and already has an optimistic state
machine whose compare and swap is exactly the lock the queue needs. A second store would duplicate
all of it and diverge.

**Why `due_at` stays NULL.** An item with a due date in `pending`, `doing` or `blocked` is a live
candidate for the reminder tick, which would fire Discord notifications for a work order that is
merely running. Leaving it NULL keeps the work order out of the reminder path entirely.

**What goes in `ext`, and what does not.** `ext` carries only small fixed fields under an
`x_agent_exec_` prefix: schema version, origin stream, origin message id, execution state, process
id and process creation time, run directory, round and approach counters, workspace path. Nothing
that grows.

Everything that grows, the verbatim request, the prompts, the per-round transcript, the verification
outputs, the diffs, lives as files in a per-order run directory under the Agent Center config dir,
alongside the cursors and inboxes the bus already keeps there. Two reasons. `--ext` is passed as a
process argument and Windows caps a command line near 32767 characters, so a transcript in `ext`
fails eventually and does so at the worst moment. And the pool is a state store, not a log store.

The run directory is real runtime output, so it lives outside this repo, in the private companion
that already holds every other piece of bus state. It is never resolved to a repo relative path and
there is no in-repo fallback.

**Execution state versus pool state.** They are different alphabets and conflating them loses
information. Pool state answers "is this item still open"; execution state answers "what is the
runner doing". The mapping is: `queued` is pool `pending`; `running` and `stalled` are pool `doing`;
`done` is pool `done`; `failed` is pool `blocked` with a reason.

## Stage 2, dispatch learns to hand off

**Two new operations in the action plan.**

`{"op":"agent","request":"...","workspace":"...","why":"..."}` enqueues a work order. It carries no
item id, so there is nothing for the model to hallucinate; the anti-hallucination rule that governs
`done` and `snooze` is not weakened, it simply does not apply.

`{"op":"stop","id":"..."}` cancels a work order. Its id is validated against the set of work orders
that are actually running, which is the same allowlist discipline the existing operations use. An id
outside that set is skipped and named in the confirmation.

**The triage instruction.** The judging prompt gains an explicit rule: choose `agent` when the reply
asks for a change in the world, and `create` only when the reply asks for a change in the record. It
also gains the counterexample by name, that answering "make X stop" with a to-do is wrong.

**The confirmation is immediate and specific.** It names the work order id and what was accepted,
and it goes out before any work starts, so the ten minute tick is never held open by execution.

## Stage 3, the runner

**Where it runs.** Its own detached process, launched with `DETACHED_PROCESS`,
`CREATE_NEW_PROCESS_GROUP` and `CREATE_NO_WINDOW`, stdout and stderr redirected to the run log. This
combination was measured on this machine: such a child keeps running both when its parent exits
normally and when the scheduler terminates the parent at its execution time limit. Do not add
`CREATE_BREAKAWAY_FROM_JOB`; from inside a scheduled task it raises access denied, and it is not
needed.

**How it talks to models.** Through llmcall, with three deliberate deviations from the defaults,
each of which is a measured hazard rather than a preference.

Before importing llmcall it points `LLMCALL_AGENT_RUNNER` at the shim that forwards `-DirectOnly`
and `-NoCodex`. The variable is read at import time and cannot be changed afterwards. On this
machine it is otherwise set to the real machine runner, which internally retries cc, then codex,
then claude direct, three rounds with ninety second sleeps. That means the cc leg of an agentic call
runs codex a second time, so a file edit or a task change executes twice.

It runs `chain=["codex"]` by default. One provider means the reported provider is the true one and
the side effects happen once. Only if the codex leg is unavailable, as opposed to merely producing a
failing verification, does it retry on `["cc"]`.

It validates the mode string itself. llmcall's mode tuple is dead code, so a typo silently degrades
an agentic call to a read-only judgement that quietly changes nothing.

**Where it can write.** codex under `workspace-write` can write in the process working directory
plus temp, and llmcall never sets a working directory, so the sandbox is whatever the caller's
current directory happens to be. The runner therefore changes directory into the work order's
workspace before the first call. Known limit, recorded here rather than discovered later: shell
network access is off under that sandbox, so steps that need the network, a push being the obvious
one, do not complete and must be reported as not done rather than assumed.

**One round.** Act, verify, review, decide.

*Act.* The agent is given the request, the workspace, the current state of the world and, from the
second round on, the previous round's verification failure verbatim. It is required to return a JSON
tail carrying `verify`, a command that exits non-zero if the job is not done, `changed`, the files it
touched, and `summary`.

*Verify.* The runner executes `verify` itself and records the return code and the real output. The
agent does not get to report on its own verification.

*Review.* Only if verification passed. An independent reviewer on a rotated provider chain, read
only, is given the original request, the diff, the verification command and its actual output, and
answers DONE or CONTINUE with a reason. It is a different model call from the one that did the work,
which is the point.

*Decide.* Verification failure or a CONTINUE verdict starts another round carrying the failure text.
Both passing closes the order.

**Tasks that cannot be expressed as a command.** Some genuinely cannot. Those fall back to review
only, and the terminal report states plainly that completion was judged without an executable check.
A weaker guarantee that announces itself is safe; a weaker guarantee that looks like the strong one
is how this incident happened.

**Progress signature and stall.** After each round the runner computes a signature over the
normalized verification output together with the sorted names and content hashes of the changed
files. Three consecutive identical signatures mean the round is not moving, whatever the model says
about its own effort.

**Stalling rotates the approach rather than stopping.** A fresh run directory, a fresh agent, a
different provider order, and a prompt containing the problem statement and the current state of the
world but not the failed reasoning, told explicitly that earlier attempts failed and that the earlier
framing may itself be wrong. Two rotations that both stall end the order as stalled.

There is no round ceiling and no wall clock ceiling. The stall detector is the only thing that ends
an order that is not progressing, and it ends it by evidence rather than by a timer.

## Stage 4, the work tick

Runs every two minutes as its own scheduled task, separate from the ten minute inbound tick so that
neither can starve the other.

**Reap before dispatching.** For every work order in `running`, compare the recorded process id and
process creation time against the live process table, using an open handle plus the still-active
exit code rather than a signal probe, which on Windows is not an existence check. A process id that
matches but whose creation time does not is a different process that inherited the number. Anything
that is not the same live process means the run died: mark the order failed and post the tail of its
log to the origin channel. Never requeue silently.

**Then claim exactly one.** Take the oldest queued order and move it to `doing` with an expected
current state of `pending`. That compare and swap is what makes two overlapping ticks safe; the
loser sees a state conflict and does nothing.

**Serial by default.** One runner at a time. Two agents editing the same working tree concurrently is
a corruption source, not throughput.

**Killing.** Terminate the process tree, not the process. A launcher started through a command
wrapper has grandchildren that survive a direct kill and hold pipe handles open; that mistake once
turned a seven minute timeout into a thirty four hour freeze elsewhere in the fleet. Kill only after
the recorded creation time matches, so a recycled process id cannot get an unrelated process killed.

## Stage 5, the owner gate

The inbound filter currently accepts any message that is neither from a bot nor from a webhook. The
reaction path already narrows further to the owner recorded in the registry; the text path does not.
Granting execution to text replies without that narrowing would mean anyone who can reach the
channel can run commands on this machine. The text path is narrowed to the same owner.

## Testing

Everything below is testable without a model and without Discord, which is the point of the
judge-then-execute split.

The triage mapping from a reply to an operation. The stop operation's id validation against running
orders only. The compare and swap claim under a simulated race, where the loser must not launch. The
liveness check against a real short lived child process, spawned, killed, and asserted dead, plus a
recycled-identity shaped input asserted dead. The stall signature, identical outputs stalling and a
single byte difference not stalling. The rotation prompt asserted to contain the problem statement
and not the prior reasoning. An upper bound on the serialized size of `ext`. Terminal reports
asserted to contain the real verification output.

And one negative control, because a check that cannot fail proves nothing: poison the verification
command so that it exits non-zero, and assert that the round does not close and the order is not
reported done.

The repository currently has no continuous integration job that runs this skill's own test suite;
the guards run, the tests do not. This change adds that job, because the logic it introduces is
exactly the kind that rots silently.

## What this deliberately does not do

No parallel execution. No approval prompts for destructive operations; the boundary chosen is full
authority with a complete audit trail and an owner-only gate, on the grounds that the urgent case is
precisely the one where waiting for a confirmation defeats the purpose. No round or time ceiling. No
change to the reminder contract; every verb, flag and field used here already exists, so the api
version does not move.
