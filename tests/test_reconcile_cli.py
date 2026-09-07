"""`pm reconcile --source pr|rig` end-to-end."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import pmlib.commands.reconcile as reconcile_cmd
from pmlib.cli import main

_URL = "https://github.com/x/y/pull/1"

# The REAL `task read HYP-1256 --json` attachments payload (captured 2026-08-18 against the
# hyperide repo) — a real ticket with a real merged GitHub PR attached. `task list --json` (what
# `pm ingest --source task` calls in bulk) never carries this field.
_REAL_PR_URL = "https://github.com/hyperide/hyper-ext-e2e/pull/155"
_REAL_ATTACHMENTS = [
    {
        "id": "59544ba6-500f-4a24-aef9-cd2c0e76a381",
        "title": "fix(e2e): install remix-console-capture message listener via frame.evaluate "
        "(HYP-1256)",
        "url": _REAL_PR_URL,
        "subtitle": "",
    }
]


def _run(*argv: str) -> int:
    return main(list(argv))


def _facts_file(tmp_path: Path, ci: str = "success", pr_state: str = "open") -> str:
    path = tmp_path / "facts.json"
    path.write_text(
        json.dumps([{"item_id": "w", "pr_url": _URL, "pr_state": pr_state, "ci": ci}]),
        encoding="utf-8",
    )
    return str(path)


def _seed_pr_item(task_ref: str | None = None, *, with_pr_evidence: bool = True) -> None:
    intake_argv = ["record", "intake", "--id", "w", "--title", "feature"]
    if task_ref:
        intake_argv += ["--task-ref", task_ref]
    _run(*intake_argv)
    for s in (
        "classified",
        "ticketed",
        "ready",
        "scoped",
        "queued",
        "dispatched",
        "implementing",
        "reviewing",
        "verifying",
        "pr-open",
    ):
        _run("record", "transition", "w", s)
    if with_pr_evidence:
        _run("record", "evidence", "w", "pr", _URL)


def _refuse_to_run(cmd, **kwargs):
    raise AssertionError(f"should not be called, got: {cmd}")


def test_pr_reconcile_advances_on_green_ci(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    capsys.readouterr()
    assert _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path)) == 0
    capsys.readouterr()
    _run("queue", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == "w")
    assert row["state"] == "ship-ready"


def test_pr_reconcile_from_file_never_shells_task_read(
    pm_home: Path, monkeypatch, tmp_path: Path
) -> None:
    """`--from-file` is the fully-hermetic path (no `gh`, no `task`) — a future refactor must not
    silently add PR-url discovery's subprocess overhead to it."""
    _seed_pr_item(task_ref="HYP:HYP-1256")
    monkeypatch.setattr(reconcile_cmd.task_adapter, "fetch_ticket_attachments", _refuse_to_run)
    assert _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path)) == 0


def test_pr_reconcile_is_idempotent(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    ff = _facts_file(tmp_path)
    _run("reconcile", "--source", "pr", "--from-file", ff)
    capsys.readouterr()
    _run("reconcile", "--source", "pr", "--from-file", ff, "--json")
    summary = json.loads(capsys.readouterr().out)
    assert summary["transitions"] == 0  # converged: a re-run changes nothing


def test_merged_pr_does_not_advance_past_ship_ready(pm_home: Path, tmp_path: Path, capsys) -> None:
    _seed_pr_item()
    _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path))  # -> ship-ready
    capsys.readouterr()
    _run("reconcile", "--source", "pr", "--from-file", _facts_file(tmp_path, pr_state="merged"))
    capsys.readouterr()
    _run("queue", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == "w")
    assert row["state"] == "ship-ready"  # merge recorded, but the fork boundary holds


def test_rig_reconcile_reports_config_drift(pm_home: Path, tmp_path: Path, capsys) -> None:
    health = tmp_path / "rig.json"
    health.write_text(
        json.dumps(
            {"returncode": 4, "stdout": "", "stderr": "unknown key harness.kinds; rejected"}
        ),
        encoding="utf-8",
    )
    assert _run("reconcile", "--source", "rig", "--from-file", str(health), "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "config-drift"


def test_reconcile_rejects_unknown_source(pm_home: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _run("reconcile", "--source", "nope")
    assert exc.value.code != 0


# ── the regression: PR-url discovery via `task read` (the fix for the `scanned:0` bug) ────────
def test_pr_reconcile_discovers_pr_url_via_task_read(pm_home: Path, monkeypatch, capsys) -> None:
    """Reproduces the exact plumbing bug: an item ingested the way `pm ingest --source task`
    onboards every ticket — task_refs set, but only `task-record` evidence, never `pr` evidence,
    because `task list --json` (bulk ingest) has no `attachments` field. Before the fix,
    `fetch_pr_facts` found no url for this item and `pm reconcile --source pr` silently returned
    `scanned:0` for it, forever. After the fix, reconcile shells `task read` (mocked here with the
    REAL HYP-1256/#155 attachments payload), discovers the PR, fetches its (mocked) `gh` facts,
    and advances the item — proving the whole chain now actually works, not just one function."""
    _seed_pr_item(task_ref="HYP:HYP-1256", with_pr_evidence=False)
    capsys.readouterr()

    monkeypatch.setattr(
        reconcile_cmd.task_adapter,
        "fetch_ticket_attachments",
        lambda ticket_id, cwd="": _REAL_ATTACHMENTS if ticket_id == "HYP-1256" else [],
    )

    def fake_gh(cmd, **kwargs):
        assert cmd[:3] == ["gh", "pr", "view"]
        assert cmd[3] == _REAL_PR_URL
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"state": "OPEN", "statusCheckRollup": [{"conclusion": "SUCCESS"}]}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_gh)

    assert _run("reconcile", "--source", "pr", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["scanned"] == 1  # was 0, forever, before this fix

    _run("queue", "--json")
    row = next(r for r in json.loads(capsys.readouterr().out) if r["id"] == "w")
    assert row["state"] == "ship-ready"
    assert any(e["kind"] == "pr" and e["uri"] == _REAL_PR_URL for e in row["evidence"])


def test_pr_reconcile_skips_item_with_no_task_ref(pm_home: Path, monkeypatch, capsys) -> None:
    """An item with no task_refs at all can't be discovered via `task read` — must be skipped,
    not crash."""
    _run("record", "intake", "--id", "w", "--title", "feature")
    monkeypatch.setattr(reconcile_cmd.task_adapter, "fetch_ticket_attachments", _refuse_to_run)
    capsys.readouterr()
    assert _run("reconcile", "--source", "pr", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["scanned"] == 0


def test_pr_reconcile_never_discovers_when_pr_evidence_already_recorded(
    pm_home: Path, monkeypatch, capsys
) -> None:
    """An item that already carries `pr` evidence (e.g. seeded by `pm record evidence`, or from a
    prior reconcile) must never trigger `task read` — `pr_url_for` already has the answer."""
    _seed_pr_item(task_ref="HYP:HYP-1256")  # with_pr_evidence=True (the default)
    monkeypatch.setattr(reconcile_cmd.task_adapter, "fetch_ticket_attachments", _refuse_to_run)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        ),
    )
    capsys.readouterr()
    assert _run("reconcile", "--source", "pr", "--json") == 0  # never raised via _refuse_to_run


def test_pr_reconcile_discovery_converges_no_repeat_task_read(
    pm_home: Path, monkeypatch, capsys
) -> None:
    """Once a discovered PR url is confirmed by `gh` and recorded as real `pr` evidence, a SECOND
    reconcile run must resolve the item straight from that evidence — never re-shelling
    `task read` for it. This is what actually bounds the discovery cost to "once per item"."""
    _seed_pr_item(task_ref="HYP:HYP-1256", with_pr_evidence=False)
    capsys.readouterr()

    read_calls = []
    monkeypatch.setattr(
        reconcile_cmd.task_adapter,
        "fetch_ticket_attachments",
        lambda ticket_id, cwd="": (read_calls.append(ticket_id), _REAL_ATTACHMENTS)[1],
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        ),
    )

    assert _run("reconcile", "--source", "pr", "--json") == 0
    capsys.readouterr()
    assert read_calls == ["HYP-1256"]  # first run: one discovery shell-out

    monkeypatch.setattr(reconcile_cmd.task_adapter, "fetch_ticket_attachments", _refuse_to_run)
    assert _run("reconcile", "--source", "pr", "--json") == 0  # second run: none — would've raised


def test_pr_reconcile_discover_memoizes_shared_ticket_across_items(
    pm_home: Path, monkeypatch, capsys
) -> None:
    """Two items that both track the SAME ticket ref must trigger exactly ONE `task read` call
    for it — mirroring `fetch_pr_facts`'s per-url memo on the `gh` side."""
    _run("record", "intake", "--id", "a", "--title", "a", "--task-ref", "HYP:HYP-1256")
    _run("record", "intake", "--id", "b", "--title", "b", "--task-ref", "HYP:HYP-1256")
    for item_id in ("a", "b"):
        for s in (
            "classified",
            "ticketed",
            "ready",
            "scoped",
            "queued",
            "dispatched",
            "implementing",
            "reviewing",
            "verifying",
            "pr-open",
        ):
            _run("record", "transition", item_id, s)
    capsys.readouterr()

    read_calls = []
    monkeypatch.setattr(
        reconcile_cmd.task_adapter,
        "fetch_ticket_attachments",
        lambda ticket_id, cwd="": (read_calls.append(ticket_id), _REAL_ATTACHMENTS)[1],
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        ),
    )
    assert _run("reconcile", "--source", "pr", "--json") == 0
    assert read_calls == ["HYP-1256"]  # not one call per item


def test_pr_reconcile_missing_task_binary_still_reconciles_already_evidenced_items(
    pm_home: Path, monkeypatch, capsys
) -> None:
    """A missing `task` CLI (or a bad --repo-dir) makes discovery categorically impossible for
    EVERY item — but must degrade discovery only, not abort the whole reconcile. An item that
    already carries `pr` evidence needs no discovery at all and must still reconcile via `gh`.
    Regression: `_discover_pr_urls` used to let `fetch_ticket_attachments`'s RuntimeError
    propagate straight out of `_run_pr`, killing the run for items that never needed `task`.

    Needs TWO items to actually exercise the catch: one WITHOUT `pr` evidence (so discovery is
    attempted and the mocked `RuntimeError` actually fires — a first version of this test seeded
    only an already-evidenced item, which skips discovery via the `pr_url_for(item)` guard before
    `fetch_ticket_attachments` is ever called, so the regression guard went uncovered) and one
    WITH `pr` evidence (the one this test asserts still reconciles)."""
    _seed_pr_item()  # id "w" — has pr evidence, needs zero discovery
    _run(
        "record",
        "intake",
        "--id",
        "u",
        "--title",
        "undiscoverable",
        "--task-ref",
        "HYP:HYP-1",
    )
    for s in (
        "classified",
        "ticketed",
        "ready",
        "scoped",
        "queued",
        "dispatched",
        "implementing",
        "reviewing",
        "verifying",
        "pr-open",
    ):
        _run("record", "transition", "u", s)

    monkeypatch.setattr(
        reconcile_cmd.task_adapter,
        "fetch_ticket_attachments",
        lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("`task` CLI not found on PATH — install task-cli to discover PR urls")
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        ),
    )
    capsys.readouterr()
    assert _run("reconcile", "--source", "pr", "--json") == 0  # did not abort on "u"'s RuntimeError
    summary = json.loads(capsys.readouterr().out)
    assert summary["scanned"] == 1  # "w" reconciled via gh alone; "u" stayed unresolved, no crash


def test_pr_reconcile_discovers_via_second_task_ref_when_first_has_no_pr(
    pm_home: Path, monkeypatch, capsys
) -> None:
    """An item can aggregate more than one tracked ticket (`task_refs`); the PR-carrying one is
    not guaranteed to be `task_refs[0]`. Regression: discovery used to probe only `task_refs[0]`,
    so a PR linked to any OTHER ref was never found."""
    _run(
        "record",
        "intake",
        "--id",
        "w",
        "--title",
        "feature",
        "--task-ref",
        "HYP:HYP-1",
        "--task-ref",
        "HYP:HYP-1256",
    )
    for s in (
        "classified",
        "ticketed",
        "ready",
        "scoped",
        "queued",
        "dispatched",
        "implementing",
        "reviewing",
        "verifying",
        "pr-open",
    ):
        _run("record", "transition", "w", s)
    capsys.readouterr()

    monkeypatch.setattr(
        reconcile_cmd.task_adapter,
        "fetch_ticket_attachments",
        lambda ticket_id, cwd="": _REAL_ATTACHMENTS if ticket_id == "HYP-1256" else [],
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **k: subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"state": "OPEN", "statusCheckRollup": []}), stderr=""
        ),
    )
    assert _run("reconcile", "--source", "pr", "--json") == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["scanned"] == 1  # found via task_refs[1], not just task_refs[0]
