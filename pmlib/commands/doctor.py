"""``pm doctor`` — health checks: store writability + state-machine integrity.

Diagnostics whose only side effect is a write-and-delete probe file that proves the store dir is
writable (it never touches the event log). Research #255 lists ``doctor`` among the Release-1
surfaces. Exits non-zero (with a structured code) when a check fails, so a script can gate on it.
"""

from __future__ import annotations

import argparse

from ..states import WorkStateCategory, default_machine
from ._shared import open_store

NAME = "doctor"
HELP = "check store writability and state-machine integrity"

EXIT_UNHEALTHY = 6


def configure(parser: argparse.ArgumentParser) -> None:
    _ = parser  # no options yet


def run(args: argparse.Namespace) -> int:
    _ = args
    problems: list[str] = []
    _check_store(problems)
    _check_machine(problems)
    for line in problems:
        print(f"FAIL  {line}")
    if problems:
        return EXIT_UNHEALTHY
    print("ok  event store is writable")
    print("ok  state machine is internally consistent")
    return 0


def _check_store(problems: list[str]) -> None:
    store = open_store()
    try:
        store.directory.mkdir(parents=True, exist_ok=True)
        probe = store.directory / ".pm-doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        problems.append(f"store dir not writable ({store.directory}): {exc}")


def _check_machine(problems: list[str]) -> None:
    m = default_machine()
    ids = set(m.states)
    for defn in m.states.values():
        for nxt in defn.allowed_next:
            if nxt not in ids:
                problems.append(f"state `{defn.id}` points at unknown `{nxt}`")
        if defn.terminal and defn.allowed_next:
            problems.append(f"terminal state `{defn.id}` has exits {defn.allowed_next}")
    covered = {d.category for d in m.states.values()}
    for cat in WorkStateCategory:
        if cat not in covered:
            problems.append(f"category `{cat.value}` has no states")
