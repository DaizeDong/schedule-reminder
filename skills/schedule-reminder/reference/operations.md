# Delivery and work recovery

The registry selects inbound channels, outbound streams and deterministic command handlers. Keep it in the initialized PRIVATE companion described in [deployment.md](deployment.md). The [CLI contract](contract.md) defines pool state and reminder operations.

## Inbound commands

A matching handler owns its message even when it fails. The dispatcher never sends that message to a model. Before invoking the handler, the worker stores a durable command attempt. Exit zero completes the attempt; a failure or interrupted outcome remains `command_failed`. Failed commands receive no success acknowledgement and are not automatically replayed.

Inspect the handler's effects and its PRIVATE inbound record before retrying. A handler can change external state before timing out. After resolving that uncertainty, use `ingest_tick.py --retry-command <full-inbound-id>` to rearm the same message. Ordinary polling does not rearm it; completed messages cannot be retried through this command.

Unmatched messages and reactions use the durable dispatcher. Saved plans retain the item and running-work IDs shown to the planner. A retry may mutate only IDs in that original authorization that remain eligible now; it checks just those IDs instead of listing every active item again. A saved wildcard stop is expanded to those work IDs; it cannot include later work. Previously authorized completed items can be reconciled with a read before confirming success. Action identities and success receipts prevent repeated actions when a confirmation needs another attempt. A rejected action (an ID the planner was not shown, a snooze without a time) is final and is reported in the confirmation; it does not keep the message pending. A failed action, an undelivered confirmation or a planner that returned no usable plan is retried on the next tick, at most three attempts; the record then becomes `failed`, and `ingest_tick.py --retry-command <full-inbound-id>` rearms it after you have checked its effects.

A newly discovered or registered channel first records a PRIVATE baseline of owner reactions on its recent 50 messages, then advances the text cursor without dispatching old content. The original baseline survives a failed cursor write and later retries. Each sweep reads a channel once and admits it before its reactions are read. Failure to fetch reactors, or a full reactor page that cannot establish a complete snapshot, leaves the channel unadmitted. Subsequent distinct owner reaction keys and new text are processed normally. Existing text cursors retain their prior reaction behavior.

## Work completion

Work orders use agent_task.py, agent_run.py and agent_tick.py. Finalization commits execution metadata and pool state in one generation-fenced SQLite transaction (`store.advance_work`). A failed transaction leaves the prior running state available to recovery.

The launch/stop lock remains required: it keeps a stop out of the window between a runner's spawn and its PID receipt. PID creation-time checks, detached process survival and Job Object termination need platform tests. Synthetic state-transition checks do not establish those properties.

Model calls use the installed llmcall interface: `llmcall.call(prompt, mode="agent")` for agent work, and its default judge mode for text decisions. Routing, models, timeouts and fallback policy belong to that interface.

## Reminder delivery

Delivery uses notify.deliver, which calls the sibling relay module in the tick's own process (no child process per reminder). A stream without a webhook or channel falls back to the Big Brother DM, whose boolean success acknowledges delivery without a message ID. `SCHEDULE_RELAY_CMD` replaces the relay with a command for tests or a custom notifier.

Failed sends use bounded backoff. At the retry limit, delivery stops until explicitly rearmed. `snooze --until <timestamp>` resets delivery retries; moving an idempotent reminder's due time forward also rearms an exhausted delivery. An ordinary task blocked on a dependency remains eligible for reminders.

`tick --dry-run` previews due work without sending or changing items or audit events. A real tick also writes nothing when nothing is due: it scans on a read-only connection and opens a write connection only to claim a due reminder or close a lapsed one. It keeps no heartbeat row; Task Scheduler records each run. The preview requires an initialized database.

## Evidence

[Generated examples](../../../examples.json) contain synthetic records reproduced by tools/make_fixtures.py.

These recovery rules specify the repaired behavior. Earlier code could mark failed handlers completed, persist finalization in separate writes, retry exhausted deliveries and update the watchdog during preview. Execution reports must distinguish focused synthetic checks from full regression, native platform tests, external delivery and installation.
