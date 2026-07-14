"""The GitHub adapter: find an item's PR ref and parse gh pr view JSON into reconcile facts."""

from __future__ import annotations

from pmlib.adapters.github import parse_pr_facts, pr_url_for
from pmlib.model import EvidenceRef, WorkItem

_URL = "https://github.com/x/y/pull/1"


def test_pr_url_for_finds_pr_evidence() -> None:
    item = WorkItem(id="w", evidence=[EvidenceRef("task-record", "t", "x"),
                                      EvidenceRef("pr", _URL, "x")])
    assert pr_url_for(item) == _URL


def test_pr_url_for_none_when_no_pr_evidence() -> None:
    assert pr_url_for(WorkItem(id="w")) == ""


def test_parse_success() -> None:
    fact = parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"conclusion": "SUCCESS"}]})
    assert fact == {"pr_url": _URL, "pr_state": "open", "ci": "success"}


def test_parse_failure_when_any_check_failed() -> None:
    fact = parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]})
    assert fact["ci"] == "failure"


def test_parse_pending_when_a_check_unconcluded() -> None:
    fact = parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": [
        {"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS", "conclusion": None}]})
    assert fact["ci"] == "pending"


def test_parse_none_when_no_checks() -> None:
    assert parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": []})["ci"] == "none"


def test_parse_merged_state() -> None:
    assert parse_pr_facts(_URL, {"state": "MERGED", "statusCheckRollup": []})["pr_state"] == "merged"


def test_malformed_rollup_is_pending_not_false_green() -> None:
    """A non-empty rollup with no readable dict entries must NOT read as a green CI."""
    assert parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": ["junk", 3]})["ci"] == "pending"


def test_unknown_pr_state_is_surfaced_not_masked_as_open() -> None:
    assert parse_pr_facts(_URL, {"state": "DRAFT", "statusCheckRollup": []})["pr_state"] == "draft"
