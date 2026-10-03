# Delivery and work recovery

The registry selects inbound channels, outbound streams and deterministic command handlers. Keep it in the initialized PRIVATE companion described in [deployment.md](deployment.md). The [CLI contract](contract.md) defines pool state and reminder operations.

## Inbound commands

A matching handler owns its message even when it fails. The dispatcher never sends that message to a model. Before invoking the handler, the worker stores a durable command attempt. Exit zero completes the attempt; a failure or interrupted outcome remains `command_failed`. Failed commands receive no success acknowledgement and are not automatically replayed.

Inspect the handler's effects and its PRIVATE inbound record before retrying. A handler can change external state before timing out. After resolving that uncertainty, use `ingest_tick.py --retry-command <full-inbound-id>` to rearm the same message. Ordinary polling does not rearm it; completed messages cannot be retried through this command.

Unmatched messages and reactions use the durable dispatcher. Saved plans retain the item and running-work IDs shown to the planner. A retry may mutate only IDs in that original authorization that remain eligible now. A saved wildcard stop is expanded to those work IDs; it cannot include later work. Previously authorized completed items can be reconciled with a read before confirming success. Action identities and success receipts prevent repeated actions when a confirmation needs another attempt.

A newly discovered or registered channel first records a PRIVATE baseline of owner reactions on its recent 50 messages, then advances the text cursor without dispatching old content. The original baseline survives a failed cursor write and later retries. Reaction-only polling uses the same admission step. Failure to fetch reactors, or a full reactor page that cannot establish a complete snapshot, leaves the channel unadmitted. Subsequent distinct owner reaction keys and new text are processed normally. Existing text cursors retain their prior reaction behavior.

## Work completion

Work orders use agent_task.py, agent_run.py and agent_tick.py. Finalization commits execution metadata and pool state in one SQLite transaction through the CLI's additive `--ext` option on `done` and `transition`. A failed transaction leaves the prior running state available to recovery.

The serial lifecycle lock remains required. PID creation-time checks, detached process survival and termination need platform tests. Synthetic state-transition checks do not establish those properties.

Model calls use the installed llmcall interface: `llmcall.call(prompt, mode="agent")` for agent work, and its default judge mode for text decisions. Routing, models, timeouts and fallback policy belong to that interface.

## Reminder delivery

Delivery uses notify.deliver. When a configured relay script is absent, the standalone Big Brother fallback remains available. Its legacy boolean success can acknowledge delivery but supplies no external readiness receipt.

Failed sends use bounded backoff. At the retry limit, delivery stops until explicitly rearmed. `snooze --until <timestamp>` resets delivery retries; moving an idempotent reminder's due time forward also rearms an exhausted delivery. An ordinary task blocked on a dependency remains eligible for reminders.

`tick --dry-run` previews due work without sending, changing items or audit events, or replacing the worker's watchdog timestamp and tick count. The preview requires an initialized database.

## Evidence

[Generated examples](../../../examples.json) contain synthetic records reproduced by tools/make_fixtures.py.

These recovery rules specify the repaired behavior. Earlier code could mark failed handlers completed, persist finalization in separate writes, retry exhausted deliveries and update the watchdog during preview. Execution reports must distinguish focused synthetic checks from full regression, native platform tests, external delivery and installation.
