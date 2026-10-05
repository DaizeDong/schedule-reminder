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

Test source roots are derived from the test files. Synthetic filesystem fixtures create and
clean up their own temporary workspaces; no author-specific workspace setting is required.

## Conventions

- Stdlib-first; optional deps (`dateutil`, `pysqlite3`) degrade gracefully.
- All time is UTC RFC3339 with microsecond precision.
- Writes are short `BEGIN IMMEDIATE` transactions; never hold the write lock across a network/push.

## Version sync

Update `.claude-plugin/plugin.json.version` for a release. Keep any explicit current-version claims
in README and README_CN consistent with that release. Roadmap badges and ROADMAP.md describe
planned work; CHANGELOG.md preserves historical entries. Do not rewrite history or add a release
claim before that release is made.

License: MIT (see [LICENSE](LICENSE)).


## Canonical test dependency

The route tests execute the canonical code-only notification language rule. Prepare the exact artifact using the hash and size in tools/test_dependencies.json:

    python tools/prepare_test_dependency.py --source /path/to/notification_language.py --out /tmp/schedule-tests/notification_language.py
    export SCHEDULE_TEST_LANGUAGE_RULE=/tmp/schedule-tests/notification_language.py

CI checks out the pinned code-only file, validates its hash and size, and supplies its path through
`SCHEDULE_TEST_LANGUAGE_RULE`. Missing read access or a hash mismatch fails before test collection.
Fork CI needs the dependency provisioned by a trusted runner; it must not skip language assertions
or reduce the acceptance floor. The suite runs on Linux and Windows. Local consumer integration
also accepts `SCHEDULE_TEST_TASK_CONSOLE_ROOT` pointing to a Task Console checkout; those checks
compile synthetic declarations and invoke the owner API without registering or running tasks.

The optional automatic completion path requires execution, cleanup, and effective model-family
evidence from the shared model interface. Missing evidence leaves work in `reconcile` or
`review_unavailable`; a model's text alone cannot establish completion. Tests cover full behavior
with generated typed results and separately cover the current interface's missing metadata.
