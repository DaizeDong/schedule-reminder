# Owner notification receipts

The owner selects a business event after deciding its outcome. Reuse the same stable run ID,
phase and condition when a notification is retried. Provider attempts, timestamps and random
retry IDs are unsuitable business identities.

`notification_receipts.Event(owner, run_id, phase, condition, stream)` requires nonempty strings.
Its ID is `notification:v1:` followed by compact JSON for `[run_id, phase, condition]`. Reusing
that ID with a different owner, stream or request payload raises an event conflict. The request
hash covers content, attachment paths, channel, username, language, verbatim declarations,
fallback and an optional command policy.

## Storage and delivery

`notification_receipts.deliver(event, content, **options)` and
`notify.notify_event(event, content, **options)` return a persisted business-event receipt.
They use the existing reminder database through `store.admitted_connection` and `store._Tx`.
Both receipt reads and writes require PRIVATE admission. The producer never creates or migrates
a database. Initialize the current store before enabling callers; missing storage or schema
remains an explicit error.

The database primary key and short `BEGIN IMMEDIATE` transactions arbitrate concurrent senders.
The producer commits a claim, prepares a target and payload, then commits a send-start marker
with `synchronous=FULL` before invoking transport. Network and command execution occur outside
the write transaction. Only the current token may finish its attempt.

| State | Meaning and retry rule |
| --- | --- |
| `pending` | An attempt owns the event. Reentry returns its receipt without sending. |
| `sent` | The boolean transport confirmed delivery. Reentry returns the same receipt. |
| `failed` | Preparation failed before transport started. Only `retry_failed=True` may claim another attempt. |
| `uncertain` | Delivery may have occurred, including partial chunks, a lost response, or an expired claim with a send-start marker. Never automatically resend. |

An expired pending claim becomes `failed` if it has no send-start marker, otherwise `uncertain`.
A late acknowledgement can resolve uncertainty for the same token; it cannot replace a newer
attempt. Reconcile external evidence before creating any separately identified follow-up.
The protocol does not promise exactly-once network delivery.

Receipts store identity, hashes, safe error codes, target fingerprints, claim times, attempts,
target-change history and an optional database-relative command snapshot reference. They do not
store message text, credentials or attachment bytes. Invalid
receipt fields fail closed, including malformed digests and incomplete target history.

## Options and preparation

| Option | Behavior |
| --- | --- |
| `channel_id`, `files`, `username` | Preserve explicit channel routing, attachments and webhook identity. Attachments travel on the first bot chunk only. Explicit channel/files never fall back to a DM. |
| `fallback` | `none` by default. An owner may explicitly select `big_brother`. The initial fallback is recorded in target history. |
| `retry_failed` | Boolean, default false. Authorizes delivery-only retry of a proven pre-send failure. |
| `allow_target_change` | Boolean, default false. Separately authorizes a changed resolved target during a failed-event retry. |
| `lease_seconds` | Positive finite claim lifetime, default 300 seconds. Expiry cannot make a started send safe to retry. |
| `language` | `zh-CN` checks the maintained notification rule. `preserve` explicitly retains an owner's existing language policy. |
| `verbatim` | A list of owner-declared fragments passed to the maintained language rule. |
| `redactor` | Python-only maintained owner callable. Changed redacted content on retry is rejected. |

The prepared registry is an independent snapshot. A registry change after preparation cannot
change the recipient. Bot attachment bytes are captured before the send marker and those same
bytes are transmitted. Changed content or attachment bytes on a later attempt are rejected.
No credential-bearing registry snapshot is persisted.

Set `SCHEDULE_NOTIFICATION_LANGUAGE_POLICY` to an absolute maintained policy path, or configure
`SCHEDULE_ROUTE_SCRIPTS_DIR` containing `notification_language.py`. A Python caller may pass
`language_policy_path` explicitly. Missing policy is a pre-send failure. Tests use the pinned,
code-only dependency selected by `SCHEDULE_TEST_LANGUAGE_RULE` and synthetic generated data.

## Consumer and compatibility APIs

`notification_client.submit(owner, run_id, phase, condition, stream, content, **options)` is the
thin consumer API. It accepts `TASK_RUN_ID` or `SCHEDULE_RUN_ID` if its run ID is omitted. An absent
identity or invalid event is an explicitly unpersisted refusal. Dry runs are unpersisted and do
not send. If a producer exception prevents a definitive result, the client returns `reconcile`,
`persisted: null`, and the derived event ID. It does not claim that a durable send marker is absent.

`notify.notify_occurrence(run_id, content, ...)` preserves the reminder command override, selected
stream and standalone routing. `notify.notify(content, run_id=...)` provides a boolean wrapper;
only `sent` returns true. Existing `notify.notify(content)` callers keep their legacy transport
until their owner supplies a stable occurrence identity. Their behavior does not change merely
because a task-level run ID appears in the environment.

`relay.send` remains boolean. The separate `relay.deliver` and `notify.deliver` APIs retain their
Discord message-ID receipts and readiness semantics. A business-event `sent` receipt is delivery
confirmation only; it is not proof of external readiness. Legacy text delivery remains available.

## UTF-8 command boundary

Run `python notification_receipts.py deliver` with one UTF-8 JSON object on stdin containing
`event`, `content` and optional `delivery` fields. This preserves newlines and Unicode without
PowerShell argument conversion. An optional event ID must match the derived identity. Output
contains `api_version`, `ok`, and the receipt; exit 0 means `sent`. Pending, failed and uncertain
return exit 1 with their receipt. Invalid input or unavailable storage returns a safe error.
The `get` subcommand accepts an event ID and reads its receipt without reconciling or sending.

An optional `command` policy preserves explicitly selected standalone notifiers. It specifies
argv, payload mode (`text`, `base64`, or `at-file`), a finite process timeout and optional cwd.
This timeout governs the notifier process, not any model call. Execution uses `llmcall.process`;
no model is dispatched and no provider routing is overridden. Text/base64 adapters use the
canonical relay chunker. Custom commands do not accept bot attachments or silently fall back
to a stream. Their fingerprint identifies the resolved argv/cwd/protocol, but cannot attest a
recipient hidden in a third-party script's own configuration.

`at-file` requires `message_file` whose UTF-8 content matches the prepared text. The producer
creates a unique snapshot in the PRIVATE `notification-payloads` directory beside its admitted
database. The command receives that snapshot, so an edit to the original file cannot change the
payload after preparation. The fingerprint retains the original file's identity. The receipt's
`payload_artifact` records `notification-payloads/<random-id>.txt` relative to the database directory.
Cleanup removes the snapshot before process invocation or when `llmcall.process` explicitly
confirms that the process tree has stopped. A lost process response or unconfirmed cleanup retains
the snapshot and leaves delivery uncertain; retries do not invoke the command again. An abrupt
producer termination can also leave a private snapshot. Reconcile the retained artifact and prove
the process tree has stopped before removing it. Missing PRIVATE admission prevents snapshot
creation and sending.
