# Dispatch identity

Inbound messages are dispatched individually with their channel and message IDs. The owner saves
the first parsed plan before executing it. A retry uses that plan and the same action keys, even
if another model response would have changed the wording or order. A completed dispatch reuses
its stored outcomes. Identified creates use `ensure --if-exists return`, which preserves
the original keyed record and reviews cross-source candidates before creating another item.
The existing `add --if-exists return` contract also remains available. Concurrent creates
share the same storage transaction and unique key.

Before the first action, new saved plans persist `action_identity_version: 2` and canonical
`action_keys`. Sorting JSON fields while writing or reading a plan cannot change those keys.
An older unversioned pending plan can replay only when every exact canonical key already has
a terminal `succeeded` or `rejected` outcome. Missing, failed or mismatched old keys require
reconciliation before any mutation. Inspect the original durable effects; a new occurrence ID
is not an automatic workaround for uncertain old execution. No existing inbound files are
rewritten merely by installing this source.

An explicit CLI caller should pass `--request-id`. Reuse it after a timeout; change it for a new
intentional occurrence. Calls without an identity retain the legacy immediate-dispatch path;
they cannot guarantee replay protection, so integrations must supply the source identity.
`--no-post` suppresses confirmation messages but still changes records.

Generic channels read active obligations across sources and omit status signals. A follow-up uses `update` with an exact
listed ID and a note. The note is appended once without replacing prior details. An `agent` action
can reference the same ID so its execution remains linked to the todo. Similarity is advisory;
different deadlines and obligations must not be merged automatically. The two reviewed
market-intel watchdog keys are displayed as signals; other records from that producer remain work.

For example, a second request to add an appendix to an existing Acme report should reference the
report's ID. A separately requested report for another date should have a new request ID.

Saved plans retain their authorized item and work IDs, and retries use the existing inbound
outcome ledger. A follow-up receipt binds its item and note, so changing either under the same
receipt identity is rejected. Current query pagination remains authoritative for the planner's
complete active-item context.

If an outcome write is lost after a follow-up or linked work order was committed, retry may read
the matching owner receipt for a previously authorized item even after that item becomes terminal.
It verifies the original note or full request, origin and published work state; it does not use
historical authorization to make a new mutation. Missing or inconsistent evidence is rejected.
Confirmation messages count actual updated and reused records and include their IDs.

Saved stop plans also retain `authorized_work_generations`. A stop is bound to the exact observed
work item and generation, including explicit generation zero for legacy work without an operation
row. Missing generation evidence requires reconciliation. A completed stop can be recovered
read-only only for that same cancelled generation with confirmed cleanup; it cannot authorize
stopping a replacement run.

These guarantees cover record creation and plan replay. They do not prove that a model will always
recognize differently worded follow-ups, or provide exactly-once delivery of external messages.
