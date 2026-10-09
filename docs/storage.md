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

`private_data.py` selects the initialized PRIVATE companion through
`SCHEDULE_REMINDER_CONFIG`, `AGENT_CENTER_CONFIG` or its configured discovery.
Without an explicit config pointer, the pinned Guards resolver selects an initialized
sibling companion before a home companion. DATA overrides do not select the config
root. Missing storage remains inert for reads; missing Guards or an unproven inferred
companion fails explicitly, and every write still requires PRIVATE proof. One process
reuses a successful proof of an unchanged companion for at most 60 s; any change to what the
proof depends on, or a refusal, proves in full again.
Current defaults use root-level `state/`, `agent-runs/` and `digest.json`; the sole
reminder database is `data/db.sqlite3`. The contract also declares known leaves for
retained `data/state/`, `data/agent-runs/` and `data/digest.json` selections. Explicit
overrides need their own reviewed path families and PRIVATE write proof. An entry
does not create a directory or change the installed consumer's selected path. See
[deployment](../skills/schedule-reminder/reference/deployment.md) for initialization
and SQLite-aware backup requirements.

State ownership covers cursor files, inbox projections, reaction identities/baselines
and durable inbound/dispatch JSON records. One inbox pattern covers text, reaction
and migration projections without overlapping owners. Preserve replay identities
and uncertain actions with their matching task-store receipts. Diagnostics, atomic
staging and locks have separate conditional retention; an old timestamp or a newer
cursor proves neither writer inactivity nor successful delivery.
Ingest staging follows the root inbox state directory; durable dispatch retry records
retain their distinct default under `data/state/dispatch/`.

Run ownership names request/event files and the known approach/round leaves in both
current attempt and verified earlier layouts. Keep active or unresolved work and
evidence required to interpret its matching generation and attempt. Worker logs
are diagnostics with a conditional hold during unresolved recovery. Once completed
rounds no longer support selected results or recovery, preserve the useful result
once and review the remaining development records for retirement. Model answers,
verification text and a directory's existence do not independently prove completion.

The installed action workspace is `data/todo-actions/`. Its producer creates a
per-work-item directory and permits task-specific output formats. That does not
establish a stable contract for every descendant. A requested final output needs a
manifest linked to its owner action/work-item identity, selected outputs and hashes,
and any required pending state. A hash manifest without that linkage is insufficient.
Until those facts are reviewed, workspace files remain protected inventory gaps;
the contract does not classify arbitrary cloned source, scripts or logs as core.

The diagnostic families name fifteen selected report, reproducer, evidence and
delivery-manifest leaves under `data/todo-actions/*/`. The work-item segment is
structural; owner identities and selected byte hashes remain in PRIVATE manifests.
These paths identify eligible formats, not automatic core selection of every
same-named output. Keep selected evidence with its owner-action linkage while
work, review or recovery is unresolved. A delivery receipt proves provenance,
not completion or current runtime success; missing workspace references still
require reconciliation.

Diagnostic `repository/**` copies and the named `schedule-reminder.bundle` have a
conditional `retired` hold. Review useful source differences, selected refs/results
and recovery dependencies before any separately authorized retirement. Copied
source does not replace current source authority or restore superseded caller
policy overrides. Diagnostic `validation/**` is rebuildable synthetic evidence;
preserve selected results and confirm writer inactivity before retirement. No
declaration creates a workspace, runs an action or authorizes deletion.

The single-level action directories themselves remain gaps: the matcher checks
paths and cannot prove directory type or owner linkage. Files outside the named
families also remain gaps. State files without a confirmed producer retain their
gap and recovery hold; metadata coverage cannot establish a live writer binding.

The historical `data/task-console/console.sqlite3*` set belongs to the console
schema; the current console binding selects its separate private companion. The
`data/todo-action-upgrade/` namespace holds owner-database upgrade, isolated acceptance,
registration and launcher staging evidence. Both are `retired` with a conditional
hold, and accept no new development writes. Extract useful unique code, required
DATA, selected conclusions and rollback evidence, then reconcile incomplete
registrations, unique DB history, consumers, processes and SQLite state before a
separately authorized retirement. Historical status does not prove that these
requirements have passed. The retained original reminder import remains a separate
recovery dependency and is never another live scheduler.

`DIRECTORY-LAYOUT.md` is an exact current recovery reference. The historical private
`ARCHITECTURE.md`, `PUSH_ARCHITECTURE.md` and `REMINDER_REDESIGN.md` documents have a
conditional `retired` governance hold. Compare required private operating/recovery
decisions with current owner references before separately authorized retirement;
do not publish private topology. Other unreviewed design documents and state files
without a confirmed producer remain gaps. `retired/` is a
temporary reviewed namespace pending removal. The aggregate review threshold is 64 MiB,
excluding Git administration. A breached threshold remains a failed capacity check;
required state and recovery evidence remain protected. Budgets never authorize
deletion or make a development archive a required output.

Use the shared storage checker from the canonical skill-smith source checkout:

```text
python skills/skill-smith/scripts/storage_contract.py validate --repo <source-checkout>
python skills/skill-smith/scripts/storage_contract.py check --repo <source-checkout> --companion <private-repository-root> --json
```

The explicit companion path must be the exact PRIVATE Git worktree root, including
configuration and recovery files. Checking only its `data/` child is invalid. Folder
names do not establish visibility. The checker proves PRIVATE admission and validates
metadata coverage and sizes; domain scripts validate record contents. Undeclared,
ambiguous or unobserved requirements remain failed checks after schema validation.
Moving data into PRIVATE storage changes its location and does not reclaim space.
