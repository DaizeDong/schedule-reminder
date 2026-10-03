# Agent execution tier

The inbound bus turns an owner-authorized reply into a deterministic action plan. Record changes use the reminder API; requests for execution become durable work orders.

## Work orders

A work order is an ordinary pool item with source `agent-center:work`. Fixed execution metadata lives in `ext`; requests, transcripts and verification output live in the PRIVATE companion. No runtime file falls back into public source.

Before publishing a queued item, enqueue saves the full request through an atomic replacement. The request has an integrity digest. Retries use the same action identity and preserve the original request. A missing or damaged request fails explicitly.

The lifecycle lock serializes enqueue, claim, spawn, process registration and stop. A partial pending-to-doing claim retains a queued execution state and can be recovered under the same lock. Only the claimant that completes the running-state update may launch.

## Execution and verification

The runner uses the installed `llmcall` interface. Agent calls select `mode="agent"`; review calls use the installed judge policy. This component does not select provider chains, models, fallback ladders or a machine runner.

Each round asks the actor for a verification command, runs that command, records its actual output, and obtains a separate review decision. A failing command cannot close the round. If no executable verification exists, the completion report explicitly identifies the weaker review-only basis.

The actor and completion reviewer receive the complete accepted request. The review considers
all of its requirements and changes outside its scope. Verification output and change-inspection provenance remain visible in the terminal report. Repeated unchanged failures rotate the approach without carrying forward the failed reasoning.

Windows verification uses PowerShell 5.1 syntax and a UTF-8 script. An explicit exit controls the result; otherwise a nonzero native exit or an error record causes failure. Semantics must be tested with that interpreter; another PowerShell version is not an equivalent check.

## Lifecycle and stop

The work tick reaps terminated work, refreshes durable state, and launches at most one queued order. Any remaining running or stop-pending order keeps the serial slot.

Process identity consists of PID and creation time. Failed status queries are uncertainty, not evidence of exit. Stop intent is persisted before termination. Cancellation is reported only after verified termination or a verified absent/reused identity. An unresolved launch remains tracked.

The detached runner redirects output into its private run directory. Platform behavior requires separate native validation; source inspection alone cannot prove process survival.

## Inbound delivery and authorization

Only the configured owner can cause execution. A notification-only channel is excluded by channel ID, including all registered aliases and discovery. An initially empty channel records an empty baseline; later owner messages are staged exactly once.

A dispatch plan stores the item IDs shown to its planner before executing. If an action succeeds but its outcome cannot be persisted, a retry may inspect the already-authorized target to reconcile its completed state. This does not authorize mutations to new or unshown IDs.

## Validation boundaries

Generated synthetic controls cover request persistence, claim recovery, stop ownership, identity-query failures, channel baselines, exclusions and outcome-write retries. Original contract and platform tests remain separately required. A static compile, an inert control, native execution and a live installation check are different evidence.
