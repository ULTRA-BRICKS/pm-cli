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

import importlib.metadata
import tomllib
from pathlib import Path


def _get_version() -> str:
    """Resolve pm's version from its checkout first, then installed distribution metadata."""
    try:
        with (Path(__file__).resolve().parent.parent / "pyproject.toml").open("rb") as source:
            project = tomllib.load(source).get("project", {})
        if isinstance(project, dict) and project.get("name") == "hyper-pm":
            declared = project.get("version")
            if isinstance(declared, str) and declared:
                return declared
    except (OSError, tomllib.TOMLDecodeError):
        pass

    try:
        return importlib.metadata.version("hyper-pm")
    except importlib.metadata.PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _get_version()
