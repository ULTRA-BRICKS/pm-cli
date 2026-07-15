"""The rig-health adapter: interpret `rig status` into a first-class health status."""

from __future__ import annotations

from pmlib.adapters.rig_health import interpret_rig_status


def test_clean_status_is_ok() -> None:
    assert interpret_rig_status(0, "in sync\n", "").status == "ok"


def test_schema_skew_is_config_drift() -> None:
    # the research #255 failure: installed rig rejects a `harness.kinds` global-config key
    health = interpret_rig_status(4, "", "error: unknown key harness.kinds; rig rejects it")
    assert health.status == "config-drift"
    assert "harness.kinds" in health.detail


def test_other_nonzero_is_error() -> None:
    assert interpret_rig_status(1, "", "network unreachable").status == "error"


def test_normal_disk_drift_report_is_ok_not_error() -> None:
    """rig exiting non-zero to signal DISK drift (with a normal report) is rig working, not broken."""
    report = "repo: /x\n  config layers: global, repo\n  areas rig manages:\n    drift: 1 file"
    assert interpret_rig_status(4, report, "").status == "ok"
