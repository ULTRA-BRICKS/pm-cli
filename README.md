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
pm reminders [--json]                            # blocked/unblocked, stale, duplicate, cycle, timer, sprint
pm reconcile --source pr [--from-file <f>] [--dry-run] [--json]
pm reconcile --source rig [--repo-dir <dir>] [--from-file <f>] [--json]
pm executors [--from-file <f>] [--json]          # the executor ledger (harness/model/quota)
pm route [<id>] [--risk low|normal|high] [--from-file <f>] [--json]   # read-only recommendation
pm report queue|stuck|retro|monthly|metrics|aging [--month YYYY-MM] [--json]
pm queue [--json] [--category <c>] [--state <s>]
pm why <id>                                    # current state, legal next moves, missing evidence
pm sprint create --name <n> [--goal <g>]       # -> prints a fresh sprint id
pm sprint add <sprint_id> <item_id> [--estimate <n>]   # pull a work item in + estimate it
pm sprint start <sprint_id>                    # commit scope; freezes further `add`
pm sprint status <sprint_id> [--json]          # progress, joined live against work-item states
pm sprint close <sprint_id>
pm sprint retro <sprint_id> [--note <text>] [--json]   # record a note, or view with no --note
pm sprint list [--json]
pm doctor                                      # store writability + FSM integrity
pm version
```

## Sprints (Milestone 4)

A sprint is a lightweight, time-boxed grouping of work items — event-sourced exactly like
everything else here (no parallel mutable store): `create` names it, `add` pulls a work item in
with an estimate (repeatable — this is "plan"), `start` commits the scope and freezes further
`add`s, `status`/`retro` are read-only views joined LIVE against the real work-item projection (so
a sprint's notion of "done" can never drift from an item's own FSM state), and `close` + `retro
--note` close the loop.

"Autonomous" here respects the same Release-1 boundary as the rest of pm (it observes and
reconciles, it does not dispatch): `pm reminders` — the same surface the `pm-reminders-push` cron
already pushes to Telegram — now also nudges on sprint lifecycle events with no new dispatch
machinery: `sprint-stale-planning` (still `planning`, untouched — no `add`/`start` — past the
threshold), `sprint-ready-to-close` (every item
terminal, but the sprint itself hasn't been closed), and `sprint-needs-retro` (closed with no retro
notes yet — the part of the loop most likely to get skipped).

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

`pm reconcile --source pr` reads each item's PR state and CI conclusion (via `gh`) and advances it
along the CI window — `pr-open → ci-pending → ship-ready` on green CI, `ci-pending → fixing` on red
— attaching `pr`/`ci` evidence idempotently, using only facts GitHub reports (which the PM agent
cannot forge). It **never advances past `ship-ready`**: a merged PR is recorded as evidence, but
moving to `deployed`/`done` requires a per-project deploy probe and a decision on trusting the ship
audit — deliberately out of scope here. `pm reconcile --source rig` runs `rig status` and reports a
first-class `config-drift` health status on config/schema skew (vs `ok` when rig merely reports disk
drift, vs `error`).

`pm executors` shows the executor ledger (each harness+model's reliability, quota headroom, context
pressure) from a telemetry snapshot or a best-effort `tg-ctl status` read. `pm route` then
**recommends** an executor for a work item purely from that ledger and the item's risk (from labels,
or `--risk`) and privacy: it routes high-risk work to a senior model and low-risk to a cheaper one,
says `wait` when every executor lacks quota headroom, and says `blocked` when local-only work has no
local executor. It is quota/limit guarding, **not** cost accounting (the ecosystem has invocation
stats, not spend), and it is strictly read-only — it writes no events and dispatches nothing.

`pm report` is the read-only reporting surface over the whole ledger: `queue` (counts by category
and health), `stuck` (blocked/exception states, no-progress, cycles, expired timers, dead-lettered
intakes), `retro` (per-item retrospective facts — trigger, recovery, time-in-problem — plus
recurring-pattern counts), `monthly` (a month grouped product-vs-process), `metrics` (average and p50/p90 time-in-status per lifecycle category, from consecutive
transition timestamps), and `aging` (items whose current-state age exceeds the per-state SLA).
Every number carries the item ids behind it, so it is traceable back to the event log — never a
vanity metric.

`bin/pm-metrics-digest` is an optional cron/launchd sibling of `bin/pm-reminders-push`. It
always sends a weekly flow-metrics pulse (`tg --format html --tag report`, title
`Weekly flow metrics (pm)`). Dry-run: `PM_PUSH_DRY_RUN=1 bin/pm-metrics-digest` prints the
body and sends nothing.

The event log lives at `$PM_HOME/events.jsonl` (default `$XDG_STATE_HOME/pm` or
`~/.local/state/pm`).

## Develop

```
python -m pytest -q      # hermetic unit suite
ruff check .             # lint
```

Stdlib-only at import time; no third-party runtime dependencies.
