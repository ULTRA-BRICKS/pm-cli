"""The audit collector end to end through fakes: coverage math, caching, stops, budget, privacy."""

from __future__ import annotations

import itertools
import json

import pytest

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_decision as dec
from pmlib.chatgpt_control import audit_report as report_mod
from pmlib.chatgpt_control import audit_validation as val
from pmlib.chatgpt_control.audit_collector import run_audit
from pmlib.chatgpt_control.audit_guard import Limits, LockBusy, RunLock
from pmlib.chatgpt_control.audit_reader import (
    ReaderHttpError,
    ReaderTimeout,
    ReaderUnavailable,
)
from tests.audit_collector_support import (
    ALLOWED_CALLS,
    BENCH,
    GLIDE_HASH,
    MODEL,
    SENTINEL_FINAL,
    SENTINEL_REQUEST,
    FakeClassifier,
    FakeReader,
    conversation_payload,
    invalid_classification,
    listing_item,
    project_id,
    rig,
    simple_account,
    unavailable_classification,
)
from tests.audit_support import NOW, laya

DENIED = {"allowed": False, "reason": "classification-not-validated"}
DAY = 86400.0


def account_of(report, name="home"):
    return report["accounts"][name]


def test_full_run_paginates_every_scope_and_reports_exact_coverage(tmp_path):
    recent = [listing_item(f"r{i}", 1000 + 10 * i) for i in range(20)]
    recent += [listing_item(f"o{i}", 3 * DAY + i) for i in range(380)]
    p1 = project_id(1)
    scopes = {
        "recent": recent,
        "recent:tpp": recent[:5],
        "recent:non-tpp": recent[5:20],
        "archived": [
            listing_item("a0", 2000, archived=True),
            listing_item("a1", 2100, archived=True),
            listing_item("a2", 4 * DAY, archived=True),
        ],
        "archived:tpp": [listing_item("a0", 2000, archived=True)],
        "archived:non-tpp": [
            listing_item("a1", 2100, archived=True),
            listing_item("a2", 4 * DAY, archived=True),
        ],
        f"project:{p1}": [
            listing_item("pc0", 3000, project=p1),
            listing_item("pc1", 5 * DAY, project=p1),
            listing_item("r3", 1030, project=p1),
        ],
    }
    reader = FakeReader(scopes=scopes, projects=[p1])
    r = rig(tmp_path, {"home": reader})

    report = r.run(days=1.0)

    acct = account_of(report)
    assert acct["coverage"] == {
        "listed_total": 205,  # 200 recent (2 pages) + a0..a2 + pc0 + pc1
        "in_window": 23,  # r0..r19, a0, a1, pc0
        "read": 23,
        "cached": 0,
        "skipped": {},
        "errors": 0,
        "partial": False,
        "budget_exhausted": False,
        "stopped": None,
        "requests": 33,  # identity + 9 listing pages + 23 reads
    }
    assert acct["scopes"] == {
        "recent": {"listed": 200, "pages": 2, "complete": True, "reason": "", "total": 400},
        "recent:tpp": {"listed": 5, "pages": 1, "complete": True, "reason": "", "total": 5},
        "recent:non-tpp": {"listed": 15, "pages": 1, "complete": True, "reason": "", "total": 15},
        "archived": {"listed": 3, "pages": 1, "complete": True, "reason": "", "total": 3},
        "archived:tpp": {"listed": 1, "pages": 1, "complete": True, "reason": "", "total": 1},
        "archived:non-tpp": {"listed": 2, "pages": 1, "complete": True, "reason": "", "total": 2},
        "projects": {"listed": 1, "pages": 1, "complete": True, "reason": "", "total": 1},
        f"project:{p1}": {"listed": 3, "pages": 1, "complete": True, "reason": "", "total": 3},
    }
    assert acct["status"] == "ok" and acct["reason"] == "" and report["status"] == "ok"
    assert acct["observed_account"] == "home"
    assert len(reader.called("list_page")) == 8  # recent stopped at the cutoff after page 2
    assert len(acct["conversations"]) == 23
    by_id = {c["conversation_id"]: c for c in acct["conversations"]}
    assert by_id["a0"]["archived"] and by_id["a1"]["archived"] and not by_id["r0"]["archived"]
    assert by_id["pc0"]["project_id"] == p1
    assert report["totals"] == {
        "conversations": 23,
        "done": 23,
        "incomplete": 0,
        "blocked": 0,
        "working": 0,
        "unknown": 0,
    }
    assert report["cutoff"] == report["generated_at"] - DAY
    assert report["auto_continue"] == DENIED
    assert reader.closed


def test_cutoff_stop_does_not_fetch_pages_beyond_an_entirely_old_page(tmp_path):
    items = [listing_item(f"n{i}", 600 + i) for i in range(100)]
    items += [listing_item(f"x{i}", 9 * DAY + i) for i in range(300)]
    reader = FakeReader(scopes={"recent": items})
    report = rig(tmp_path, {"home": reader}).run(days=1.0)
    scope = account_of(report)["scopes"]["recent"]
    assert (scope["pages"], scope["complete"], scope["listed"]) == (2, True, 200)
    offsets = [c[2] for c in reader.called("list_page") if c[1] == "recent"]
    assert offsets == [0, 100]


def test_a_conversation_listed_in_several_scopes_is_counted_and_read_once(tmp_path):
    shared = listing_item("dup", 700)
    reader = FakeReader(
        scopes={"recent": [shared], "recent:tpp": [shared], "recent:non-tpp": [shared]}
    )
    report = rig(tmp_path, {"home": reader}).run()
    cov = account_of(report)["coverage"]
    assert (cov["listed_total"], cov["in_window"], cov["read"]) == (1, 1, 1)
    assert reader.called("read_conversation") == [("read_conversation", "dup", 4)]


def test_unpaged_totals_still_paginate_until_a_short_page(tmp_path):
    items = [listing_item(f"c{i}", 600 + i) for i in range(250)]
    reader = FakeReader(scopes={"recent": items}, report_total=False)
    report = rig(tmp_path, {"home": reader}).run()
    scope = account_of(report)["scopes"]["recent"]
    assert (scope["pages"], scope["listed"], scope["total"], scope["complete"]) == (
        3,
        250,
        None,
        True,
    )


def test_page_cap_marks_the_scope_incomplete_and_the_account_partial(tmp_path):
    items = [listing_item(f"c{i}", 600 + i) for i in range(5200)]
    reader = FakeReader(scopes={"recent": items})
    report = rig(tmp_path, {"home": reader}).run()
    acct = account_of(report)
    scope = acct["scopes"]["recent"]
    assert (scope["pages"], scope["complete"], scope["reason"]) == (50, False, "page-cap")
    assert acct["status"] == "partial" and report["status"] == "partial"


def test_archived_chats_are_inspected_but_never_mutated(tmp_path):
    reader = FakeReader(
        scopes={"archived": [listing_item("arch", 900, archived=True)]},
    )
    report = rig(tmp_path, {"home": reader}).run()
    entry = account_of(report)["conversations"][0]
    assert entry["conversation_id"] == "arch" and entry["archived"] is True
    assert entry["url"] == "https://chatgpt.com/c/arch"
    assert {c[0] for c in reader.calls} <= ALLOWED_CALLS
    assert reader.called("read_conversation")


def test_unchanged_conversations_are_reused_without_reading_and_reverified(tmp_path):
    reader = simple_account(n=3)
    r = rig(tmp_path, {"home": reader})
    first = r.run()
    assert account_of(first)["coverage"]["read"] == 3
    reads_before, calls_before = len(reader.called("read_conversation")), len(r.classifier.seen)

    r.clock.t += 900
    second = r.run()

    acct = account_of(second)
    assert acct["coverage"]["cached"] == 3 and acct["coverage"]["read"] == 0
    assert acct["status"] == "ok"
    assert len(reader.called("read_conversation")) == reads_before
    assert len(r.classifier.seen) == calls_before
    assert {c["source"] for c in acct["conversations"]} == {"cache"}
    assert {c["verified_at"] for c in acct["conversations"]} == {r.clock.t}
    assert r.store.get("home", "c0").verified_at == r.clock.t


def test_a_changed_updated_at_forces_a_re_read(tmp_path):
    reader = simple_account(n=2)
    r = rig(tmp_path, {"home": reader})
    r.run()
    r.clock.t += 60
    reader.scopes["recent"][0] = listing_item("c0", 300)
    report = r.run()
    cov = account_of(report)["coverage"]
    assert (cov["read"], cov["cached"]) == (1, 1)
    assert [c for c in reader.called("read_conversation") if c[1] == "c0"].__len__() == 2


def test_a_rewritten_conversation_with_the_same_fingerprint_skips_the_classifier(tmp_path):
    reader = simple_account(n=1)
    r = rig(tmp_path, {"home": reader})
    r.run()
    assert len(r.classifier.seen) == 1
    reader.scopes["recent"][0] = listing_item("c0", 100)  # renamed: new updated_at, same messages
    report = r.run()
    acct = account_of(report)
    assert acct["coverage"]["read"] == 1
    assert len(r.classifier.seen) == 1
    assert acct["conversations"][0]["task_state"] == "done"
    assert acct["conversations"][0]["classifier_status"] == "ok"


def test_a_scored_verdict_is_reused_only_under_a_profile_that_names_its_checkpoint(tmp_path):
    english = "convaiinnovations/laya@512"
    multilingual = "convaiinnovations/laya-multilingual@1024"
    both = f"{multilingual}|{english}"
    reader = simple_account(n=1)
    first = rig(
        tmp_path, {"home": reader}, classifier=FakeClassifier(laya(model=english)), model=both
    )
    first.run()
    assert first.classifier.seen == ["c0"]
    reader.scopes["recent"][0] = listing_item("c0", 100)  # same messages, new updated_at
    same = rig(
        tmp_path, {"home": reader}, classifier=FakeClassifier(laya(model=english)), model=both
    )
    same.run()
    assert same.classifier.seen == []  # the profile still names the stored checkpoint
    reader.scopes["recent"][0] = listing_item("c0", 90)
    other = rig(
        tmp_path,
        {"home": reader},
        classifier=FakeClassifier(laya(model=english)),
        model=multilingual,
    )
    other.run()
    assert other.classifier.seen == ["c0"]  # another checkpoint: scored again, never reused


def test_working_and_unknown_entries_are_always_re_read(tmp_path):
    working = {"w": conversation_payload("w", age=300, in_progress=True)}
    reader = FakeReader(
        scopes={"recent": [listing_item("w", 300), listing_item("u", 400)]},
        payloads={**working, "u": conversation_payload("u", age=400)},
    )
    r = rig(tmp_path, {"home": reader}, classifier=FakeClassifier(unavailable_classification()))
    first = r.run()
    states = {c["conversation_id"]: c["task_state"] for c in account_of(first)["conversations"]}
    assert states == {"w": "working", "u": "unknown"}
    second = r.run()
    cov = account_of(second)["coverage"]
    assert (cov["read"], cov["cached"]) == (2, 0)


def test_a_threshold_change_invalidates_the_cache(tmp_path):
    reader = simple_account(n=2)
    rig(tmp_path, {"home": reader}).run()
    other = rig(tmp_path, {"home": reader}, thresholds=dec.Thresholds(done_min=0.9))
    report = other.run()
    cov = account_of(report)["coverage"]
    assert (cov["read"], cov["cached"]) == (2, 0)


def test_a_prompt_version_change_invalidates_the_cache(tmp_path, monkeypatch):
    reader = simple_account(n=2)
    r = rig(tmp_path, {"home": reader})
    r.run()
    monkeypatch.setattr(clf, "PROMPT_VERSION", "completion-v999")
    cov = account_of(r.run())["coverage"]
    assert (cov["read"], cov["cached"]) == (2, 0)


def test_a_model_change_invalidates_model_dependent_entries_only(tmp_path):
    deterministic = FakeReader(
        scopes={"recent": [listing_item("c0", 700), listing_item("q", 800)]},
        payloads={
            "c0": conversation_payload("c0", age=700),
            "q": conversation_payload("q", age=800, answered=False),  # no answer: rule-based
        },
    )
    r = rig(tmp_path, {"home": deterministic})
    first = account_of(r.run())
    states = {c["conversation_id"]: c["task_state"] for c in first["conversations"]}
    assert states == {"c0": "done", "q": "incomplete"}
    changed = rig(tmp_path, {"home": deterministic}, model="laya-0.24.0")
    cov = account_of(changed.run())["coverage"]
    assert (cov["read"], cov["cached"]) == (1, 1)  # only the model-dependent entry is re-read


def test_requests_are_spaced_at_least_four_seconds_across_accounts(tmp_path):
    home = simple_account(n=2)
    glide = simple_account(n=2, account_hash=GLIDE_HASH, prefix="g")
    r = rig(tmp_path, {"home": home, "glide.vc": glide})
    r.run()
    times = sorted(home.times + glide.times)
    assert len(times) > 10
    assert all(b - a >= 4.0 - 1e-9 for a, b in itertools.pairwise(times))


def test_a_429_before_the_first_page_makes_the_account_unavailable_and_is_remembered(tmp_path):
    reader = simple_account(n=2, raises={("list_page", "recent"): ReaderHttpError(429, "120")})
    other = simple_account(n=1, account_hash=GLIDE_HASH, prefix="g")
    r = rig(tmp_path, {"home": reader, "glide.vc": other})

    report = r.run()

    acct = account_of(report)
    assert acct["status"] == "unavailable" and acct["reason"] == "http-429"
    assert acct["coverage"]["stopped"] == {"status": 429, "not_before": NOW + 120.0 + 4.0}
    assert acct["not_before"] == NOW + 124.0
    assert not reader.called("read_conversation")
    assert len(reader.called("list_page")) == 1
    assert account_of(report, "glide.vc")["status"] == "ok"
    assert report["status"] == "unavailable"

    reader.calls.clear()
    again = r.run(workspaces=("home",))
    skipped = account_of(again)
    assert skipped["status"] == "unavailable" and skipped["reason"] == "not-before"
    assert skipped["not_before"] == NOW + 124.0
    assert reader.calls == []


@pytest.mark.parametrize("status", [401, 403])
def test_a_stop_status_midway_is_partial_and_stops_all_further_reads(tmp_path, status):
    reader = simple_account(n=3, raises={("list_page", "archived"): ReaderHttpError(status, None)})
    report = rig(tmp_path, {"home": reader}).run()
    acct = account_of(report)
    assert acct["status"] == "partial" and acct["reason"] == f"http-{status}"
    assert acct["coverage"]["stopped"]["status"] == status
    assert acct["coverage"]["in_window"] == 3
    assert acct["coverage"]["skipped"] == {"stopped": 3}
    assert acct["coverage"]["read"] == 0
    assert not reader.called("read_conversation")
    assert acct["conversations"] == []
    assert reader.called("list_page")[-1][1] == "archived"  # nothing after the stop


def test_budget_exhaustion_during_reads_is_partial_never_ok(tmp_path):
    reader = simple_account(n=10)
    r = rig(tmp_path, {"home": reader}, limits=Limits(budget_seconds=60))
    report = r.run()
    acct = account_of(report)
    cov = acct["coverage"]
    assert acct["status"] == "partial" and acct["reason"] == "budget-exhausted"
    assert cov["budget_exhausted"] is True and cov["partial"] is True
    assert 0 < cov["read"] < 10
    assert cov["read"] + cov["cached"] + sum(cov["skipped"].values()) == cov["in_window"] == 10
    assert cov["skipped"] == {"budget-exhausted": 10 - cov["read"]}
    assert report["status"] == "partial"


def test_budget_exhaustion_during_listing_is_partial(tmp_path):
    reader = simple_account(n=3)
    r = rig(tmp_path, {"home": reader}, limits=Limits(budget_seconds=10))
    acct = account_of(r.run())
    assert acct["status"] == "partial" and acct["coverage"]["budget_exhausted"] is True
    incomplete = [s for s in acct["scopes"].values() if not s["complete"]]
    assert incomplete and incomplete[0]["reason"] == "budget-exhausted"


def test_an_account_the_budget_never_reached_is_unavailable_not_empty(tmp_path):
    home = simple_account(n=10)
    glide = simple_account(n=2, account_hash=GLIDE_HASH, prefix="g")
    r = rig(tmp_path, {"home": home, "glide.vc": glide}, limits=Limits(budget_seconds=20))
    report = r.run()
    assert account_of(report, "glide.vc")["status"] == "unavailable"
    assert account_of(report, "glide.vc")["reason"] == "budget-exhausted"
    assert glide.calls == []


def test_request_errors_are_counted_and_make_the_account_partial(tmp_path):
    reader = simple_account(
        n=3,
        raises={
            ("read_conversation", "c1"): ReaderHttpError(500, None),
            ("list_page", "archived"): ReaderTimeout("slow"),
        },
    )
    report = rig(tmp_path, {"home": reader}).run()
    acct = account_of(report)
    cov = acct["coverage"]
    assert acct["status"] == "partial"
    assert cov["errors"] == 2
    assert cov["skipped"] == {"http-500": 1}
    assert (cov["in_window"], cov["read"]) == (3, 2)
    assert acct["scopes"]["archived"] == {
        "listed": 0,
        "pages": 0,
        "complete": False,
        "reason": "timeout",
        "total": None,
    }
    assert acct["scopes"]["archived:tpp"]["complete"] is True  # later scopes still ran
    assert {c["conversation_id"] for c in acct["conversations"]} == {"c0", "c2"}


def test_a_missing_project_list_endpoint_is_unverified_never_full_scope(tmp_path):
    reader = simple_account(n=2, raises={("list_projects", 0): ReaderHttpError(404, None)})
    acct = account_of(rig(tmp_path, {"home": reader}).run())
    scope = acct["scopes"]["projects"]
    assert (scope["complete"], scope["reason"]) == (False, "unverified")
    assert acct["status"] == "partial" and acct["reason"] == "unverified"
    assert acct["coverage"]["read"] == 2  # the chats that were listed are still audited


def test_a_held_run_lock_means_busy_and_nothing_is_read(tmp_path):
    reader = simple_account(n=2)
    r = rig(tmp_path, {"home": reader})
    with RunLock(r.env.lock_path), pytest.raises(LockBusy):
        r.run()
    assert reader.calls == []


def test_an_unavailable_reader_is_reported_with_its_reason_and_no_reads(tmp_path):
    def broken(account):
        raise ReaderUnavailable("Playwright not installed")

    report = rig(tmp_path, {"home": simple_account()}, factory=broken).run()
    acct = account_of(report)
    assert acct["status"] == "unavailable" and acct["reason"] == "playwright-not-installed"
    assert acct["conversations"] == [] and acct["coverage"]["listed_total"] == 0
    assert report["status"] == "unavailable" and report["totals"]["conversations"] == 0


def test_an_unbound_account_is_never_audited(tmp_path):
    reader = simple_account()
    report = rig(tmp_path, {"home": reader}, bound=()).run()
    acct = account_of(report)
    assert (acct["status"], acct["reason"]) == ("unavailable", "account-not-bound")
    assert [c[0] for c in reader.calls] == ["identity"]


@pytest.mark.parametrize("hash_value", [GLIDE_HASH, "d" * 64])
def test_a_mismatched_session_is_never_audited_as_the_requested_account(tmp_path, hash_value):
    reader = simple_account(account_hash=hash_value)
    report = rig(tmp_path, {"home": reader}).run()
    acct = account_of(report)
    assert (acct["status"], acct["reason"]) == ("unavailable", "account-mismatch")
    assert [c[0] for c in reader.calls] == ["identity"]
    assert acct["conversations"] == []


def test_a_classifier_outage_costs_one_call_and_yields_unknown_never_done(tmp_path):
    reader = simple_account(n=3)
    r = rig(
        tmp_path,
        {"home": reader},
        classifier=FakeClassifier(unavailable_classification()),
        model="",
    )
    report = r.run()
    assert len(r.classifier.seen) == 1
    acct = account_of(report)
    assert {c["task_state"] for c in acct["conversations"]} == {"unknown"}
    assert all("classifier-unavailable" in c["reasons"] for c in acct["conversations"])
    assert report["classifier"]["status"] == "unavailable"
    assert report["totals"]["unknown"] == 3 and report["totals"]["done"] == 0
    # the warm-up waited for the missing socket exactly once, using only the injected sleep
    assert r.clock.sleeps.count(5.0) == 30


def test_the_warm_up_stops_waiting_as_soon_as_the_model_appears(tmp_path):
    reader = simple_account(n=2)
    holder = {}

    def model():
        return "" if holder["rig"].clock.t < NOW + 80 else MODEL

    r = rig(tmp_path, {"home": reader}, model=model)
    holder["rig"] = r
    report = r.run()
    waited = r.clock.sleeps.count(5.0)
    assert 0 < waited < 30
    assert {c["task_state"] for c in account_of(report)["conversations"]} == {"done"}
    assert report["classifier"]["model"] == MODEL


def test_an_invalid_classification_gives_unknown(tmp_path):
    reader = simple_account(n=2)
    r = rig(tmp_path, {"home": reader}, classifier=FakeClassifier(invalid_classification()))
    report = r.run()
    assert {c["task_state"] for c in account_of(report)["conversations"]} == {"unknown"}
    assert report["classifier"]["status"] == "invalid"


def test_no_conversation_text_reaches_the_report_or_the_store(tmp_path):
    reader = simple_account(n=3)
    r = rig(tmp_path, {"home": reader})
    report = r.run()
    blob = report_mod.to_json(report)
    r.store.put_report(report)
    stored = "".join(p.read_text(errors="replace") for p in r.directory.rglob("*") if p.is_file())
    for text in (blob, stored):
        assert SENTINEL_REQUEST not in text and SENTINEL_FINAL not in text
    assert 'remaining_steps"' not in blob


def test_include_text_adds_only_the_bounded_remaining_steps(tmp_path):
    answer = f"{SENTINEL_FINAL}\nRemaining:\n- Ship STEP-SENTINEL\n"
    reader = FakeReader(
        scopes={"recent": [listing_item("c0", 600)]},
        payloads={"c0": conversation_payload("c0", age=600, answer=answer)},
    )
    incomplete = laya(task_finished=0.1, steps_remaining=0.9, model=MODEL)
    r = rig(tmp_path, {"home": reader}, classifier=FakeClassifier(incomplete))

    plain = r.run()
    assert "STEP-SENTINEL" not in report_mod.to_json(plain)
    assert "remaining_steps" not in account_of(plain)["conversations"][0]

    r.clock.t += 10
    with_text = r.run(include_text=True)
    conv = account_of(with_text)["conversations"][0]
    assert conv["task_state"] == "incomplete"
    assert conv["remaining_steps"] == ["Ship STEP-SENTINEL"]
    blob = report_mod.to_json(with_text)
    assert SENTINEL_FINAL not in blob and SENTINEL_REQUEST not in blob
    stored = "".join(p.read_text(errors="replace") for p in r.directory.rglob("*") if p.is_file())
    assert "STEP-SENTINEL" not in stored


def save_record(r, **kw):
    base = {
        "prompt_version": clf.PROMPT_VERSION,
        "model": MODEL,
        "thresholds_digest": dec.thresholds_digest(dec.DEFAULT_THRESHOLDS),
        "bench_digest": BENCH,
        "passed": True,
        "recorded_at": NOW,
        "summary": {"heldout_false_done": 0},
    }
    base.update(kw)
    val.save_record(r.directory, val.ValidationRecord(**base))


def test_auto_continue_is_denied_without_a_validation_record(tmp_path):
    r = rig(tmp_path, {"home": simple_account(n=2)})
    report = r.run()
    assert report["auto_continue"] == DENIED
    assert all(c["auto_continue"] == DENIED for c in account_of(report)["conversations"])


def test_auto_continue_is_allowed_only_with_a_matching_passing_record(tmp_path):
    allowed = {"allowed": True, "reason": "validated"}
    r = rig(tmp_path, {"home": simple_account(n=2)})
    save_record(r)
    report = r.run()
    assert report["auto_continue"] == allowed
    assert all(c["auto_continue"] == allowed for c in account_of(report)["conversations"])
    cached = r.run()  # confirmed from the cache: still gated by the same record
    assert all(c["auto_continue"] == allowed for c in account_of(cached)["conversations"])

    (tmp_path / "other").mkdir()
    other = rig(tmp_path / "other", {"home": simple_account(n=2)})
    save_record(other, model="laya-0.01")
    denied = other.run()
    assert denied["auto_continue"] == DENIED
    assert all(c["auto_continue"] == DENIED for c in account_of(denied)["conversations"])


def test_the_report_is_strict_json_with_the_contract_shape(tmp_path):
    report = rig(tmp_path, {"home": simple_account(n=2)}).run(days=3.0, turns=2)
    wire = json.loads(json.dumps(report, allow_nan=False))
    assert wire["schema"] == 1 and wire["tool"] == "pm-chatgpt-audit"
    assert wire["days"] == 3.0 and wire["workspaces"] == ["home"]
    assert wire["thresholds_digest"] == dec.thresholds_digest(dec.DEFAULT_THRESHOLDS)
    assert set(wire["classifier"]) == {"status", "model", "prompt_version", "reason"}
    conv = wire["accounts"]["home"]["conversations"][0]
    assert set(conv) == {
        "conversation_id",
        "url",
        "project_id",
        "archived",
        "updated_at",
        "source",
        "task_state",
        "response_state",
        "recovery_state",
        "reasons",
        "evidence_ids",
        "remaining_steps_count",
        "fingerprint",
        "classifier_status",
        "verified_at",
        "auto_continue",
    }
    assert conv["source"] == "read" and len(conv["fingerprint"]) == 64
    reads = account_of(report)["conversations"]
    assert [c["updated_at"] for c in reads] == sorted(
        (c["updated_at"] for c in reads), reverse=True
    )
    assert run_audit is not None
