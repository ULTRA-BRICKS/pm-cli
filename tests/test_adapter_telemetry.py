"""The telemetry adapter: parse an executor snapshot and best-effort parse `tg-ctl status`."""

from __future__ import annotations

from pmlib.adapters.telemetry import parse_executors, parse_tgctl_status


def test_parse_executors_from_snapshot() -> None:
    execs = parse_executors([{
        "id": "x", "harness": "claude-code", "model": "claude-opus-4-8",
        "pane_id": "%3", "repo": "/r", "reliability": "healthy",
        "limit_remaining": 0.5, "context_pressure": 0.3, "active_tasks": 2, "local": False}])
    e = execs[0]
    assert e.id == "x" and e.model == "claude-opus-4-8" and e.limit_remaining == 0.5
    assert e.reliability == "healthy" and e.active_tasks == 2


def test_parse_executors_defaults_unknown_telemetry() -> None:
    execs = parse_executors([{"id": "y", "harness": "codex"}])
    assert execs[0].limit_remaining is None
    assert execs[0].reliability == "no-telemetry"


def test_parse_executors_skips_records_without_id() -> None:
    assert parse_executors([{"harness": "codex"}, "junk", {"id": "z", "harness": "pi"}]) == \
        parse_executors([{"id": "z", "harness": "pi"}])


_TGCTL = """tg-ctl: running (pid 824) — bot 8574086668
registrations: 2
  - pane %0, cwd /Users/ultra/work/hyperide
  - pane %3, cwd /Users/ultra/xp/rig-cli
target: ambiguous —
hyperos 1:2 %4 (claude, /Users/ultra/xp/hyperos)
rig:1 %3 (claude, /Users/ultra/xp/rig-cli)
usage telemetry: NOT installed — run `tg-ctl install-hooks`
"""


def test_parse_tgctl_status_extracts_executors() -> None:
    execs = parse_tgctl_status(_TGCTL)
    rig = next(e for e in execs if e.pane_id == "%3")
    assert rig.harness == "claude"
    assert rig.repo.endswith("rig-cli")
    # usage telemetry not installed -> we cannot trust quota, so no-telemetry
    assert all(e.reliability == "no-telemetry" for e in execs)
