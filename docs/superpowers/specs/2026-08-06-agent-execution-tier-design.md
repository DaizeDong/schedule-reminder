# Agent execution tier

This retained design has been reconciled with the current implementation. Earlier partial-claim
recovery and provider-selection assumptions are superseded by the generation protocol and installed
llmcall policy below. Operational commands are in the
[Agent Center reference](../../../skills/schedule-reminder/reference/agent-center.md#execution-when-a-reply-asks-for-something-to-happen).

The inbound bus turns an owner-authorized reply into a deterministic action plan. Record changes use the reminder API; requests for execution become durable work orders.

## Work orders

A work order is an ordinary pool item with source `agent-center:work`. Fixed execution metadata lives in `ext`; requests, transcripts and verification output live in the PRIVATE companion. No runtime file falls back into public source.

Before publishing a queued item, enqueue saves the full request through an atomic replacement. The request has an integrity digest. Retries use the same action identity and preserve the original request. A missing or damaged request fails explicitly.

The lifecycle lock serializes enqueue, claim, spawn, process registration and stop. A claim atomically
publishes the pool state, execution metadata and an `agent_operations` generation with run and attempt
identities. The detached child must acquire that generation before executing. A stale parent receipt,
checkpoint or completion cannot modify the replacement generation.

## Execution and verification

The runner uses the installed `llmcall` interface. Agent calls select `mode="agent"`; review calls use the installed judge policy. This component does not select provider chains, models, fallback ladders or a machine runner.

Each round asks the actor for a verification command, runs that command, records its actual output, and obtains a separate review decision. A failing command cannot close the round. If no executable verification exists, the completion report explicitly identifies the weaker review-only basis.

The final contract requires a nonempty summary and either a nonempty command or explicit
`verify: null`. Completion requires actual actor and reviewer model identities, different reported
model families, an exact `DONE` verdict and review evidence that remains unchanged. The stored initial
baseline and review input bind that decision to the owned generation. A provider label alone cannot
prove independent review.

The installed llmcall result does not currently attest all required execution, cleanup and model-family
fields. Missing fields remain unknown: execution uncertainty becomes `reconcile`, and unavailable
verification or independent-review evidence becomes `review_unavailable`. Neither automatically
replays the actor. Confirmed failure before execution may end as `failed`. Synthetic typed-result
controls prove the completion branch; they do not establish that a live installed call supplies those
fields. Only known verification failures or an explicit `CONTINUE:` verdict may continue execution.

The actor and completion reviewer receive the complete accepted request. The review considers
all of its requirements and changes outside its scope. Verification output and change-inspection provenance remain visible in the terminal report. Repeated unchanged failures rotate the approach without carrying forward the failed reasoning.

Windows verification uses PowerShell 5.1 syntax and a UTF-8 script. An explicit exit controls the result; otherwise a nonzero native exit or an error record causes failure. Semantics must be tested with that interpreter; another PowerShell version is not an equivalent check.

## Lifecycle and stop

The work tick reconciles terminated work, refreshes durable state, and launches at most one queued
order. Any unreleased operation, including unknown cleanup after a terminal outcome, keeps the serial
slot. Supported legacy running, stop-pending and reconciliation states also keep it occupied.

Process identity consists of PID and creation time. Failed status queries are uncertainty, not evidence
of exit. Stop intent is persisted before termination. Console stop requests bind both intent and final
cancellation to the observed generation, preserving its process-identity snapshot. A replacement
generation makes the old stop stale. Cancellation is reported only after verified termination or a
verified absent/reused identity.

An interrupted spawn with no registered process is reconciled with unknown cleanup; it is not assumed
never to have executed. Losing the parent process cannot prove that descendants are gone. Cleanup
reservations are released only after quiescence is established. `agent_task.py status` exposes the
unreleased reservations and queued IDs. `recover-cleanup` requires an exact terminal generation and
an operator-reviewed audit containing a note and SHA256 digest, rechecks recorded runner and launcher
identities, and updates the unchanged reservation. The audit remains in the PRIVATE companion.
Recovery records reviewed cleanup without replaying the execution or claiming task success.

The detached runner redirects output into its private run directory. Platform behavior requires separate native validation; source inspection alone cannot prove process survival.

The runner CLI requires `--id` and `--generation` for an already-reserved operation. `--no-post`
suppresses reporting only; execution and state changes still occur. Current commands and recovery
arguments are maintained in the Agent Center reference linked above.

## Inbound delivery and authorization

Only the configured owner can cause execution. A notification-only channel is excluded by channel ID, including all registered aliases and discovery. An initially empty channel records an empty baseline; later owner messages are staged exactly once.

A dispatch plan stores the item IDs shown to its planner before executing. If an action succeeds but its outcome cannot be persisted, a retry may inspect the already-authorized target to reconcile its completed state. This does not authorize mutations to new or unshown IDs.

## Validation boundaries

Generated synthetic controls cover request persistence, atomic generations, stale stop/completion,
interrupted spawn, unknown-cleanup reservations, reviewed recovery, missing typed evidence,
identity-query failures, channel baselines, exclusions and outcome-write retries. Original contract
and platform tests remain separately required. A static compile, an inert control, native execution
and a live installation check are different evidence.
