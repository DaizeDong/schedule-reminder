# Owner notification receipts

The task owner selects one business notification after its final result. Provider attempts do
not create business events. Use the same stable run ID, phase and condition on reentry; do not
substitute a provider attempt ID, a new timestamp, or a random ID on every delivery retry.

The producer is `scripts/notification_receipts.py`. It stores receipts in the existing reminder
private database through `store.default_db_path`, `store._connect` and `store._Tx`. It does not
create a second database or initialize storage. The additive store schema migration must be
integrated before enabling consumers. A missing database/table is a visible error.

## Python and CLI contracts

Trusted in-process callers use `notify.notify_event(event, text, **options)` or
`notification_receipts.deliver(event, text, **options)`. Both return the persisted receipt.
`Event(owner, run_id, phase, condition, stream)` requires every field. Its `event_id` is the
collision-free `notification:v1:` prefix plus compact JSON encoding of `[run_id,phase,condition]`.
That identifier is unique across streams. Reusing it with a different owner, stream, channel,
text, attachment path, identity or language/fallback policy fails as an event conflict.

Other consumers use `python notification_receipts.py deliver` and pass one UTF-8 JSON object on
stdin. This avoids PowerShell Unicode argv conversion and preserves newlines. For example:

```json
{"event":{"owner":"example-task","run_id":"example-run-1","phase":"terminal","condition":"success","stream":"example-stream"},"content":"任务已完成","delivery":{"fallback":"none"}}
```

The `event_id` input field is optional and, if present, must equal the derived ID. Output is
`{"api_version":1,"ok":true|false,"receipt":{...}}`. Exit 0 for deliver means `state == "sent"`;
pending, failed and uncertain return exit 1 with their receipt. Invalid inputs or storage errors
return exit 1 with `error` and a safe `reason`. `get` accepts `{"event_id":"..."}` on stdin and
returns the stored receipt without reconciling or sending. A missing receipt returns exit 1.
Only the Python API accepts a callable `redactor` or an explicit `language_policy_path`.

| Option | Contract |
| --- | --- |
| `channel_id`, `files`, `username` | Preserve explicit channel routing, attachments and webhook identity. Attachments travel once on the first bot chunk. Explicit channel/files never fall back to DM. |
| `fallback` | Required owner decision when fallback is desired: `none` by default; `big_brother` permits the existing unknown/unusable-stream DM fallback. No default stream is selected. |
| `language` | `zh-CN` checks the maintained canonical `notification_language.offences`; `preserve` explicitly retains an existing owner's language policy without rewriting text. |
| `verbatim` | Declared model IDs/quoted fragments passed to the canonical language policy. |
| `retry_failed` | Boolean, default false. Retry only a proven pre-send failure. Never retries a sent or uncertain event. |
| `allow_target_change` | Boolean, default false. Separate authorization to use a changed resolved target on a delivery retry. |
| `lease_seconds` | Positive finite claim lifetime, default 300 seconds. Expiry after the send marker cannot authorize another send. |

Receipts contain event identity, request/prepared payload hashes, attempts, claim token/times,
state, safe error code, resolved target and target-change history. Content, credentials and
attachment bytes are not stored. Webhook targets use a URL hash; bot targets retain channel ID;
fallback targets retain recipient ID. Keep receipts in the reminder private data domain.

## State and retry rules

`BEGIN IMMEDIATE` and the database primary key arbitrate competing processes. A committed token
fences stale claimants. The producer records the target, then commits a send-start marker using
`synchronous=FULL`, before entering `relay.send`. Network I/O is outside the write transaction.

- `pending`: another delivery owns the event. Reentry returns it without another send.
- `sent`: relay explicitly returned `True`; reentry returns the same receipt.
- `failed`: the producer proved transport had not started. An explicit delivery-only retry can
  claim a new token. It cannot invoke business code because no business callback exists here.
- `uncertain`: transport may have sent, including partial chunks, an exception, a false/empty
  verdict, or an expired claim with a send-start marker. Never automatically resend.

After a process crash the next `deliver` reconciles an expired pending claim. A crash before the
marker becomes failed; after the marker it becomes uncertain even if no request actually left.
A late acknowledgement can resolve uncertainty for the same token, but cannot overwrite a newer
claim. There is no network exactly-once guarantee and no uncertainty-reset API. An operator must
reconcile remote evidence before deciding on a separately identified follow-up event.

The registry snapshot used for preparation is the snapshot used for sending. An explicit fallback
records a target change even on the first attempt. A changed resolved target on retry is blocked
and recorded unless explicitly allowed. Changed redacted text or attachment bytes on retry are
also blocked. Callers must keep attachment artifacts immutable until delivery/reconciliation;
the relay opens their paths during transmission.

## Shared dependencies and compatibility

Transport stays `relay.send`; existing relay chunking and bot/webhook/DM implementations are
reused. No copied chunker or redaction rules are shipped here. If a consumer needs its maintained
redactor, import and pass that callable, or redact through its existing owner policy before the
CLI call. Business-specific DLP, title limits, conditions and escalation remain with that owner.

Set `SCHEDULE_NOTIFICATION_LANGUAGE_POLICY` to the installed maintained policy artifact. Until
CONFIG owns a relocation, the legacy production default remains the installed
`.claude/scripts/notification_language.py`. Missing policy always fails. Test fixtures use an
explicit maintained CONFIG source (environment path or the sibling CONFIG checkout), copy those
bytes only into temporary fixture directories, and retain missing-policy negative controls.

`relay.relay`, `relay.send` and `relay.digest` retain their transport contracts. `notify_event`
is the receipt shell. `notify(text, run_id=...)` is now a boolean shell over that same producer;
it accepts TASK_RUN_ID/SCHEDULE_RUN_ID from its orchestration owner when run_id is omitted.
Missing identity is a visible refusal, not a one-shot send. Its explicit command/path overrides
and standalone Big Brother target remain intact. Only a sent receipt returns True.

## Consumer API and explicit command targets

`notification_client.py` is installed beside this producer. Consumers bind it through
SCHEDULE_NOTIFICATION_CLIENT or the existing installed reminder scripts directory. Root owns
that artifact/private task binding. `submit(owner, run_id, phase, condition, stream, content,
**options)` forwards to the producer and never initializes a database. Refusals and dry runs
are explicitly unpersisted; a dry run creates no receipt and sends nothing.

The optional `command` delivery policy preserves standalone notifiers. It contains an argv list,
payload mode (`text`, `base64`, or `at-file`), a finite timeout and optional cwd. `at-file` also
requires `message_file`; its UTF-8 contents must match the prepared text before sending. This
mode preserves the existing backup DM adapter. argv is executed through `llmcall.process`,
without shell string construction; no model is dispatched. It uses the same claim/send-start/
sent/failed/uncertain lifecycle. Command targets never fall back to a stream.

The command target fingerprint covers resolved argv, cwd, payload mode and message-file path.
It identifies an explicit adapter policy; it cannot attest a destination hidden in arbitrary
third-party script configuration. Files attached through the normal `files` option are supported
by the existing bot transport. A custom notifier with such attachments fails visibly with
`custom_notifier_attachments_unsupported`; it is never silently rerouted. Text and base64 command
adapters reuse the canonical chunker. `at-file` preserves the owner's existing file transport.

Hotspots no longer hard-cuts oversized lines or maintains a second generic chunker. It sends the
scrubbed complete body once to the producer; canonical `(n/m)` chunks retain the tail and stay
within Discord's limit. The consumer integration fixture documents this presentation change.

Backup `ReceiptRelay` replaces `LegacyRelay` (the latter name remains an import alias). The outer
pipeline stores references/results and does not create or reconcile delivery claims. Its existing
event keys remain aliases for callback compatibility; each real result carries the canonical
receipt event_id. `Pipeline.retry_notification(run_id, event_key)` retries through the producer
and never calls business steps. Legacy injected event callbacks retain their `(context,event)`
shape and their uncertain-on-exception verdict, explicitly marked unpersisted.

## Consumer integration differences (other lanes)

| Consumer | Existing behavior to preserve | Required integration |
| --- | --- | --- |
| Reminder tick / `notify.notify` | `SCHEDULE_RELAY_CMD` override, `reminders` stream, native DM only when relay is absent; store owns current reminder idempotency | T14 owner supplies a stable occurrence event before replacing this path. This producer does not edit tick/store business logic. Do not add another alarm sender. |
| CONFIG model-map wrappers | `model-mapping` and `gateway-model-mapping`, Chinese rationale with verbatim model IDs, boolean verdict, missing-relay diagnostics, UTF-8/base64 shell handling | Keep each wrapper's stream and message builder. Replace the send call with JSON-stdin receipt delivery after the map operation finishes. Map only `sent` to true; retain failed/uncertain receipt evidence separately from business success. |
| `daily-hotspots/push_card.py` | `hotspots`, `rd.scrub_egress`, evidence URLs/handles, owner alert policy, dry-run preview, tuple/embedded-card result | Pass the maintained scrubber; send one complete event to the canonical relay chunker. Existing local hard-cut logic and Chinese suffix markers differ from relay's lossless `(n/m)` markers: remove them only with a consumer fixture documenting that deliberate presentation change. Dry-run must not create a sent receipt. |
| `demand-mining/push_card.py` | `demand`, pre-send `has_pii` / card DLP checks, embedded-card validation, configurable command/standalone notifier, tuple result | Keep owner DLP and message rendering; receipt delivery receives the approved full text. Preserve override adapters explicitly. Do not silently replace an unavailable custom notifier with the default stream. |
| `email-monitor/em_alert.py` | `mail`, `redact_push` 60-character gist limit / `redact_subject` fallback, priority and account title, standalone notifier, raises on delivery failure | Keep the maintained title/DLP policy and severity selection; map non-sent receipts to visible delivery failure. Do not apply a general scrubber that discards the approved gist. |
| `promotion-assistant/alert.py` | `promotion`, exception/escalation selection, standalone notifier, `sent/error/no-relay/dry-run` result | Preserve the anomaly owner and escalation conditions. Add receipt evidence to the existing envelope; only sent maps to sent. Marketing sends in `providers.py` are a separate business action, outside this notification migration. |
| `llmcall` notify compatibility | Optional total-chain-failure hook; missing relay/exception currently swallowed | Owner workflows must omit that business-terminal hook and emit once from their final outcome. Provider-attempt telemetry remains telemetry. The llmcall lane must adapt or deprecate its old notify hook with explicit outcome visibility; this producer does not call any model or provider. |

Standalone custom notifiers are not equivalent to an Agent Center stream. Preserve their explicit
policy in the owning lane; an unavailable receipt transport must be reported instead of covertly
reverting to a different destination. After migrating each owner, disable its duplicate old send
path in the same change. No sibling repository was edited for this producer.
