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

```bash
python -m pytest skills/schedule-reminder/tests/ -q
```

## Conventions

- Stdlib-first; optional deps (`dateutil`, `pysqlite3`) degrade gracefully.
- All time is UTC RFC3339 with microsecond precision.
- Writes are short `BEGIN IMMEDIATE` transactions; never hold the write lock across a network/push.

## Version sync

`plugin.json.version` == README/README_CN Roadmap badge == `ROADMAP.md` "Current:" ==
`CHANGELOG.md` latest entry. Keep all four in lock-step on every bump.

License: MIT (see [LICENSE](LICENSE)).
