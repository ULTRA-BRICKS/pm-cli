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
pm queue [--json] [--category <c>] [--state <s>]
pm why <id>                                    # current state, legal next moves, missing evidence
pm doctor                                      # store writability + FSM integrity
pm version
```

The event log lives at `$PM_HOME/events.jsonl` (default `$XDG_STATE_HOME/pm` or
`~/.local/state/pm`).

## Develop

```
python -m pytest -q      # hermetic unit suite
ruff check .             # lint
```

Stdlib-only at import time; no third-party runtime dependencies.
