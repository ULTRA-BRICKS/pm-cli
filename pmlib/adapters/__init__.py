"""Adapters — the seam between pm and the surrounding tools (task-cli, tg-cli, rig, CI, ...).

Each adapter is the ONLY module that knows a foreign tool's on-disk/CLI shape (mirrors rig-cli's
``catalog.py`` single-seam convention). An adapter turns foreign records into pm events (inbound)
and, later, pm intents into foreign commands (outbound). Pure planning is kept separate from the
effectful fetch so it stays unit-testable without the foreign tool installed.
"""

from __future__ import annotations
