"""``pm version`` — print the tool version (also available as ``pm --version``)."""

from __future__ import annotations

import argparse

from .. import __version__

NAME = "version"
HELP = "print the pm version"


def configure(parser: argparse.ArgumentParser) -> None:
    _ = parser


def run(args: argparse.Namespace) -> int:
    _ = args
    print(f"pm {__version__}")
    return 0
