# schedule-reminder, Integration guide for downstream skills

> For #2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant, and any future
> skill that needs to persist a reminder or read task progress. Use the CLI contract or the
> separately documented read-only Task Console linkage seam, never database internals.

## The golden rules

1. Call `python <path>/reminder.py [--actor NAME] <verb> ...` via subprocess; parse stdout JSON.
2. Put global `--actor <your-skill>` before the verb. Use `--source <your-skill>` on add/list/query for filtering.
3. Bind creation to a stable source occurrence with `--idempotency-key <your-skill>:<your-record-id>`.
   Reuse the same identity and payload for a retry; a new intentional occurrence gets a new identity.
4. Put your own extra data in `--ext` under an `x_<yourskill>_*` namespace; the base preserves it.
5. Read progress with `get`/`list`/`query`; never assume internal storage.

## Example, email-monitor files a deadline reminder

Before creating a todo, compare existing active obligations across sources with
`creation-preflight`, including the source and occurrence date. Use `ensure` for transactional
create/reuse. Similar wording alone does not authorize merging separate obligations. See below
for reviewed follow-ups; use `add` when the integration intentionally owns a keyed upsert.

```bash
python reminder.py --actor email-monitor add \
  --title "Reply to recruiter (Acme Corp)" \
  --kind task --due-at "2026-06-28T17:00:00Z" --priority 1 \
  --source email-monitor \
  --idempotency-key "email-monitor:msg-8841" \
  --ext '{"x_email_monitor_uid":"8841","x_email_monitor_thread":"t-1207"}'
```

Re-running the exact command returns the same item ID. By default, `add` updates the keyed
record's supplied content; use `--if-exists return` when a replay must preserve it.

## Reuse an obligation or append a follow-up

Call `creation-preflight` with the proposed `--title`, `--source`, `--idempotency-key` and all known
occurrence/content fields. It is read-only and returns candidate items with revisions. `ensure`
accepts the same fields, repeats the comparison under the owner transaction and creates or reuses
an equivalent item. A `review` decision requires deciding whether the obligation and occurrence
are actually the same.

For a follow-up, use `ensure --reuse-id <reviewed ID> --expected-revision <revision> --note <new text>`
with a new source request identity. The note is appended once and earlier content is preserved.
For a different obligation, provide `--distinct-reason` with the reviewed reason. A stale revision
or changed request payload is rejected; refresh and review it instead of bypassing the check.
Do not reuse a legacy add key to attach new follow-up options.

Inbound dispatch keeps the original channel/message identity, authorized IDs, first plan and
outcomes. An explicit `dispatch.py` caller supplies `--request-id`; unidentified calls retain the
legacy immediate path. `--no-post` only suppresses confirmation messages and still performs actions.
New saved plans bind canonical action keys before execution. Older pending plans with unprovable
action identities require reconciliation of their original effects before replay.
See [dispatch identity](../../../docs/dispatch-identity.md).

## Example, promotion-assistant reads what is still open

```bash
python reminder.py list --source promotion-assistant --active --limit 100
# -> {"items":[...], "next_cursor": "..."}  (page with --cursor)
```

## Example, daily-hotspots advances progress, then completes

```bash
python reminder.py --actor daily-hotspots transition --id "$ID" --to doing --progress 40
python reminder.py --actor daily-hotspots done --id "$ID"
```

## Example, cross-skill dependency (block until a prerequisite is done)

```bash
python reminder.py block --id "$CHILD" --blocker-id "$PARENT" --reason "waiting on data pull"
# the base will refuse to `done` $CHILD until $PARENT is done (ERR_DEPENDENCY_UNMET)
```

## Handling errors

Exit 1 emits structured JSON on stderr with `error_code`. Usage errors exit 2. Handle at least:

- `ERR_STATE_CONFLICT` / `ERR_CONFLICT`, someone changed the item or request; read and review again
  before deciding whether a new operation is authorized.
- `ERR_CREATION_REVIEW`, compare returned candidates and explicitly reuse or distinguish the occurrence.
- `ERR_ILLEGAL_TRANSITION`, your state move is not allowed; read `allowed[]`.
- `ERR_DEPENDENCY_UNMET`, finish the `unmet[]` items first.
- `ERR_BUSY`, transient; retry with back-off (rare; the base already retries internally).
- `ERR_DATA_POLICY`, the selected output is not governed by a proven PRIVATE repository.
- `ERR_UNINITIALIZED`, explicitly initialize the PRIVATE database before writing.
- `ERR_PERMISSION`, storage access was denied; this is distinct from DATA policy refusal.

## Python helper pattern

```python
import json, os, subprocess, sys

def call(*args, db=None):
    env = dict(os.environ)
    if db: env["SCHEDULE_DB_PATH"] = db
    r = subprocess.run([sys.executable, REMINDER, *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(json.loads(r.stderr)["error_code"])
    return json.loads(r.stdout)

item = call("--actor", "my-skill", "add", "--title", "X", "--source", "my-skill",
            "--idempotency-key", "my-skill:42", "--if-exists", "return")["item"]
```

## Work and notification consumers

Task Console reads `work-feed`, then submits its owner-issued action/revision through `work-action`
using JSON stdin. Manual completion needs no executor and preserves dependency guards. Linked task
execution first requires explicit [linkage review](linkage-review.md); task IDs are compiled from
declarations and private bindings, never guessed from titles. A Scheduler handoff receipt leaves
the todo's business state unchanged. Work-feed is persisted observation, not a liveness probe.

For notifications tied to a durable business event, use the [notification receipt contract](notification-receipts.md).
Keep its event identity stable across retries and preserve uncertain outcomes for reconciliation.
This contract is separate from worker readiness and from legacy due-reminder transport retries.

## Versioning promise

The verb set, item field set, and state enum are golden-tested (E11). Within `api_version 1.x` you
get **only additive** changes. A breaking change bumps `api_version` and ships a dual-run period,
pin to the `api_version` in the envelope if you need to be strict.
