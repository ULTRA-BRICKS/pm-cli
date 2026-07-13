"""Shared fixtures — an isolated event store rooted at ``tmp_path`` (never the real HOME)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the package importable when running pytest from the repo root without an install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture()
def pm_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the store at an isolated dir via ``PM_HOME`` so tests never touch the real state."""
    home = tmp_path / "pm-home"
    monkeypatch.setenv("PM_HOME", str(home))
    return home
