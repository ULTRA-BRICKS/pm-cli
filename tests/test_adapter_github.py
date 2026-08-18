"""The GitHub adapter: find an item's PR ref and parse gh pr view JSON into reconcile facts."""

from __future__ import annotations

import json
import subprocess

import pytest

from pmlib.adapters.github import fetch_pr_facts, parse_pr_facts, pr_url_for
from pmlib.model import EvidenceRef, WorkItem

_URL = "https://github.com/x/y/pull/1"


def test_pr_url_for_finds_pr_evidence() -> None:
    item = WorkItem(
        id="w", evidence=[EvidenceRef("task-record", "t", "x"), EvidenceRef("pr", _URL, "x")]
    )
    assert pr_url_for(item) == _URL


def test_pr_url_for_none_when_no_pr_evidence() -> None:
    assert pr_url_for(WorkItem(id="w")) == ""


def test_parse_success() -> None:
    fact = parse_pr_facts(
        _URL,
        {
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "SUCCESS"}],
        },
    )
    assert fact == {"pr_url": _URL, "pr_state": "open", "ci": "success"}


def test_parse_failure_when_any_check_failed() -> None:
    fact = parse_pr_facts(
        _URL,
        {
            "state": "OPEN",
            "statusCheckRollup": [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}],
        },
    )
    assert fact["ci"] == "failure"


def test_parse_pending_when_a_check_unconcluded() -> None:
    fact = parse_pr_facts(
        _URL,
        {
            "state": "OPEN",
            "statusCheckRollup": [
                {"conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS", "conclusion": None},
            ],
        },
    )
    assert fact["ci"] == "pending"


def test_parse_none_when_no_checks() -> None:
    assert parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": []})["ci"] == "none"


def test_parse_merged_state() -> None:
    assert (
        parse_pr_facts(_URL, {"state": "MERGED", "statusCheckRollup": []})["pr_state"] == "merged"
    )


def test_malformed_rollup_is_pending_not_false_green() -> None:
    """A non-empty rollup with no readable dict entries must NOT read as a green CI."""
    assert (
        parse_pr_facts(_URL, {"state": "OPEN", "statusCheckRollup": ["junk", 3]})["ci"] == "pending"
    )


def test_unknown_pr_state_is_surfaced_not_masked_as_open() -> None:
    assert parse_pr_facts(_URL, {"state": "DRAFT", "statusCheckRollup": []})["pr_state"] == "draft"


def test_fetch_pr_facts_falls_back_to_discovered_url(monkeypatch) -> None:
    """An item with NO recorded `pr` evidence (the state every task-ingested item was stuck in
    before the ingestion fix) must still resolve via a `discovered_urls` override — this is the
    fallback `commands/reconcile.py._discover_pr_urls` feeds in from task-cli attachments."""
    item = WorkItem(id="w", evidence=[EvidenceRef("task-record", "t", "x")])
    assert pr_url_for(item) == ""  # confirms the fallback path is actually exercised

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    facts = fetch_pr_facts({"w": item}, discovered_urls={"w": _URL})
    assert facts["w"]["pr_url"] == _URL
    assert calls == [["gh", "pr", "view", _URL, "--json", "state,statusCheckRollup"]]


def _refuse_to_run(cmd, **kwargs):
    raise AssertionError(f"gh should never be called here, got: {cmd}")


def test_fetch_pr_facts_skips_item_with_no_url_anywhere(monkeypatch) -> None:
    """No recorded evidence AND no discovered url -> skipped, never a crash."""
    monkeypatch.setattr(subprocess, "run", _refuse_to_run)
    item = WorkItem(id="w")
    assert fetch_pr_facts({"w": item}, discovered_urls={}) == {}


def test_fetch_pr_facts_memoizes_shared_pr_url(monkeypatch) -> None:
    """Two items tracking the SAME PR (aggregated via distinct task_refs) must trigger exactly
    ONE `gh pr view` call — not one per item."""
    a = WorkItem(id="a", evidence=[EvidenceRef("pr", _URL, "x")])
    b = WorkItem(id="b", evidence=[EvidenceRef("pr", _URL, "x")])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    facts = fetch_pr_facts({"a": a, "b": b})
    assert facts["a"]["pr_url"] == _URL and facts["b"]["pr_url"] == _URL
    assert len(calls) == 1  # not 2
    assert facts["a"] is not facts["b"]  # a copy each — never the same shared dict object


def test_fetch_pr_facts_memoizes_a_shared_unreadable_url(monkeypatch) -> None:
    """The memo also covers the FAILURE case: two items sharing a PR that `gh` can't read must
    still trigger only one `gh pr view` call, and both end up with no fact."""
    a = WorkItem(id="a", evidence=[EvidenceRef("pr", _URL, "x")])
    b = WorkItem(id="b", evidence=[EvidenceRef("pr", _URL, "x")])
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert fetch_pr_facts({"a": a, "b": b}) == {}
    assert len(calls) == 1


def test_fetch_pr_facts_raises_when_gh_missing(monkeypatch) -> None:
    item = WorkItem(id="w", evidence=[EvidenceRef("pr", _URL, "x")])

    def _missing(cmd, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", _missing)
    with pytest.raises(RuntimeError, match="gh.*CLI not found"):
        fetch_pr_facts({"w": item})


def test_fetch_pr_facts_skips_on_timeout(monkeypatch) -> None:
    """A hung `gh` call must not raise past `fetch_pr_facts` — best-effort, like a nonzero exit."""
    item = WorkItem(id="w", evidence=[EvidenceRef("pr", _URL, "x")])

    def _hangs(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 30))

    monkeypatch.setattr(subprocess, "run", _hangs)
    assert fetch_pr_facts({"w": item}) == {}
