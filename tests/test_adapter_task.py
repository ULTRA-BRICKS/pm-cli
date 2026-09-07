"""The task-cli adapter: task records -> ingest events, with dedupe and state mapping."""

from __future__ import annotations

import json
import subprocess

import pytest

from pmlib.adapters.task import (
    INGEST_STATE_MAP,
    extract_pr_url,
    fetch_ticket_attachments,
    flatten_records,
    plan_ingest,
    raw_ticket_id,
)
from pmlib.events import EventKind
from pmlib.projection import build_projection
from pmlib.states import StateMachine, WorkStateCategory, WorkStateDefinition, default_machine

# The REAL `attachments` array from `task read HYP-1256 --json` against the hyperide repo
# (captured 2026-08-18) — a real ticket with a real merged GitHub PR attached. `task list --json`
# never carries this field; only `task read` does. This is the exact shape PR-url discovery
# (`extract_pr_url` / `fetch_ticket_attachments`) must handle correctly.
_REAL_HYP_1256_ATTACHMENTS = [
    {
        "id": "59544ba6-500f-4a24-aef9-cd2c0e76a381",
        "title": "fix(e2e): install remix-console-capture message listener via frame.evaluate "
        "(HYP-1256)",
        "url": "https://github.com/hyperide/hyper-ext-e2e/pull/155",
        "subtitle": "",
    }
]

_RECORDS = [
    {"id": "#12", "title": "Fix login", "state": "in-progress", "url": "https://gh/12"},
    {"id": "#13", "title": "Docs pass", "state": "todo", "url": "https://gh/13"},
]


def test_plan_ingest_creates_one_item_per_record() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    intakes = [e for e in events if e.kind == EventKind.INTAKE]
    assert len(intakes) == 2
    items = build_projection(events, default_machine())
    assert set(items) == {"task:#12", "task:#13"}


def test_state_is_mapped_from_task_state() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:#12"].state == INGEST_STATE_MAP["in-progress"]
    assert items["task:#13"].state == INGEST_STATE_MAP["todo"]


def test_task_labels_flow_into_work_item() -> None:
    recs = [
        {
            "id": "#9",
            "title": "Harden auth",
            "state": "todo",
            "url": "u",
            "labels": ["security", "backend"],
        }
    ]
    items = build_projection(plan_ingest(recs, {}, default_machine()), default_machine())
    assert items["task:#9"].labels == ["security", "backend"]


def test_task_id_and_url_recorded_as_ref_and_evidence() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:#12"].task_refs == ["#12"]
    assert items["task:#12"].evidence[0].kind == "task-record"
    assert items["task:#12"].evidence[0].uri == "https://gh/12"


def test_dedupe_skips_already_tracked_ticket() -> None:
    first = plan_ingest(_RECORDS, {}, default_machine())
    existing = build_projection(first, default_machine())
    # A second ingest of the same records against the existing projection must be a no-op.
    second = plan_ingest(_RECORDS, existing, default_machine())
    assert second == []


def test_new_ticket_alongside_tracked_ones_is_added() -> None:
    existing = build_projection(plan_ingest(_RECORDS, {}, default_machine()), default_machine())
    more = _RECORDS + [{"id": "#14", "title": "New", "state": "todo", "url": "u"}]
    events = plan_ingest(more, existing, default_machine())
    items = build_projection(events, default_machine())
    assert set(items) == {"task:#14"}  # only the untracked one


def test_flat_records_without_project_use_bare_ref() -> None:
    events = plan_ingest(_RECORDS, {}, default_machine())
    items = build_projection(events, default_machine())
    # no group project -> bare ref, id task:<ticket-id>
    assert items["task:#12"].project_id == ""
    assert items["task:#12"].task_refs == ["#12"]


def test_record_without_id_is_skipped() -> None:
    events = plan_ingest([{"id": "", "title": "junk", "state": "todo"}], {}, default_machine())
    assert events == []


# ── the real `task list --all --json` shape: project-grouped, not a flat ticket array ──
_GROUPED = [
    {
        "project": "alex/app",
        "backend": "github-issues",
        "error": None,
        "tickets": [
            {"id": "#12", "title": "Fix login", "state": "in-progress", "url": "https://gh/12"},
            {"id": "#13", "title": "Docs", "state": "todo", "url": "https://gh/13"},
        ],
    },
]


def test_flatten_grouped_output_yields_tickets_with_project() -> None:
    flat = flatten_records(_GROUPED)
    assert [r["id"] for r in flat] == ["#12", "#13"]
    assert all(r["project"] == "alex/app" for r in flat)


def test_flatten_passes_through_a_flat_array() -> None:
    assert flatten_records(_RECORDS) == _RECORDS


def test_flatten_skips_groups_with_an_error() -> None:
    grouped = [{"project": "broken", "error": "auth failed", "tickets": []}]
    assert flatten_records(grouped) == []


def test_ingest_uses_group_project_when_no_override() -> None:
    events = plan_ingest(flatten_records(_GROUPED), {}, default_machine())
    items = build_projection(events, default_machine())
    assert items["task:alex/app:#12"].project_id == "alex/app"


def test_same_ticket_id_in_two_projects_stays_distinct() -> None:
    """GitHub ids are per-repo — #12 in two projects must NOT collapse into one item."""
    grouped = [
        {
            "project": "alex/app",
            "error": None,
            "tickets": [{"id": "#12", "title": "A", "state": "todo", "url": "a"}],
        },
        {
            "project": "alex/lib",
            "error": None,
            "tickets": [{"id": "#12", "title": "B", "state": "todo", "url": "b"}],
        },
    ]
    events = plan_ingest(flatten_records(grouped), {}, default_machine())
    items = build_projection(events, default_machine())
    assert set(items) == {"task:alex/app:#12", "task:alex/lib:#12"}
    assert items["task:alex/app:#12"].title == "A"
    assert items["task:alex/lib:#12"].title == "B"


def test_onboarding_a_ticket_is_a_single_atomic_event() -> None:
    """Intake + its task-record evidence ride ONE event, so a crash can't strand the evidence."""
    events = plan_ingest([_RECORDS[0]], {}, default_machine())
    assert len(events) == 1
    assert events[0].kind == EventKind.INTAKE
    items = build_projection(events, default_machine())
    assert items["task:#12"].evidence[0].kind == "task-record"


def test_within_batch_duplicate_id_dedups() -> None:
    events = plan_ingest([_RECORDS[0], _RECORDS[0]], {}, default_machine())
    assert len(events) == 1  # the second copy of the same id is skipped


def test_unmappable_pm_state_falls_back_to_initial() -> None:
    """A machine lacking the mapped target seeds the item into its initial state, never crashing."""
    tiny = StateMachine(
        {"seen": WorkStateDefinition("seen", WorkStateCategory.INTAKE, [])}, initial_state="seen"
    )
    events = plan_ingest([_RECORDS[1]], {}, tiny)  # state "todo" -> "ticketed" (absent in tiny)
    items = build_projection(events, tiny)
    assert items["task:#13"].state == "seen"


# ── PR-url discovery (`task list --json` omits `attachments`; `task read` carries it) ──────
def test_extract_pr_url_finds_real_github_pr_attachment() -> None:
    """The regression proof: a real ticket (HYP-1256) with a real merged PR attached (#155) must
    resolve — this is exactly the data `task list --json` never surfaces, and exactly what made
    `pr_url_for` (and therefore `pm reconcile --source pr`) return nothing for ANY ticket, ever."""
    assert extract_pr_url(_REAL_HYP_1256_ATTACHMENTS) == (
        "https://github.com/hyperide/hyper-ext-e2e/pull/155"
    )


def test_extract_pr_url_ignores_non_pr_github_links() -> None:
    """A github.com attachment that ISN'T a pull-request url (an issue, a commit, a repo root)
    must not false-positive as the tracked PR."""
    attachments = [
        {"url": "https://github.com/hyperide/hyperide/issues/42"},
        {"url": "https://github.com/hyperide/hyperide/commit/abc123"},
        {"url": "https://github.com/hyperide/hyperide"},
    ]
    assert extract_pr_url(attachments) == ""


def test_extract_pr_url_none_when_no_attachments() -> None:
    assert extract_pr_url([]) == ""


def test_extract_pr_url_skips_malformed_entries() -> None:
    assert extract_pr_url([None, "junk", {"no_url": "here"}]) == ""


def test_extract_pr_url_returns_first_match() -> None:
    attachments = [
        {"url": "https://example.com/doc"},
        {"url": "https://github.com/x/y/pull/1"},
        {"url": "https://github.com/x/y/pull/2"},
    ]
    assert extract_pr_url(attachments) == "https://github.com/x/y/pull/1"


def test_extract_pr_url_accepts_trailing_slash_and_canonicalizes_it_away() -> None:
    assert extract_pr_url([{"url": "https://github.com/x/y/pull/1/"}]) == (
        "https://github.com/x/y/pull/1"
    )


def test_extract_pr_url_strips_query_string_and_fragment() -> None:
    """A tracking param or comment-anchor fragment on the attachment url must not stop discovery
    — and must not leak into the returned (canonical) url used for `gh pr view`."""
    assert (
        extract_pr_url([{"url": "https://github.com/x/y/pull/1?notification_referrer_id=abc"}])
        == "https://github.com/x/y/pull/1"
    )
    assert extract_pr_url([{"url": "https://github.com/x/y/pull/1#issuecomment-123"}]) == (
        "https://github.com/x/y/pull/1"
    )


def test_extract_pr_url_rejects_pull_sub_paths() -> None:
    """`/pull/1/files`, `/pull/1/commits` etc. are PR SUB-pages, not the PR itself — must not
    match (the anchor's whole point is precision here)."""
    assert extract_pr_url([{"url": "https://github.com/x/y/pull/1/files"}]) == ""


def test_extract_pr_url_rejects_non_https() -> None:
    assert extract_pr_url([{"url": "http://github.com/x/y/pull/1"}]) == ""


def test_extract_pr_url_rejects_non_numeric_pr_id() -> None:
    assert extract_pr_url([{"url": "https://github.com/x/y/pull/abc"}]) == ""


# ── `raw_ticket_id` — the inverse of the qualified `<project>:<id>` ref ────────────────────
def test_raw_ticket_id_strips_project_prefix() -> None:
    assert raw_ticket_id("HYP:HYP-1256") == "HYP-1256"


def test_raw_ticket_id_passes_through_bare_ref() -> None:
    assert raw_ticket_id("#12") == "#12"


# ── `fetch_ticket_attachments` — the effectful shell-out (mocked subprocess) ───────────────
def test_fetch_ticket_attachments_returns_the_array(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"attachments": _REAL_HYP_1256_ATTACHMENTS}), stderr=""
        ),
    )
    assert fetch_ticket_attachments("HYP-1256") == _REAL_HYP_1256_ATTACHMENTS


def test_fetch_ticket_attachments_empty_on_nonzero_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="nope"),
    )
    assert fetch_ticket_attachments("HYP-404") == []


def test_fetch_ticket_attachments_empty_on_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout="{not json", stderr=""),
    )
    assert fetch_ticket_attachments("HYP-1") == []


def test_fetch_ticket_attachments_empty_when_attachments_not_a_list(monkeypatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"attachments": "not-a-list"}), stderr=""
        ),
    )
    assert fetch_ticket_attachments("HYP-1") == []


def test_fetch_ticket_attachments_empty_on_timeout(monkeypatch) -> None:
    """A hung backend call must not raise past this function — best-effort, like a nonzero exit."""

    def _hangs(cmd, **k):
        raise subprocess.TimeoutExpired(cmd, k.get("timeout", 30))

    monkeypatch.setattr(subprocess, "run", _hangs)
    assert fetch_ticket_attachments("HYP-1") == []


def test_fetch_ticket_attachments_raises_when_task_missing(monkeypatch) -> None:
    def _missing(cmd, **k):
        raise FileNotFoundError("task")

    monkeypatch.setattr(subprocess, "run", _missing)
    with pytest.raises(RuntimeError, match="task.*CLI not found"):
        fetch_ticket_attachments("HYP-1")


def test_fetch_ticket_attachments_bad_cwd_reports_repo_dir_not_task_missing(tmp_path) -> None:
    """The same misreport `fetch_task_records` guards against (bad cwd manifests as the identical
    `FileNotFoundError` subprocess raises for a missing executable) must not surface here as
    "task not found" either."""
    missing = str(tmp_path / "does-not-exist")
    with pytest.raises(RuntimeError, match="repo-dir"):
        fetch_ticket_attachments("HYP-1", cwd=missing)
