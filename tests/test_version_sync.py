"""Version resolution used by pm: source metadata wins over stale installed metadata.

Installed packages fall back to distribution metadata; all paths use only stdlib imports.
"""

from __future__ import annotations

import importlib.metadata
import runpy
import tomllib
from pathlib import Path

import pytest

from pmlib import __version__

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_package_version_matches_pyproject() -> None:
    assert __version__ == PROJECT["version"]


def test_distribution_name_is_the_pypi_one() -> None:
    assert PROJECT["name"] == "hyper-pm"


def test_release_version() -> None:
    assert PROJECT["version"] == "0.8.0"


def test_source_version_wins_over_stale_install(tmp_path, monkeypatch) -> None:
    package = tmp_path / "pmlib"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text((ROOT / "pmlib/__init__.py").read_text(), encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "hyper-pm"\nversion = "8.7.6"\n', encoding="utf-8"
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "0.5.0")
    assert runpy.run_path(str(init))["__version__"] == "8.7.6"


@pytest.mark.parametrize(
    "source", [None, '[project]\nname = "other"\nversion = "9.9.9"', "invalid["]
)
@pytest.mark.parametrize("installed", ["7.6.5", None])
def test_installed_and_unknown_fallback(tmp_path, monkeypatch, source, installed) -> None:
    package = tmp_path / "pmlib"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text((ROOT / "pmlib/__init__.py").read_text(), encoding="utf-8")
    if source is not None:
        (tmp_path / "pyproject.toml").write_text(source, encoding="utf-8")

    def installed_version(name: str) -> str:
        assert name == "hyper-pm"
        if installed is None:
            raise importlib.metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(importlib.metadata, "version", installed_version)
    assert runpy.run_path(str(init))["__version__"] == (installed or "0.0.0+unknown")
