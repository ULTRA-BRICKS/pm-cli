"""``pm sprint`` end-to-end via the CLI entry point."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from pmlib.cli import main
from pmlib.commands._shared import open_store
from pmlib.sprint import build_sprint_projection

_SPRINT_ID_RE = re.compile(r"^sprint:[0-9a-f]{10}$")


def _run(*argv: str) -> int:
    return main(list(argv))


def _create(capsys, name: str = "S1", goal: str = "") -> str:
    argv = ["sprint", "create", "--name", name]
    if goal:
        argv += ["--goal", goal]
    assert _run(*argv) == 0
    sprint_id = capsys.readouterr().out.strip()
    assert _SPRINT_ID_RE.match(sprint_id)
    return sprint_id


def _item(iid: str, capsys) -> None:
    _run("record", "intake", "--id", iid, "--title", iid)
    capsys.readouterr()


def test_create_prints_a_fresh_sprint_id(pm_home: Path, capsys) -> None:
    _create(capsys, name="Sprint 1", goal="ship the thing")
    assert _run("sprint", "list") == 0
    out = capsys.readouterr().out
    assert "Sprint 1" in out
    assert "planning" in out


def test_add_pulls_an_item_in_with_an_estimate(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    assert _run("sprint", "add", sid, "w1", "--estimate", "3") == 0
    capsys.readouterr()
    assert _run("sprint", "status", sid, "--json") == 0
    status = json.loads(capsys.readouterr().out)
    assert "items" not in status["sprint"]  # superseded by "rows" — never both in the same payload
    assert status["rows"][0]["item_id"] == "w1"
    assert status["rows"][0]["estimate"] == 3.0


def test_add_rejects_unknown_work_item(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    rc = _run("sprint", "add", sid, "ghost")
    assert rc != 0
    assert "ghost" in capsys.readouterr().err.lower()


def test_add_rejects_non_finite_estimate(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    capsys.readouterr()
    rc = _run("sprint", "add", sid, "w1", "--estimate", "nan")
    assert rc != 0
    assert "finite" in capsys.readouterr().err.lower()


def test_add_rejects_negative_estimate(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    capsys.readouterr()
    rc = _run("sprint", "add", sid, "w1", "--estimate", "-3")
    assert rc != 0
    assert "negative" in capsys.readouterr().err.lower()
    # Rejected at the write boundary -- no event should have landed, so the sprint stays empty.
    assert _run("sprint", "start", sid) != 0


def test_add_rejects_an_estimate_that_would_make_the_total_non_finite(
    pm_home: Path, capsys
) -> None:
    # Regression: two individually-finite huge estimates can still sum to `inf` -- a per-item
    # finiteness check alone doesn't protect the sprint's aggregate total.
    sid = _create(capsys)
    _item("w1", capsys)
    _item("w2", capsys)
    capsys.readouterr()
    assert _run("sprint", "add", sid, "w1", "--estimate", "1e308") == 0
    capsys.readouterr()
    rc = _run("sprint", "add", sid, "w2", "--estimate", "1e308")
    assert rc != 0
    assert "non-finite" in capsys.readouterr().err.lower()


def test_sprint_name_with_terminal_escape_is_rendered_safely(pm_home: Path, capsys) -> None:
    # Regression: a foreign log entry's `name`/`goal`/retro text could contain raw ANSI/OSC
    # control sequences (e.g. `\x1b]52;c;...\x07` for clipboard hijacking) and was printed
    # verbatim to a real terminal on `sprint list`/`status`/`retro`. The raw ESC byte must never
    # reach stdout in the human-readable view.
    evil_name = "S1\x1b]0;pwned\x07"
    assert _run("sprint", "create", "--name", evil_name) == 0
    capsys.readouterr()
    assert _run("sprint", "list") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\\x1b" in out  # the byte survives as a visible, inert escape, not silently dropped


def test_sprint_name_with_c1_terminal_escape_is_rendered_safely(pm_home: Path, capsys) -> None:
    # Regression: display_safe's control-char sweep only covered ASCII C0 controls + DEL -- some
    # terminals also recognize the 8-bit (Unicode C1, U+0080-U+009F) form of the SAME OSC/ANSI
    # sequences, e.g. U+009D (single-byte OSC start) ... U+009C (single-byte ST end), which
    # passed through unescaped.
    c1_start, c1_end = chr(0x9D), chr(0x9C)
    evil_name = "S1" + c1_start + "52;c;pwned" + c1_end
    assert _run("sprint", "create", "--name", evil_name) == 0
    capsys.readouterr()
    assert _run("sprint", "list") == 0
    out = capsys.readouterr().out
    assert c1_start not in out
    assert c1_end not in out
    assert "\\x9d" in out  # survives as a visible, inert escape, not silently dropped


def test_sprint_name_with_carriage_return_injection_is_rendered_safely(
    pm_home: Path, capsys
) -> None:
    # Regression: the C0 control-char sweep's ranges (\x00-\x08, \x0b\x0c, \x0e-\x1f) had a gap
    # at \x0d (CR) -- carriage return isn't tab/newline (the two controls this module explicitly
    # lets pass through for prose) and reaches a real terminal, which overwrites the start of the
    # current line, letting a hand-edited log entry spoof `sprint list`/`status` output.
    evil_name = "legit" + "\r" + "FORGED"
    assert _run("sprint", "create", "--name", evil_name) == 0
    capsys.readouterr()
    assert _run("sprint", "list") == 0
    out = capsys.readouterr().out
    assert "\r" not in out
    assert "\\r" in out  # survives as a visible, inert escape, not silently dropped


def test_sprint_name_with_lone_surrogate_is_rendered_safely(pm_home: Path, capsys) -> None:
    # Regression: a hand-edited/foreign JSONL log line can carry an ESCAPED lone surrogate
    # (`"name":"\ud800"` as literal text) -- json.loads happily decodes that into a Python str
    # holding the real surrogate code point (Python tolerates lone surrogates internally, even
    # though they can't round-trip through json.dumps(ensure_ascii=False), which is why this
    # event is written as a raw line rather than via EventStore.append()/new_event(), the same
    # way a foreign writer's hand-edited line would arrive). display_safe's old control-char
    # sweep didn't touch surrogates, so printing this to a real UTF-8 terminal raised
    # UnicodeEncodeError, taking down `sprint list`/`status`/`retro` entirely on one malformed
    # record instead of rendering it safely.
    store = open_store()
    store.directory.mkdir(parents=True, exist_ok=True)
    raw_line = (
        '{"id":"evilsurrogate0000000000000000","ts":"2026-08-30T00:00:00.000000Z",'
        '"kind":"sprint_created","item_id":"sprint:evil-surrogate",'
        '"payload":{"name":"legit\\ud800FORGED"}}'
    )
    with store.log_path.open("a", encoding="utf-8") as fh:
        fh.write(raw_line + "\n")
    capsys.readouterr()

    assert _run("sprint", "list") == 0  # must not raise UnicodeEncodeError
    out = capsys.readouterr().out
    assert chr(0xD800) not in out
    assert "\\ud800" in out  # survives as a visible, inert escape, not silently dropped


def test_display_safe_covers_low_surrogate_and_leaves_non_ascii_untouched() -> None:
    # Companion to the lone-surrogate CLI regression above: the escaped range must be a single
    # contiguous \ud800-\udfff (both the high AND low surrogate halves), not just the range's
    # endpoints -- and it must not clobber ordinary non-ASCII prose, which sits well outside the
    # C0/C1/surrogate ranges entirely.
    from pmlib.commands._shared import display_safe

    assert display_safe("legit" + chr(0xDC00) + "FORGED") == "legit\\udc00FORGED"
    assert display_safe("café 日本 Привет") == "café 日本 Привет"


def test_sprint_and_item_ids_with_terminal_escapes_are_rendered_safely(
    pm_home: Path, capsys
) -> None:
    # Regression: the CLI only ever mints `sprint:<uuid4>` ids and rejects unknown work-item ids,
    # but a foreign/hand-edited log entry isn't bound by that -- `_apply_created`/
    # `_apply_item_added` accept any nonempty string as a real id, and `sprint status`/`list`
    # printed those ids raw (only name/goal/notes were sanitized, missing the id fields entirely).
    from pmlib.events import EventKind, new_event

    evil_sprint_id = "sprint:\x1b]52;c;evil\x07"
    evil_item_id = "w\x1b[2Jpwned"
    open_store().append(new_event(EventKind.SPRINT_CREATED, evil_sprint_id, {"name": "S"}))
    open_store().append(
        new_event(EventKind.SPRINT_ITEM_ADDED, evil_sprint_id, {"work_item_id": evil_item_id})
    )
    capsys.readouterr()

    assert _run("sprint", "list") == 0
    list_out = capsys.readouterr().out
    assert "\x1b" not in list_out
    assert "\\x1b" in list_out

    assert _run("sprint", "status", evil_sprint_id) == 0
    status_out = capsys.readouterr().out
    assert "\x1b" not in status_out
    assert "\\x1b" in status_out


def test_lifecycle_error_paths_render_evil_sprint_id_safely(pm_home: Path, capsys) -> None:
    # Regression: `display_safe` was applied to `list`/`status`'s id fields first, but 3 error/
    # success paths in `start`/`close`/`retro` still echoed `args.sprint_id` raw -- found across
    # multiple review rounds one sink at a time. Cover every lifecycle command's own id echo in
    # one test so a future sink doesn't need yet another dedicated regression.
    from pmlib.events import EventKind, new_event

    evil_sprint_id = "sprint:\x1b]52;c;evil\x07"
    open_store().append(new_event(EventKind.SPRINT_CREATED, evil_sprint_id, {"name": "S"}))
    capsys.readouterr()

    # `start` on an empty sprint -- exercises the "has no items" error echo.
    assert _run("sprint", "start", evil_sprint_id) != 0
    assert "\x1b" not in capsys.readouterr().err

    # `close` on a still-planning sprint -- exercises the "not active" error echo.
    assert _run("sprint", "close", evil_sprint_id) != 0
    assert "\x1b" not in capsys.readouterr().err

    # `retro --note` on a still-planning (not closed) sprint -- exercises that error echo too.
    assert _run("sprint", "retro", evil_sprint_id, "--note", "x") != 0
    assert "\x1b" not in capsys.readouterr().err


def test_pm_reminders_renders_sprint_id_and_detail_safely(pm_home: Path, capsys) -> None:
    # Regression: `pm reminders`' plaintext table printed `r.item_id`/`r.detail` raw -- the SAME
    # sink existed before this PR for WorkItem reminders, but sprint reminders (new here) flow
    # through it too, and a foreign sprint id reaches it unescaped either way.
    from pmlib.events import Event, EventKind, new_event

    evil_sprint_id = "sprint:\x1b]52;c;evil\x07"
    fresh = new_event(EventKind.SPRINT_CREATED, evil_sprint_id, {"name": "Old"})
    old = Event(
        id=fresh.id,
        ts="2000-01-01T00:00:00.000000Z",
        kind=fresh.kind,
        item_id=fresh.item_id,
        payload=fresh.payload,
    )
    open_store().append(old)
    capsys.readouterr()
    assert _run("reminders") == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "\\x1b" in out


def test_sprint_status_renders_a_foreign_work_item_state_safely(pm_home: Path, capsys) -> None:
    # Regression: `sprint status`'s per-item row printed the LIVE WorkItem state raw. A normal
    # `record transition` is rejected by the state machine for an unknown target, but `intake`'s
    # own fold (`_apply_intake` in projection.py) assigns `state=str(payload.get("state") or
    # ...)` with NO such validation -- a foreign intake event can seed an item at an arbitrary
    # "state" from day one, and that value joins straight into this sprint view.
    from pmlib.events import EventKind, new_event

    sid = _create(capsys)
    capsys.readouterr()
    open_store().append(
        new_event(EventKind.INTAKE, "w1", {"title": "w1", "state": "\x1b]0;pwned\x07"})
    )
    _run("sprint", "add", sid, "w1")
    capsys.readouterr()
    assert _run("sprint", "status", sid) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out


def test_retro_note_before_close_is_rejected(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "retro", sid, "--note", "too early")  # still `planning`
    assert rc != 0
    assert "closed" in capsys.readouterr().err.lower()


def test_whitespace_only_retro_note_is_treated_as_no_note(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "   ") == 0  # falls through to view mode
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    # a whitespace-only "note" must NOT have permanently silenced the retro nudge
    assert any(r["kind"] == "sprint-needs-retro" and r["item_id"] == sid for r in reminders)


def test_add_rejects_unknown_sprint(pm_home: Path, capsys) -> None:
    _item("w1", capsys)
    rc = _run("sprint", "add", "sprint:ghostghost", "w1")
    assert rc != 0


def test_start_requires_at_least_one_item(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "start", sid)
    assert rc != 0
    assert "no items" in capsys.readouterr().err.lower()


def test_start_freezes_scope(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _item("w2", capsys)
    _run("sprint", "add", sid, "w1")
    capsys.readouterr()
    assert _run("sprint", "start", sid) == 0
    rc = _run("sprint", "add", sid, "w2")
    assert rc != 0  # scope frozen — w2 must be rejected, not silently added


def test_concurrent_add_and_start_never_silently_drops_a_reported_success(
    pm_home: Path, capsys
) -> None:
    """Regression for the read-validate-append race: two processes racing `sprint add` against
    `sprint start` on the same sprint must never BOTH report success (rc 0) while the store ends
    up missing one side's effect — that was the exact bug (`add` prints success, then `start`
    lands first in log order, and the projection silently discards the now-lifecycle-invalid add).
    With the store's `lock()` serializing the whole read-validate-append sequence, whichever
    command's lock-acquisition wins must see (and correctly react to) the other's completed
    effect, not a stale read. Uses a FRESH sprint per trial so every repetition is a real,
    independent race (reusing one sprint would make every trial after the first `start` a no-op,
    since the sprint is no longer `planning`).
    """
    _item("w1", capsys)
    _item("w2", capsys)
    capsys.readouterr()

    for _ in range(20):  # repeat to raise the odds of hitting either interleaving
        sid = _create(capsys)
        _run("sprint", "add", sid, "w1")  # one item in already, so `start` alone is always legal
        capsys.readouterr()

        results: dict[str, int] = {}

        def _do_add(trial_results: dict[str, int], sprint_id: str) -> None:
            trial_results["add"] = _run("sprint", "add", sprint_id, "w2")

        def _do_start(trial_results: dict[str, int], sprint_id: str) -> None:
            trial_results["start"] = _run("sprint", "start", sprint_id)

        t_add = threading.Thread(target=_do_add, args=(results, sid))
        t_start = threading.Thread(target=_do_start, args=(results, sid))
        t_add.start()
        t_start.start()
        t_add.join()
        t_start.join()
        capsys.readouterr()

        events = open_store().read_all()
        sprint = build_sprint_projection(events)[sid]
        # The invariant the bug violated: if `add` reported success, `w2` must actually be a
        # scoped item of the sprint — never "printed 0 but the event got discarded by the fold".
        if results["add"] == 0:
            assert "w2" in sprint.item_ids(), (
                "sprint add reported success but w2 is missing from the sprint "
                "-- the lock did not actually serialize against the concurrent start"
            )
        # And the flip side: if `start` reported success, the sprint must actually be `active` —
        # never a phantom success on a sprint the lock let something else mutate underneath it.
        if results["start"] == 0:
            assert sprint.status != "planning"


def test_close_then_reclose_is_an_error(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    capsys.readouterr()
    assert _run("sprint", "close", sid) == 0
    rc = _run("sprint", "close", sid)
    assert rc != 0


def test_close_from_planning_is_rejected(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    rc = _run("sprint", "close", sid)  # never started
    assert rc != 0
    assert "active" in capsys.readouterr().err.lower()


def test_reminders_surfaces_stale_planning_end_to_end(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    capsys.readouterr()
    # Force the sprint's `updated_at` far enough into the past by writing the event directly
    # (the CLI has no "backdate" flag, and this exercises the real fold + command path, not a
    # hand-built Sprint object like the unit tests do).
    from pmlib.events import Event, EventKind, new_event

    fresh = new_event(EventKind.SPRINT_CREATED, "backdated", {"name": "Old"})
    old = Event(
        id=fresh.id,
        ts="2000-01-01T00:00:00.000000Z",
        kind=fresh.kind,
        item_id=fresh.item_id,
        payload=fresh.payload,
    )
    open_store().append(old)
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    kinds = {(r["kind"], r["item_id"]) for r in reminders}
    assert ("sprint-stale-planning", "backdated") in kinds
    assert ("sprint-stale-planning", sid) not in kinds  # freshly created — not stale yet


def test_reminders_surfaces_ready_to_close_end_to_end(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    for target in (
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
        "ci-pending",
        "ship-ready",
    ):
        _run("record", "transition", "w1", target)
    _run("record", "evidence", "w1", "deployment", "https://deploy/w1")
    _run("record", "transition", "w1", "deployed")
    _run("record", "transition", "w1", "done")
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "sprint-ready-to-close" and r["item_id"] == sid for r in reminders)


def test_retro_note_then_view(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "went well") == 0
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--json") == 0
    body = json.loads(capsys.readouterr().out)
    assert body["sprint"]["retro_notes"] == ["went well"]


def test_retro_note_with_json_emits_json_not_plaintext(pm_home: Path, capsys) -> None:
    # Regression: `--note ... --json` (recording a note, not just viewing) used to always print
    # plaintext, breaking `pm sprint retro <id> --note "..." --json | jq` despite the flag being
    # accepted -- the successful-mutation path must honor `--json` exactly like the view path.
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "went well", "--json") == 0
    out = capsys.readouterr().out
    body = json.loads(out)  # must not raise -- a plaintext line here would fail this parse
    assert body["sprint"]["retro_notes"] == ["went well"]


def test_retro_note_with_json_reports_a_fresh_updated_at_not_stale(pm_home: Path, capsys) -> None:
    # Regression: the retro-mutation --json response patched retro_notes into the in-memory
    # projection but left `updated_at` at its pre-retro value -- a JSON consumer using
    # `updated_at` as a change cursor would see a STALE timestamp here, then a NEWER one on the
    # very next `sprint status --json` call for the exact same mutation, with no event in
    # between, breaking any "did anything change" comparison built on that field.
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    capsys.readouterr()
    assert _run("sprint", "status", sid, "--json") == 0
    before_close = json.loads(capsys.readouterr().out)["sprint"]["updated_at"]
    assert _run("sprint", "close", sid) == 0
    capsys.readouterr()
    assert _run("sprint", "retro", sid, "--note", "went well", "--json") == 0
    retro_response = json.loads(capsys.readouterr().out)
    assert _run("sprint", "status", sid, "--json") == 0
    after = json.loads(capsys.readouterr().out)["sprint"]
    assert retro_response["sprint"]["updated_at"] == after["updated_at"]
    assert retro_response["sprint"]["updated_at"] != before_close


def test_status_reflects_live_item_state_not_a_stale_copy(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    capsys.readouterr()
    _run("sprint", "status", sid, "--json")
    before = json.loads(capsys.readouterr().out)
    assert before["rows"][0]["done"] is False
    # advance the underlying work item all the way to done, without touching the sprint at all
    for target in (
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
        "ci-pending",
        "ship-ready",
    ):
        _run("record", "transition", "w1", target)
    _run("record", "evidence", "w1", "deployment", "https://deploy/w1")
    _run("record", "transition", "w1", "deployed")
    _run("record", "transition", "w1", "done")
    capsys.readouterr()
    _run("sprint", "status", sid, "--json")
    after = json.loads(capsys.readouterr().out)
    assert after["rows"][0]["done"] is True  # the join picked up the live state, no drift


def test_reminders_surfaces_needs_retro(pm_home: Path, capsys) -> None:
    sid = _create(capsys)
    _item("w1", capsys)
    _run("sprint", "add", sid, "w1")
    _run("sprint", "start", sid)
    _run("sprint", "close", sid)
    capsys.readouterr()
    assert _run("reminders", "--json") == 0
    reminders = json.loads(capsys.readouterr().out)
    assert any(r["kind"] == "sprint-needs-retro" and r["item_id"] == sid for r in reminders)
