# Manual completion in the work feed

Active human todos expose a `complete` offer labeled 标记完成. The console submits
the owner-issued revision and a durable request ID through `work-action`. No Agent
workspace, conversation context or scheduler is needed.

Completion applies the same state machine and dependency checks as `done --id`.
The owner saves the action receipt and transition to `done` in one transaction,
sets progress to 100 and records the end time. Replaying the same request returns
its receipt without creating work, sending a notification or repeating the transition.
A different request from a stale page is rejected.

While agent execution or an unconfirmed task handoff is active, completion is disabled.
First stop or finish that work. A confirmed Scheduler handoff releases only the request
reservation; Task Console owns the task's runtime and stop controls, and the todo remains open.
Signal records and terminal todos have no completion offer. A dependency failure
leaves the todo and receipt store unchanged. Completed todos retain their original
content and remain available in history.
