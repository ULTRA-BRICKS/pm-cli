# pm-cli

`pm` — the autonomous project-manager coordinator over the task/tg/rig ecosystem.

pm is a coordinator and state machine, not a ticket backend, provisioning engine, Telegram
daemon, or implementation worker. It projects evidence from the surrounding tools (task-cli,
tg-cli, rig-cli, CI, ship audit, watchdog) into a durable, event-sourced work queue and reports
on it. The full design is agent-tools **research #255**.

**Release 1 is an "organ of sight"**: it observes and reconciles; it does not autonomously
dispatch or edit code.

## The invariant

The work queue is a **deterministic projection of an append-only JSONL event log**. State is
never mutated in place — every change is a new immutable event — so a crash/restart replays the
log to the exact same projection.

## Commands (Milestone 1)

```
pm record intake --id <id> --title <t> [--project <p>] [--privacy local-only|remote-allowed]
pm record transition <id> <state> [--force]   # validated against the FSM + evidence gates
pm record evidence <id> <kind> <uri>
pm record note <id> <text>
pm ingest --source task [--repo-dir <dir>] [--from-file <f>] [--dry-run] [--json]
pm ingest --source telegram [--since <3d>] [--from-file <f>] [--dry-run] [--json]
pm dlq [--json] [--status unprocessed|processed|deadletter]
pm link <from> <type> <target> [--reason <r>]   # blocks|waits_for|duplicates|supersedes|relates_to
pm timer <id> --at <iso> [--kind <k>] [--reason <r>]
pm reminders [--json]                            # blocked/unblocked, stale, duplicate, cycle, timer
pm queue [--json] [--category <c>] [--state <s>]
pm why <id>                                    # current state, legal next moves, missing evidence
pm doctor                                      # store writability + FSM integrity
pm version
```

`pm ingest --source task` reads your task-cli tickets (via `task list --all --json`, run in the
`--repo-dir` directory), projects each into a deduped WorkItem (`task:<id>`), and records its url as
`task-record` evidence. Re-running is a no-op for already-tracked tickets. Each item's pm richness
is *rendered* as `pm.stage` / `pm.blocked` / `pm.health` labels in `pm queue` / `pm why` — a
read-only projection; ingest does NOT write labels back to task-cli, and task-cli's own five states
are left untouched (writing labels onto tickets is a later, delegated step).

`pm ingest --source telegram` reads inbound Telegram messages (via `tg replies user --json`) and
runs them through the intake **dead-letter queue**: each message is captured as an
`intake_captured` (status `unprocessed`) the instant pm sees it — so it can never be lost — then
processed into a WorkItem. Processing is retried a bounded number of times — each `pm ingest` run
is one tick that advances a failing intake by one attempt — and on exhaustion the intake moves to
`intake_deadletter` with the failure reason, durably in the log and visible via `pm dlq`. `pm dlq`
lists the ledger (unprocessed / processed / deadletter).

`pm link` records typed dependency edges between work items (`blocks`, `waits_for`, `duplicates`,
`supersedes`, `relates_to`); `pm timer` schedules a reminder/SLA timer. `pm reminders` then derives
the nudges the graph implies — a blocker or waited-for item that has finished (so the blocked item
can proceed), a stale active item past its SLA, a still-open duplicate/superseded item, an expired
timer, and dependency cycles — computed purely from the projection plus the current time.

The event log lives at `$PM_HOME/events.jsonl` (default `$XDG_STATE_HOME/pm` or
`~/.local/state/pm`).

## Develop

```
python -m pytest -q      # hermetic unit suite
ruff check .             # lint
```

Stdlib-only at import time; no third-party runtime dependencies.
