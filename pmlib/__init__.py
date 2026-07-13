"""pm — the autonomous project-manager coordinator over the task/tg/rig ecosystem.

pm is a coordinator and state machine, NOT a ticket backend, provisioning engine, Telegram
daemon, or implementation worker (see agent-tools research #255). Release 1 is an "organ of
sight": it reconciles evidence from the surrounding tools into a durable, event-sourced queue
projection and reports on it. It does not autonomously dispatch or edit code.

Design invariant (the spine this package is built around): the work queue is a deterministic
projection of an append-only event log, so a crash/restart replays the log to the exact same
state. Nothing here imports a third-party package at load time (stdlib-only, per the ecosystem
convention) — the store is JSONL over stdlib ``json``.
"""

from __future__ import annotations

__version__ = "0.1.0"
