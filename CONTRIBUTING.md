# Contributing to schedule-reminder

This is a **T0 infrastructure base**, four downstream skills depend on its contract. Stability beats
features. Before changing anything, read [`PHILOSOPHY.md`](PHILOSOPHY.md).

## Golden rules

1. **The contract is frozen.** Verbs, item fields, and the state enum are golden-tested (E11). You
   may add fields/verbs (additive); deleting/renaming/changing semantics requires bumping
   `api_version` (in `store.py`) and a dual-run transition period.
2. **Never break backward compatibility.** Schema changes are additive only (`PRAGMA user_version`
   migrations); unknown fields stay MUST-PRESERVE (E12).
3. **Evaluation-driven.** Write/extend the E1-E15 assertions in `skills/schedule-reminder/tests/`
   before the implementation. The suite must stay green; E8/E9/E11/E12 are merge-blocking.
4. **Secrets never enter the repo.** The DB, `config.json`, and any token are `.gitignore`d. The skill
   must never read, log, or echo a token.

## Run the suite

Install the shared packages from the approved source revisions or wheel set before resolving
`requirements.txt`. The linkage tests also use the sibling task-console checkout's generated
fixtures. Set `SCHEDULE_NOTIFICATION_LANGUAGE_POLICY` to the maintained notification language
module. Missing test dependencies are errors, not skipped tests.

```bash
python -m pip install -r requirements.txt pytest
python -m pytest skills/schedule-reminder/tests/ -q
```

CI records the compatible dependency revisions in `.github/workflows/tests.yml`. Its private
checkouts use separate read-only deploy keys. The notification policy checkout is sparse and
contains only that module. Fork workflows without those keys fail with a dependency-access error;
they do not receive credentials or run through `pull_request_target`.

## Conventions

- Stdlib-first; optional deps (`dateutil`, `pysqlite3`) degrade gracefully.
- All time is UTC RFC3339 with microsecond precision.
- Writes are short `BEGIN IMMEDIATE` transactions; never hold the write lock across a network/push.

## Version sync

`plugin.json.version` == README/README_CN Roadmap badge == `ROADMAP.md` "Current:" ==
`CHANGELOG.md` latest entry. Keep all four in lock-step on every bump.

License: MIT (see [LICENSE](LICENSE)).
