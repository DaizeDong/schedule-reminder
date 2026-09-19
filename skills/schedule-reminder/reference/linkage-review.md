# Reminder linkage review

`reminder_link_review.py` is the reminder owner's explicit migration and attestation
tool for `ext.task_console.task_id`. It does not create a database, upgrade the
reminder schema, back up a database, or change Scheduler tasks. Use the existing
backup and reminder schema migration procedures first when needed. Review requires
the current `store.SCHEMA_USER_VERSION`; linkage uses the existing `ext` and `meta`
storage and does not introduce another schema version.

The general `reminder.py` CLI initializes the schema before dispatching commands.
The separate owner CLI keeps review reads genuinely non-creating and non-migrating.
The linked reader remains stdlib-only and does not import `store`.

## Private inputs and explicit approval

Keep the database, mapping, task input, and review document in the existing private
data location. They are DATA, never public fixtures or files in this source tree.
The review document includes the DB path and mapped item/task IDs; errors print
only a safe code and exception type. There is no default output file or repo fallback.

Prepare a JSON decision document with `schemaVersion: 1`, a `links` object mapping
exact reminder item IDs to exact task-console task IDs, and
`unmapped_items: "reviewed-unlinked"`. The latter explicitly reviews every other
item in the captured database as unlinked, including terminal items. Do not infer
IDs from a title, source, stream, or display name. Existing links must be included
unchanged. Unknown IDs, duplicate JSON keys, malformed extensions, omitted existing
links, and attempted reassignment are errors. Multiple reminders may link to one task.

Supply the reviewed task-console request JSON through `--task-input`. The existing
`task_console.registration._compile` validates authority and derives IDs from the
component declarations, private bindings, and machine input. This also supports
installation namespaces. The reminder owner does not allocate IDs or implement
another task registry. Apply requires the identical task input again.

For an explicitly reviewed no-links database, use this decision document:

```json
{"schemaVersion":1,"links":{},"unmapped_items":"reviewed-unlinked"}
```

Only this case can omit `--task-input`. Omission asserts no task identities; it does
not invent an empty task registry. Any existing linkage makes omission fail.

Use explicit absolute paths to private files:

```text
python reminder_link_review.py --db <absolute-db> --task-input <private-request> review --mapping <private-decisions>
python reminder_link_review.py --db <absolute-db> --task-input <private-request> apply --review <saved-private-review> --approve <review_revision>
```

Save the first command's JSON output in the private location. Inspect its decisions,
counts, task IDs, and DB binding before supplying its exact `review_revision` to
apply. A revision is an explicit local approval token, not a cryptographic identity
or proof of who reviewed it. The workflow trusts the operator supplying the reviewed
task input; it does not independently attest a live Scheduler inventory.

## Boundaries and receipt

Review binds the resolved database path and file identity, SQL schema and user
version, all logical table contents, prior attestation, exact linkage coverage, and
task input. Apply recomputes the plan under `store._Tx` (`BEGIN IMMEDIATE` plus the
existing busy retry and process lock). Concurrent committed edits cause rejection.
The transaction adds only missing approved `ext.task_console.task_id` values,
preserves other extension fields, and writes the marker plus full approved review
and result fingerprints into the existing `meta` table. It does not rewrite business
fields or append/rewrite item event history. A failure rolls the entire transaction back.

Applying the same approved document again to the unchanged result returns
`already_reviewed` without rewriting the receipt. Any intervening DB content change
requires a fresh review before another application.

The reader requires both the marker and a valid receipt. A bare marker is unreviewed.
It verifies current database identity, schema, and every explicitly linked item
ID/record version/assignment against the receipt before returning any results. Only active linked items
are returned; terminal links are still validated. Business state or unrelated ext
changes do not invalidate a read receipt. Adding or deleting an item with no task
link also preserves read coverage: the reader validates its absence of a link, and
does not infer a task assignment. New, removed or changed links, a database
copy/replacement, or schema changes require a fresh review. The versioned
`explicit-links-v1` binding prevents older receipts from silently acquiring these
semantics. Apply still binds the complete logical database snapshot; reader
continuity does not authorize a stale write. Missing/unconfigured
databases return unavailable, and malformed databases/links return an error. Reads
never create or migrate a database.

Hashes detect stale reviews and accidental drift. An actor with arbitrary database
write access can forge metadata; this receipt is not a tamper-resistant audit log.

## Runtime packaging

Ship `reminder_linked_items.py` and `reminder_link_review.py` alongside the existing
`store.py`. The apply/review CLI needs the installed task-console package for nonempty
linkage. The reader itself has no dependency on task-console or store. Preserve the
existing explicit `TASK_CONSOLE_REMINDER_DB` binding. Runtime build changes belong
to the root integration owner and are not part of this owner patch.
