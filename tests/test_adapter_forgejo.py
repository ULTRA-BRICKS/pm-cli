"""The Forgejo adapter: recognize Forgejo PR links and read PR state + CI from the Forgejo API."""

from __future__ import annotations

import http.client
import io
import json
import subprocess
import urllib.error
import urllib.request

import pytest

from pmlib.adapters.forgejo import (
    fetch_forgejo_pr,
    is_forgejo_pr_url,
    parse_forgejo_pr_facts,
    parse_forgejo_pr_url,
)
from pmlib.adapters.github import fetch_pr_facts
from pmlib.adapters.task import extract_pr_url
from pmlib.model import EvidenceRef, WorkItem

_URL = "https://git.hyperide.ai/ultrabricks/pm-cli/pulls/12"
_API = "https://git.hyperide.ai/api/v1/repos/ultrabricks/pm-cli"
_SHA = "0123456789abcdef0123456789abcdef01234567"


# ── link recognition ────────────────────────────────────────────────────────────────────────
def test_parse_forgejo_pr_url() -> None:
    assert parse_forgejo_pr_url(_URL) == (_URL, "git.hyperide.ai", "ultrabricks", "pm-cli", 12)


@pytest.mark.parametrize(
    "tail",
    ["/", "?style=split", "#issuecomment-5", "/?x=1#y"],
)
def test_parse_forgejo_pr_url_canonicalizes_tail(tail: str) -> None:
    assert parse_forgejo_pr_url(_URL + tail)[0] == _URL


@pytest.mark.parametrize(
    "url",
    [
        "https://git.hyperide.ai/ultrabricks/pm-cli/pulls/12/files",  # a PR sub-page
        "https://git.hyperide.ai/ultrabricks/pm-cli/pulls/abc",  # not a number
        "http://git.hyperide.ai/ultrabricks/pm-cli/pulls/12",  # not https
        "https://git.hyperide.ai/ultrabricks/pm-cli/issues/12",  # an issue, not a PR
        "https://git.hyperide.ai/ultrabricks/pm-cli/pull/12",  # GitHub spelling on Forgejo
        "https://evil.example/ultrabricks/pm-cli/pulls/12",  # unknown host
        "https://git.hyperide.ai.evil.example/ultrabricks/pm-cli/pulls/12",  # suffix trick
        "https://git.hyperide.ai@evil.example/ultrabricks/pm-cli/pulls/12",  # userinfo trick
        "https://git.hyperide.ai:8443/ultrabricks/pm-cli/pulls/12",  # port is not the host
        "https://git.hyperide.ai/../pm-cli/pulls/12",  # path traversal in the owner
        "https://git.hyperide.ai/ultrabricks/%2e%2e/pulls/12",  # encoded segment
        "",
    ],
)
def test_parse_forgejo_pr_url_rejects(url: str) -> None:
    assert parse_forgejo_pr_url(url) is None
    assert not is_forgejo_pr_url(url)


def test_host_match_is_case_insensitive_and_canonicalized() -> None:
    parsed = parse_forgejo_pr_url("https://Git.Hyperide.AI/ultrabricks/pm-cli/pulls/12")
    assert parsed is not None and parsed[0] == _URL


# ── discovery from task-cli attachments ──────────────────────────────────────────────────────
def test_extract_pr_url_finds_forgejo_link() -> None:
    assert extract_pr_url([{"url": _URL + "?style=unified"}]) == _URL


def test_extract_pr_url_first_pr_link_wins_across_forges() -> None:
    attachments = [
        {"url": "https://git.hyperide.ai/ultrabricks/pm-cli/issues/3"},
        {"url": _URL},
        {"url": "https://github.com/x/y/pull/1"},
    ]
    assert extract_pr_url(attachments) == _URL


def test_extract_pr_url_ignores_unknown_forge_pulls_link() -> None:
    assert extract_pr_url([{"url": "https://evil.example/a/b/pulls/1"}]) == ""


# ── facts ───────────────────────────────────────────────────────────────────────────────────
def _status(state: str, n: int = 1) -> dict:
    return {"state": state, "statuses": [{"status": state}] * n, "total_count": n}


def test_facts_open_success() -> None:
    fact = parse_forgejo_pr_facts(_URL, {"state": "open", "merged": False}, _status("success"))
    assert fact == {"pr_url": _URL, "pr_state": "open", "ci": "success"}


def test_facts_merged_wins_over_closed() -> None:
    fact = parse_forgejo_pr_facts(_URL, {"state": "closed", "merged": True}, _status("success"))
    assert fact["pr_state"] == "merged"


def test_facts_closed_unmerged() -> None:
    assert parse_forgejo_pr_facts(_URL, {"state": "closed", "merged": False}, None)["pr_state"] == (
        "closed"
    )


@pytest.mark.parametrize(
    ("state", "ci"),
    [("failure", "failure"), ("error", "failure"), ("pending", "pending"), ("warning", "pending")],
)
def test_facts_ci_mapping(state: str, ci: str) -> None:
    assert parse_forgejo_pr_facts(_URL, {"state": "open"}, _status(state))["ci"] == ci


@pytest.mark.parametrize(
    "status",
    [
        {"state": "success", "statuses": "corrupt"},
        {"state": "success", "statuses": [{}]},
        {"state": "success", "statuses": [{"status": "success"}, "junk"]},
        {"state": "success", "statuses": [{"status": "success"}, {"status": "pending"}]},
        {"state": "", "statuses": [{"status": "success"}]},
    ],
)
def test_facts_malformed_or_mixed_status_is_never_green(status: dict) -> None:
    assert parse_forgejo_pr_facts(_URL, {"state": "open"}, status)["ci"] == "pending"


def test_facts_any_failed_status_is_failure() -> None:
    status = {"state": "pending", "statuses": [{"status": "success"}, {"status": "error"}]}
    assert parse_forgejo_pr_facts(_URL, {"state": "open"}, status)["ci"] == "failure"


def test_facts_no_statuses_is_none() -> None:
    fact = parse_forgejo_pr_facts(_URL, {"state": "open"}, {"state": "", "statuses": []})
    assert fact["ci"] == "none"


def test_facts_unreadable_status_is_pending_not_false_green() -> None:
    assert parse_forgejo_pr_facts(_URL, {"state": "open"}, None)["ci"] == "pending"
    assert parse_forgejo_pr_facts(_URL, {"state": "open"}, "junk")["ci"] == "pending"  # type: ignore[arg-type]


def test_facts_unknown_state_is_surfaced() -> None:
    assert parse_forgejo_pr_facts(_URL, {"state": "Weird"}, None)["pr_state"] == "weird"


# ── fetch ───────────────────────────────────────────────────────────────────────────────────
class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _opener(routes: dict[str, object], calls: list | None = None):
    def _open(req, timeout=None):
        if calls is not None:
            calls.append(req)
        body = routes.get(req.full_url)
        if body is None:
            raise urllib.error.HTTPError(req.full_url, 404, "not found", {}, None)
        if isinstance(body, BaseException):
            raise body
        return _Resp(json.dumps(body).encode())

    return _open


def test_fetch_reads_pr_and_head_status() -> None:
    calls: list = []
    routes = {
        f"{_API}/pulls/12": {"state": "open", "merged": False, "head": {"sha": _SHA}},
        f"{_API}/commits/{_SHA}/status": _status("success", 2),
    }
    fact = fetch_forgejo_pr(_URL, opener=_opener(routes, calls))
    assert fact == {"pr_url": _URL, "pr_state": "open", "ci": "success"}
    assert [c.full_url for c in calls] == [f"{_API}/pulls/12", f"{_API}/commits/{_SHA}/status"]


def test_fetch_reports_the_canonical_url() -> None:
    routes = {f"{_API}/pulls/12": {"state": "open"}}
    assert fetch_forgejo_pr(_URL + "/?style=split", opener=_opener(routes))["pr_url"] == _URL


def test_fetch_unreadable_pr_is_none() -> None:
    assert fetch_forgejo_pr(_URL, opener=_opener({})) is None


def test_fetch_network_error_is_none() -> None:
    routes = {f"{_API}/pulls/12": urllib.error.URLError("down")}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes)) is None


def test_fetch_timeout_is_none() -> None:
    routes = {f"{_API}/pulls/12": TimeoutError("slow")}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes)) is None


def test_fetch_http_protocol_error_is_none() -> None:
    routes = {f"{_API}/pulls/12": http.client.IncompleteRead(b"")}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes)) is None


def test_fetch_unreadable_status_is_pending() -> None:
    routes = {f"{_API}/pulls/12": {"state": "open", "head": {"sha": _SHA}}}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes))["ci"] == "pending"


def test_fetch_skips_status_for_a_malformed_sha() -> None:
    calls: list = []
    routes = {f"{_API}/pulls/12": {"state": "open", "head": {"sha": "../../x"}}}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes, calls))["ci"] == "pending"
    assert len(calls) == 1


@pytest.mark.parametrize("head", ["abc", ["x"], 7])
def test_fetch_malformed_head_is_pending_not_a_crash(head: object) -> None:
    routes = {f"{_API}/pulls/12": {"state": "open", "head": head}}
    assert fetch_forgejo_pr(_URL, opener=_opener(routes))["ci"] == "pending"


def test_fetch_non_forgejo_url_is_none() -> None:
    assert fetch_forgejo_pr("https://github.com/x/y/pull/1", opener=_opener({})) is None


def test_token_sent_only_to_the_matching_host(monkeypatch) -> None:
    routes = {f"{_API}/pulls/12": {"state": "open"}}
    calls: list = []
    monkeypatch.setenv("FORGEJO_TOKEN", "sekrit")
    monkeypatch.setenv("FORGEJO_URL", "https://git.hyperide.ai")
    fetch_forgejo_pr(_URL, opener=_opener(routes, calls))
    assert calls[0].get_header("Authorization") == "token sekrit"
    assert "Authorization" not in calls[0].headers  # unredirected only

    # A redirect off the forge must not carry the token.
    redirected = urllib.request.HTTPRedirectHandler().redirect_request(
        calls[0], None, 302, "Found", {}, "https://attacker.example/x"
    )
    assert redirected is not None
    assert redirected.get_header("Authorization") is None

    calls.clear()
    monkeypatch.setenv("FORGEJO_URL", "https://other.example")
    fetch_forgejo_pr(_URL, opener=_opener(routes, calls))
    assert calls[0].get_header("Authorization") is None


# ── reconcile dispatch (adapters/github.fetch_pr_facts) ──────────────────────────────────────
def test_fetch_pr_facts_reads_forgejo_pr_without_gh(monkeypatch) -> None:
    def _no_gh(cmd, **kwargs):
        raise AssertionError(f"gh must not be called for a Forgejo PR, got: {cmd}")

    monkeypatch.setattr(subprocess, "run", _no_gh)
    seen: list[str] = []

    def _fake_fetch(url, opener=None):
        seen.append(url)
        return {"pr_url": url, "pr_state": "merged", "ci": "success"}

    monkeypatch.setattr("pmlib.adapters.github.fetch_forgejo_pr", _fake_fetch)
    a = WorkItem(id="a", evidence=[EvidenceRef("pr", _URL, "x")])
    b = WorkItem(id="b")
    facts = fetch_pr_facts({"a": a, "b": b}, discovered_urls={"b": _URL})
    assert facts["a"]["pr_state"] == "merged" and facts["b"]["pr_state"] == "merged"
    assert seen == [_URL]  # memoized: one read for the shared url


def test_fetch_pr_facts_still_uses_gh_for_github(monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(
        "pmlib.adapters.github.fetch_forgejo_pr", lambda *a, **k: pytest.fail("forgejo read")
    )
    gh_url = "https://github.com/x/y/pull/1"
    item = WorkItem(id="w", evidence=[EvidenceRef("pr", gh_url, "x")])
    assert fetch_pr_facts({"w": item})["w"]["pr_state"] == "open"
    assert calls == [["gh", "pr", "view", gh_url, "--json", "state,statusCheckRollup"]]
