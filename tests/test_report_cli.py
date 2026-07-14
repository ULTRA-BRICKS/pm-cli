"""`pm report queue|stuck|retro|monthly` end-to-end (read-only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pmlib.cli import main


def _run(*argv: str) -> int:
    return main(list(argv))


def _seed(states: list[str]) -> None:
    _run("record", "intake", "--id", "a", "--title", "feature")
    for s in states:
        _run("record", "transition", "a", s)


def test_report_queue_json(pm_home: Path, capsys) -> None:
    _run("record", "intake", "--id", "a", "--title", "x")
    capsys.readouterr()
    assert _run("report", "queue", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["kind"] == "queue" and rep["total"] == 1


def test_report_stuck_json(pm_home: Path, capsys) -> None:
    _seed(["classified", "ticketed", "ready", "blocked-by-dependency"])
    capsys.readouterr()
    assert _run("report", "stuck", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert any(e["item_id"] == "a" for e in rep["entries"])


def test_report_retro_json(pm_home: Path, capsys) -> None:
    _seed(["classified", "ticketed", "ready", "blocked-by-dependency", "ready"])
    capsys.readouterr()
    assert _run("report", "retro", "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert any(e["item_id"] == "a" and e["recovered"] for e in rep["entries"])


def test_report_monthly_json(pm_home: Path, capsys) -> None:
    _seed(["classified"])
    capsys.readouterr()
    # use the current month so the intake/transition fall in-range
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    assert _run("report", "monthly", "--month", month, "--json") == 0
    rep = json.loads(capsys.readouterr().out)
    assert "product" in rep["groups"] and "process" in rep["groups"]


def test_report_rejects_unknown_kind(pm_home: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run("report", "nonsense")
    assert exc.value.code != 0


def test_report_monthly_rejects_malformed_month(pm_home: Path, capsys) -> None:
    for bad in ("2026", "2026-13", "2026-00", "2026-7"):
        rc = _run("report", "monthly", "--month", bad)
        assert rc != 0, bad
        assert "yyyy-mm" in capsys.readouterr().err.lower()
