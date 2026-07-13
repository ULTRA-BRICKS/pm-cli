"""pm CLI — argparse + self-registering subcommand dispatch (the effectful entry point).

Thin: it owns argument parsing and turns tool errors into clean ``error:`` lines with a stable
exit code. All logic lives in the sibling pure modules (``events``/``store``/``states``/
``projection``/``model``) and the command handlers in ``pmlib.commands``.

Commands self-register: dropping a module into ``pmlib/commands/`` that exposes ``NAME``,
``HELP``, ``configure(parser)`` and ``run(args) -> int`` adds a subcommand with zero edits here
(the ecosystem's self-registering-commands convention). Stdlib-only at import time.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .commands import discover_commands
from .states import EvidenceError, TransitionError

EXIT_OK = 0
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level parser and wire every discovered subcommand under it."""
    parser = argparse.ArgumentParser(
        prog="pm",
        description="pm — autonomous project-manager coordinator over the task/tg/rig ecosystem.",
    )
    parser.add_argument("--version", action="version", version=f"pm {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    for cmd in discover_commands():
        cp = sub.add_parser(cmd.NAME, help=cmd.HELP, description=cmd.HELP)
        cmd.configure(cp)
        cp.set_defaults(_run=cmd.run)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse args and dispatch. Returns a process exit code; never raises for a user error."""
    parser = build_parser()
    args = parser.parse_args(argv)
    runner = getattr(args, "_run", None)
    if runner is None:
        parser.print_help()
        return EXIT_OK
    try:
        return int(runner(args))
    except (TransitionError, EvidenceError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return getattr(exc, "exit_code", EXIT_USAGE)
    except (ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
