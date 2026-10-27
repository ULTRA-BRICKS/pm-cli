"""The validation gate: auto-continue stays forbidden unless a matching passing record exists."""

from __future__ import annotations

import json
import os
import stat

import pytest

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_decision as dec
from pmlib.chatgpt_control import audit_files as files
from pmlib.chatgpt_control import audit_validation as val

DENIED = {"allowed": False, "reason": "classification-not-validated"}
BENCH = "b" * 64


def record(**kw) -> val.ValidationRecord:
    base = {
        "prompt_version": clf.PROMPT_VERSION,
        "model": "laya-0.23.0",
        "thresholds_digest": dec.thresholds_digest(dec.DEFAULT_THRESHOLDS),
        "bench_digest": BENCH,
        "passed": True,
        "recorded_at": 1792254000.0,
        "summary": {"heldout_false_done": 0, "heldout_cases": 20},
    }
    base.update(kw)
    return val.ValidationRecord(**base)


def gate(tmp_path, **kw):
    args = {"model": "laya-0.23.0", "thresholds": dec.DEFAULT_THRESHOLDS, "bench_digest": BENCH}
    args.update(kw)
    return val.load_gate(tmp_path / "audit", **args)


def test_without_a_record_auto_continue_is_denied(tmp_path):
    g = gate(tmp_path)
    assert g.auto_continue(fingerprint_matches=True) == DENIED
    assert g.report_field() == DENIED


def test_a_matching_passing_record_with_a_matching_fingerprint_allows(tmp_path):
    val.save_record(tmp_path / "audit", record())
    g = gate(tmp_path)
    assert g.auto_continue(fingerprint_matches=True) == {"allowed": True, "reason": "validated"}
    assert g.report_field() == {"allowed": True, "reason": "validated"}


def test_a_mismatching_verdict_fingerprint_denies_even_with_a_valid_record(tmp_path):
    val.save_record(tmp_path / "audit", record())
    assert gate(tmp_path).auto_continue(fingerprint_matches=False) == DENIED


def test_a_failing_record_denies(tmp_path):
    val.save_record(tmp_path / "audit", record(passed=False))
    assert gate(tmp_path).auto_continue(fingerprint_matches=True) == DENIED


@pytest.mark.parametrize(
    "current",
    [
        {"model": "laya-9.9.9"},
        {"model": ""},
        {"thresholds": dec.Thresholds(done_min=0.95)},
        {"bench_digest": "c" * 64},
    ],
)
def test_any_changed_ingredient_voids_the_record(tmp_path, current):
    val.save_record(tmp_path / "audit", record())
    assert gate(tmp_path, **current).auto_continue(fingerprint_matches=True) == DENIED


def test_a_changed_prompt_version_voids_the_record(tmp_path):
    val.save_record(tmp_path / "audit", record(prompt_version="completion-v0"))
    assert gate(tmp_path).auto_continue(fingerprint_matches=True) == DENIED


def test_the_record_is_private_and_a_symlinked_record_is_ignored_and_never_written(tmp_path):
    directory = tmp_path / "audit"
    val.save_record(directory, record())
    path = directory / val.RECORD_FILE
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    path.symlink_to(target)
    assert val.load_record(directory) is None
    with pytest.raises(files.UnsafePath):
        val.save_record(directory, record())
    assert target.read_text() == "{}"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(passed="yes"),
        lambda d: d.update(passed=1),
        lambda d: d.pop("bench_digest"),
        lambda d: d.update(extra=1),
        lambda d: d.update(recorded_at=float("inf")),
        lambda d: d.update(summary="text"),
    ],
)
def test_a_malformed_record_is_no_record(tmp_path, mutate):
    directory = tmp_path / "audit"
    wire = record().to_mapping()
    mutate(wire)
    path = directory / val.RECORD_FILE
    directory.mkdir(mode=0o700)
    path.write_text(json.dumps(wire))
    assert val.load_record(directory) is None
    assert gate(tmp_path).auto_continue(fingerprint_matches=True) == DENIED


def test_a_passing_record_whose_numbers_say_otherwise_is_still_denied(tmp_path):
    """The gate re-checks the recorded heldout false-done count instead of trusting the flag."""
    val.save_record(
        tmp_path / "audit", record(summary={"heldout_false_done": 2, "heldout_cases": 20})
    )
    assert gate(tmp_path).auto_continue(fingerprint_matches=True) == DENIED
