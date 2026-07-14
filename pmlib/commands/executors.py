"""``pm executors`` — show the executor ledger (read-only).

Lists every executor the PM agent knows (harness, model, pane, repo, reliability, quota headroom,
context pressure, active tasks) from a telemetry snapshot (``--from-file``) or a best-effort
``tg-ctl status`` read. It writes nothing and dispatches nothing.
"""

from __future__ import annotations

import argparse
import json

from ..adapters.telemetry import fetch_executors, parse_executors
from ..executors import Executor

NAME = "executors"
HELP = "show the executor ledger (harness/model/quota/reliability)"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from-file", default="", dest="from_file",
                        help="read an executor snapshot (JSON array) instead of tg-ctl status")
    parser.add_argument("--json", action="store_true", help="emit a JSON array instead of a table")


def run(args: argparse.Namespace) -> int:
    executors = load_executors(args.from_file)
    if args.json:
        print(json.dumps([e.to_dict() for e in executors], ensure_ascii=False))
        return 0
    _print_table(executors)
    return 0


def load_executors(from_file: str) -> list[Executor]:
    """The executor ledger — from a snapshot file if given, else a best-effort tg-ctl read."""
    if from_file:
        import pathlib

        data = json.loads(pathlib.Path(from_file).read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError(f"{from_file}: expected a JSON array of executor records")
        return parse_executors(data)
    return fetch_executors()


def _print_table(executors: list[Executor]) -> None:
    if not executors:
        print("(no executors)")
        return
    id_w = max(len(e.id) for e in executors)
    for e in executors:
        head = _headroom(e.limit_remaining)
        print(f"{e.id:<{id_w}}  {e.capability():<6}  {e.reliability:<12}  quota {head}  {e.repo}")


def _headroom(limit: float | None) -> str:
    return "  n/a" if limit is None else f"{int(limit * 100):>3}%"
