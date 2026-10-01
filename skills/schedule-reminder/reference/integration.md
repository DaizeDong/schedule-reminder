# schedule-reminder, Integration guide for downstream skills

> For #2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant, and any future
> skill that needs to persist a reminder or read task progress. Depend on the **CLI contract only**
> (`reference/contract.md`), never the DB.

## The golden rules

1. Call `python <path>/reminder.py <verb> ...` via subprocess; responses are always JSON.
2. Always pass `--source <your-skill>` and `--actor <your-skill>` (audit + filtering).
3. Always pass `--idempotency-key <your-skill>:<your-record-id>` on writes, makes retries safe.
4. Put your own extra data in `--ext` under an `x_<yourskill>_*` namespace; the base preserves it.
5. Read progress with `get`/`list`/`query`; never assume internal storage.

## Creation and semantic follow-ups

Use `creation-preflight` with the proposed fields before creating an obligation. It reads across
sources and returns `decision`, candidate `item` content and `revision`. Similarity only selects
candidates; it never authorizes a merge. Compare the actual obligation, target, dates and alarms.

Use `ensure` with the same fields, source and stable idempotency key. Its check and write share
one transaction. An equivalent item returns `decision=reused`; a new one returns `created`.
Retries return `replayed`, including when the original was edited or completed afterward.

For a confirmed semantic follow-up, supply the candidate ID as `--reuse-id`, its revision as
`--expected-revision`, and the new instructions as `--note`. This appends once and preserves the
original description and identifiers. A stale revision refuses the write; repeat the read.
The occurrence/scope must match. Rescheduling uses the existing `snooze`/`update` operations.

`ERR_CREATION_REVIEW` means a possible duplicate needs a content decision. Choose reuse, report
that the dated item is already finished, or use `--distinct-reason` explaining the different
obligation. Do not generate another key, fall back to `add`, or assume a failed read means an
empty pool. The reason is part of the immutable request; it is not permission to revive a task.

`add` retains its historical UPSERT contract for producer-owned state and health heartbeats.
Those periodic status updates must not be converted into new human obligations by `ensure`.
Agent work deduplication uses the full request, workspace and linked origin while active;
it does not compare truncated display titles or modify a running request.

## Example, email-monitor files a deadline reminder

```bash
python reminder.py --actor email-monitor ensure \
  --title "Reply to recruiter (Acme Corp)" \
  --kind task --due-at "2026-06-28T17:00:00Z" --priority 1 \
  --source email-monitor \
  --idempotency-key "email-monitor:msg-8841" \
  --ext '{"x_email_monitor_uid":"8841","x_email_monitor_thread":"t-1207"}'
```

Preflight the same fields first. Re-running the exact ensure command returns the **same item id**
and preserves subsequent edits. Place global `--actor` before the verb when invoking the CLI.

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

Non-zero exit = structured JSON on stderr with `error_code`. Handle at least:

- `ERR_STATE_CONFLICT`, someone changed the item under you; re-`get` and retry.
- `ERR_ILLEGAL_TRANSITION`, your state move is not allowed; read `allowed[]`.
- `ERR_DEPENDENCY_UNMET`, finish the `unmet[]` items first.
- `ERR_BUSY`, transient; retry with back-off (rare; the base already retries internally).

## Python helper pattern

```python
import json, os, subprocess, sys

def call(*args, db=None):
    env = dict(os.environ)
    if db: env["SCHEDULE_DB_PATH"] = db
    r = subprocess.run([sys.executable, "-B", REMINDER, *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(json.loads(r.stderr)["error_code"])
    return json.loads(r.stdout)

proposal = ["--title", "X", "--source", "my-skill", "--idempotency-key", "my-skill:42"]
review = call("creation-preflight", *proposal)
# Inspect review["matches"] before writing; unresolved candidates require a content decision.
item = call("ensure", "--title", "X", "--source", "my-skill",
            "--idempotency-key", "my-skill:42")["item"]
```

## Versioning promise

The verb set, item field set, and state enum are golden-tested (E11). Within `api_version 1.x` you
get **only additive** changes. A breaking change bumps `api_version` and ships a dual-run period,
pin to the `api_version` in the envelope if you need to be strict.
