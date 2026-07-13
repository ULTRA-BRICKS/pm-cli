"""Self-registering command discovery.

Each sibling module in this package that defines ``NAME``, ``HELP``, ``configure(parser)`` and
``run(args) -> int`` is a subcommand. ``discover_commands`` imports them all and returns them
sorted by ``NAME``, so adding a command is dropping a file here — no central registry to edit.
Stdlib-only (``importlib`` + ``pkgutil``).
"""

from __future__ import annotations

import importlib
import pkgutil
from types import ModuleType


def discover_commands() -> list[ModuleType]:
    """Import every command module in this package and return them sorted by ``NAME``."""
    commands: list[ModuleType] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if all(hasattr(mod, attr) for attr in ("NAME", "HELP", "configure", "run")):
            commands.append(mod)
    return sorted(commands, key=lambda m: m.NAME)
