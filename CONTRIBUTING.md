# Contributing to schedule-reminder

Downstream skills depend on this infrastructure base's state and CLI contracts. Read
[`PHILOSOPHY.md`](PHILOSOPHY.md) before changing those interfaces.

## Golden rules

1. **The contract is frozen.** Verbs, item fields, and the state enum are fixed. You
   may add fields/verbs (additive); deleting/renaming/changing semantics requires bumping
   `api_version` (in `store.py`) and a dual-run transition period.
2. **Never break backward compatibility.** Schema changes are additive only (`PRAGMA user_version`
   migrations); unknown fields stay MUST-PRESERVE.
3. **Keep runtime DATA and credentials out of public source.** Use the initialized PRIVATE
   companion and source storage contract. Ignore rules are an additional safeguard, not a storage
   boundary. Read configured credentials only for their intended operation; never log or echo them.

## Conventions

- Stdlib-first; optional deps (`dateutil`, `pysqlite3`) degrade gracefully.
- All time is UTC RFC3339 with microsecond precision.
- Writes are short `BEGIN IMMEDIATE` transactions; never hold the write lock across a network/push.

## Version sync

Update `.claude-plugin/plugin.json.version` for a release. Keep any explicit current-version claims
in README and README_CN consistent with that release. Roadmap badges and ROADMAP.md describe
planned work; CHANGELOG.md preserves historical entries. Do not rewrite history or add a release
claim before that release is made.

The optional automatic completion path requires execution and cleanup evidence from the shared
model interface; missing evidence leaves work in `reconcile`. Completion is the executor's own
verification command, run by the system, or the executor's answer when it gives no executable
check; no second model reviews it. Tests stub every model call with generated typed results and
separately cover the current interface's missing metadata.

License: MIT (see [LICENSE](LICENSE)).
