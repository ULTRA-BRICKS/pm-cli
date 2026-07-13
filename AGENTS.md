# AGENTS.md — pm-cli

Rules for agents working in this repo. English only (no Cyrillic anywhere in repo docs).

## What this is

`pm` is the autonomous project-manager coordinator over the task/tg/rig ecosystem. It is a
**coordinator and state machine**, NOT a ticket backend, provisioning engine, Telegram daemon,
or implementation worker. It projects evidence from the surrounding tools into a durable,
event-sourced work queue and reports on it. The full design is agent-tools research #255
(`docs/research/255-pm-agent-research.md` in the agent-tools repo).

Release 1 is an **"organ of sight"**: it observes and reconciles, it does NOT autonomously
dispatch or edit code. Autonomous dispatch (Release 2) lands only behind the adversarial test
suite the design lists (privacy-aware fallback, kill-switch, crash-restart dedup).

## Hard rules

- **Stdlib-only at import time.** Every `pmlib/*` module imports only the standard library when
  loaded — the store is JSONL over stdlib `json`, the FSM is pure data. Heavy/optional deps (yaml,
  adapters) are imported lazily inside the command that needs them. `pm --help` and `pm doctor`
  must run with zero third-party imports.
- **The queue is a deterministic projection of an append-only event log.** State is never mutated
  in place — every change is a new immutable event. A crash/restart must replay the log to the
  exact same projection. This is the core invariant; do not add a mutable side store that can
  diverge from the log.
- **Validate before you write.** A transition is validated against the FSM (adjacency + evidence
  gate) BEFORE its event is appended, so the log never holds an illegal move. Replay is lenient
  (records a rejection on `item.errors`) so one bad line can never crash the whole queue.
- **pm never implements.** It records observations, projects state, and (later) delegates. It must
  not edit product code, run test/fix loops, commit, ship, or bypass gates.
- **Commands self-register.** Drop a module into `pmlib/commands/` exposing `NAME`, `HELP`,
  `configure(parser)`, `run(args) -> int`. No central registry to edit. Keep `cli.py` thin.

## Layout

- `pmlib/events.py` — the immutable `Event` + `EventKind` vocabulary + `new_event`.
- `pmlib/store.py` — the append-only, fsync'd, torn-line-tolerant JSONL `EventStore`.
- `pmlib/states.py` — the configurable FSM: categories, `WorkStateDefinition`, `StateMachine`,
  the `DEFAULT_STATES` registry (research #255's flexible-FSM section).
- `pmlib/model.py` — the `WorkItem` projected shape.
- `pmlib/projection.py` — `build_projection`: the deterministic fold of events into work items.
- `pmlib/cli.py` + `pmlib/commands/*` — argparse dispatch + self-registering commands.

## Tests

- `python -m pytest -q` — the unit suite. Hermetic: `tests/conftest.py::pm_home` points the store
  at `tmp_path` via `$PM_HOME`, so tests never touch the real HOME.
- `ruff check .` — lint. Both must be green before commit. TDD red-first is the house style.
