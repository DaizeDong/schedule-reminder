# schedule-reminder, Integration guide for downstream skills

> For #2 email-monitor, #4 daily-hotspots, #6 demand-mining, #7 promotion-assistant, and any future
> skill that needs to persist a reminder or read task progress. Depend on the **CLI contract only**
> (`reference/contract.md`), never the DB.

## The golden rules

1. Call `python <path>/reminder.py <verb> --json ...` via subprocess; parse stdout JSON.
2. Always pass `--source <your-skill>` and `--actor <your-skill>` (audit + filtering).
3. Always pass `--idempotency-key <your-skill>:<your-record-id>` on writes, makes retries safe.
4. Put your own extra data in `--ext` under an `x_<yourskill>_*` namespace; the base preserves it.
5. Read progress with `get`/`list`/`query`; never assume internal storage.

## Example, email-monitor files a deadline reminder

```bash
python reminder.py add \
  --title "Reply to recruiter (Acme Corp)" \
  --kind task --due-at "2026-06-28T17:00:00Z" --priority 1 \
  --source email-monitor --actor email-monitor \
  --idempotency-key "email-monitor:msg-8841" \
  --ext '{"x_email_monitor_uid":"8841","x_email_monitor_thread":"t-1207"}'
```

Re-running the exact command (e.g. the watcher re-scans the same message) returns the **same item
id**, no duplicate reminder.

## Example, promotion-assistant reads what is still open

```bash
python reminder.py list --source promotion-assistant --active --limit 100
# -> {"items":[...], "next_cursor": "..."}  (page with --cursor)
```

## Example, daily-hotspots advances progress, then completes

```bash
python reminder.py transition --id "$ID" --to doing --progress 40 --actor daily-hotspots
python reminder.py done --id "$ID" --actor daily-hotspots
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
import json, subprocess, sys

def call(*args, db=None):
    env = dict(os.environ)
    if db: env["SCHEDULE_DB_PATH"] = db
    r = subprocess.run([sys.executable, REMINDER, *args],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    if r.returncode != 0:
        raise RuntimeError(json.loads(r.stderr)["error_code"])
    return json.loads(r.stdout)

item = call("add", "--title", "X", "--source", "my-skill",
            "--idempotency-key", "my-skill:42")["item"]
```

## Versioning promise

The verb set, item field set, and state enum are golden-tested (E11). Within `api_version 1.x` you
get **only additive** changes. A breaking change bumps `api_version` and ships a dual-run period ,
pin to the `api_version` in the envelope if you need to be strict.
