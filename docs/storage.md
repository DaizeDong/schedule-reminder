# Source layout and private storage

The public checkout is the deployable tool. Runtime records belong in a separate,
initialized PRIVATE repository. The root [storage.contract.json](../storage.contract.json)
declares their purpose, producer, consumer, retention and restoration rules. Its paths are
relative to the selected private repository root, not to this source checkout.

| Source path | Responsibility |
|---|---|
| `skills/schedule-reminder/SKILL.md` | Invocation and stable public entrypoints |
| `skills/schedule-reminder/scripts/` | Store, CLI, relay, ingest, work execution and installation |
| `skills/schedule-reminder/reference/` | CLI contracts, deployment, integration and operations |
| `skills/schedule-reminder/tests/` | Offline behavioral and regression tests |
| `tools/make_fixtures.py` | Reproducible synthetic test and example inputs |
| `tools/test_dependencies.json` | Pinned external test-code identity |
| `guards/` | Pinned security and private-data boundary checks |
| `style/` | Pinned documentation checks |

`private_data.py` selects `SCHEDULE_REMINDER_CONFIG`, or `AGENT_CENTER_CONFIG`, then
the default configuration root. Current data defaults to its `data` directory;
explicit database, run and digest selections may use other PRIVATE locations.
The contract also describes retained root-level `state`, `agent-runs` and digest
layouts. An entry in the contract does not create a directory or change the installed
consumer's selected path. See [deployment](../skills/schedule-reminder/reference/deployment.md)
for initialization and SQLite-aware backup requirements.

Use the shared storage checker from the canonical skill-smith source checkout:

```text
python skills/skill-smith/scripts/storage_contract.py validate --repo <source-checkout>
python skills/skill-smith/scripts/storage_contract.py check --repo <source-checkout> --companion <private-repository-root> --json
```

The explicit companion path must be the PRIVATE Git worktree root. Its folder name
does not establish visibility or governance. The checker validates declared path
coverage and sizes; domain scripts validate record contents. Externally managed
integration artifacts and old design documents that have not been classified remain
inventory gaps until their producers, consumers and recovery dependencies are reviewed.
Do not add a broad retention rule merely to turn an unknown inventory green.

Keep current tasks, cursors, action identities, required configuration and recovery
evidence. Extract useful code and selected results from completed development groups,
then retire their redundant workspaces. `retired/` is temporary pending removal, not
an archive. A size threshold requires review and never authorizes deletion. Moving
an artifact into PRIVATE storage changes its location but does not reclaim space.
