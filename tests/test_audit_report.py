"""Pure report builders: status rules, exact totals, bounds, exit codes, JSON and text rendering."""

from __future__ import annotations

import json

import pytest

from pmlib.chatgpt_control import audit_report as rep

DENIED = {"allowed": False, "reason": "classification-not-validated"}
CLASSIFIER = {
    "status": "ok",
    "model": "laya-0.23.0",
    "prompt_version": "completion-v1",
    "reason": "",
}


def conversation(i: int, state: str = "done", **kw) -> dict:
    base = {
        "conversation_id": f"c{i:05d}",
        "url": f"https://chatgpt.com/c/c{i:05d}",
        "project_id": None,
        "archived": False,
        "updated_at": 1_000_000.0 - i,
        "source": "read",
        "task_state": state,
        "response_state": "finished",
        "recovery_state": "not_needed",
        "reasons": ["laya-task-finished"],
        "evidence_ids": ["u1", "a1"],
        "remaining_steps_count": 0,
        "fingerprint": "f" * 64,
        "classifier_status": "ok",
        "verified_at": 1_000_100.0,
        "auto_continue": DENIED,
    }
    base.update(kw)
    return base


def finished(conversations=(), **cov) -> dict:
    account = rep.new_account_report()
    account["observed_account"] = "home"
    account["conversations"] = list(conversations)
    account["coverage"].update(cov)
    return rep.finish_account(account)


def build(accounts: dict, **kw) -> dict:
    args = {
        "generated_at": 1_000_200.0,
        "days": 14.0,
        "workspaces": tuple(accounts),
        "accounts": accounts,
        "classifier": CLASSIFIER,
        "thresholds_digest": "t" * 64,
        "auto_continue": DENIED,
        "pm_version": "0.8.0",
    }
    args.update(kw)
    return rep.build_report(**args)


def test_a_new_account_report_is_an_empty_ok_shell():
    account = rep.new_account_report()
    assert account == {
        "status": "ok",
        "reason": "",
        "observed_account": None,
        "not_before": None,
        "coverage": {
            "listed_total": 0,
            "in_window": 0,
            "read": 0,
            "cached": 0,
            "skipped": {},
            "errors": 0,
            "partial": False,
            "budget_exhausted": False,
            "stopped": None,
            "requests": 0,
        },
        "scopes": {},
        "conversations": [],
        "omitted": 0,
    }


def test_a_clean_account_finishes_ok():
    account = finished([conversation(1)], listed_total=1, in_window=1, read=1)
    assert (account["status"], account["reason"], account["coverage"]["partial"]) == (
        "ok",
        "",
        False,
    )


def scope(complete=True, reason=""):
    return {"listed": 1, "pages": 1, "complete": complete, "reason": reason, "total": 1}


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda a: a["scopes"].update(recent=scope(False, "page-cap")), "page-cap"),
        (lambda a: a["scopes"].update(recent=scope(False, "timeout")), "timeout"),
        (lambda a: a["coverage"].update(errors=1), "read-errors"),
        (lambda a: a["coverage"].update(skipped={"http-500": 1}), "read-errors"),
        (lambda a: a["coverage"].update(budget_exhausted=True), "budget-exhausted"),
        (
            lambda a: a["coverage"].update(stopped={"status": 429, "not_before": 5.0}),
            "http-429",
        ),
    ],
)
def test_anything_missing_makes_the_account_partial_with_a_reason(mutate, reason):
    account = rep.new_account_report()
    mutate(account)
    rep.finish_account(account)
    assert account["status"] == "partial" and account["reason"] == reason
    assert account["coverage"]["partial"] is True


def test_stopped_outranks_budget_in_the_reason():
    account = rep.new_account_report()
    account["coverage"].update(
        budget_exhausted=True, stopped={"status": 403, "not_before": 9.0}, errors=2
    )
    assert rep.finish_account(account)["reason"] == "http-403"


def test_unavailable_stays_unavailable_with_its_reason_and_is_never_ok():
    account = rep.mark_unavailable(
        rep.new_account_report(), "not-before", observed_account=None, not_before=123.0
    )
    rep.finish_account(account)
    assert (account["status"], account["reason"], account["not_before"]) == (
        "unavailable",
        "not-before",
        123.0,
    )
    assert account["coverage"]["partial"] is True


def test_conversations_are_sorted_newest_first_when_an_account_is_finished():
    account = finished([conversation(5), conversation(1), conversation(3)])
    assert [c["conversation_id"] for c in account["conversations"]] == [
        "c00001",
        "c00003",
        "c00005",
    ]


def test_overall_status_is_the_worst_across_accounts_and_totals_are_exact():
    home = finished(
        [conversation(1, "done"), conversation(2, "incomplete"), conversation(3, "working")]
    )
    glide = rep.finish_account(rep.mark_unavailable(rep.new_account_report(), "account-not-bound"))
    report = build({"home": home, "glide.vc": glide})
    assert report["status"] == "unavailable"
    assert report["totals"] == {
        "conversations": 3,
        "done": 1,
        "incomplete": 1,
        "blocked": 0,
        "working": 1,
        "unknown": 0,
    }
    assert report["workspaces"] == ["home", "glide.vc"]
    assert report["schema"] == 1 and report["tool"] == "pm-chatgpt-audit"
    assert report["pm_version"] == "0.8.0"
    assert report["cutoff"] == 1_000_200.0 - 14.0 * 86400
    assert report["auto_continue"] == DENIED


@pytest.mark.parametrize(
    ("states", "status", "code"),
    [
        (("ok", "ok"), "ok", 0),
        (("ok", "partial"), "partial", 3),
        (("partial", "unavailable"), "unavailable", 4),
    ],
)
def test_status_and_exit_code_follow_the_worst_account(states, status, code):
    accounts = {}
    for name, state in zip(("home", "glide.vc"), states, strict=True):
        account = rep.new_account_report()
        if state == "partial":
            account["coverage"]["errors"] = 1
        if state == "unavailable":
            rep.mark_unavailable(account, "cdp-unreachable")
        accounts[name] = rep.finish_account(account)
    report = build(accounts)
    assert report["status"] == status
    assert rep.exit_code(report) == code


def test_an_unknown_status_is_never_a_success_exit_code():
    assert rep.exit_code({"status": "weird"}) == 4
    assert rep.exit_code({}) == 4


def test_at_most_max_reported_conversations_are_kept_and_the_rest_counted_omitted():
    many = [conversation(i, reasons=[], evidence_ids=[]) for i in range(rep.MAX_REPORTED + 37)]
    report = build({"home": finished(many)})
    acct = report["accounts"]["home"]
    assert len(acct["conversations"]) == rep.MAX_REPORTED
    assert acct["omitted"] == 37
    assert acct["conversations"][0]["conversation_id"] == "c00000"  # the newest are kept
    assert report["totals"]["conversations"] == rep.MAX_REPORTED + 37  # totals stay exact


def test_an_oversized_report_drops_conversations_from_the_end_and_records_omitted():
    wide = [conversation(i, reasons=["r" * 200] * 12) for i in range(450)]
    report = build({"home": finished(wide), "glide.vc": finished(wide[:200])})
    text = rep.to_json(report)
    assert len(text.encode()) <= rep.MAX_JSON_BYTES
    wire = json.loads(text)
    kept = sum(len(a["conversations"]) for a in wire["accounts"].values())
    omitted = sum(a["omitted"] for a in wire["accounts"].values())
    assert kept + omitted == 650 and omitted > 0
    assert wire["totals"]["conversations"] == 650
    for acct in wire["accounts"].values():  # the oldest were dropped, never the newest
        ids = [c["conversation_id"] for c in acct["conversations"]]
        assert ids == sorted(ids)


def test_many_project_scopes_are_collapsed_into_one_exact_summary():
    account = rep.new_account_report()
    for i in range(300):
        account["scopes"][f"project:g-p-{i:032x}"] = scope()
    account["scopes"]["recent"] = scope()
    report = build({"home": rep.finish_account(account)})
    scopes = report["accounts"]["home"]["scopes"]
    assert set(scopes) == {"recent", "project:*"}
    assert scopes["project:*"]["listed"] == 300 and scopes["project:*"]["count"] == 300
    assert scopes["project:*"]["complete"] is True


def test_incomplete_project_scopes_stay_individually_visible():
    account = rep.new_account_report()
    for i in range(300):
        account["scopes"][f"project:g-p-{i:032x}"] = scope()
    bad = f"project:g-p-{999:032x}"
    account["scopes"][bad] = scope(False, "http-500")
    scopes = build({"home": rep.finish_account(account)})["accounts"]["home"]["scopes"]
    assert scopes[bad]["reason"] == "http-500"
    assert scopes["project:*"]["count"] == 300


def test_to_json_is_strict_sorted_and_rejects_nan():
    report = build({"home": finished([conversation(1)])})
    text = rep.to_json(report)
    assert json.loads(text) == report
    assert text == json.dumps(report, sort_keys=True, allow_nan=False)
    report["generated_at"] = float("nan")
    with pytest.raises(ValueError):
        rep.to_json(report)


def test_render_text_is_a_compact_bounded_summary():
    many = [conversation(i, "incomplete") for i in range(300)]
    report = build({"home": finished(many, listed_total=300, in_window=300, read=300)})
    text = rep.render_text(report)
    assert "home" in text and "300" in text
    assert "completion-v1" in text and "classification-not-validated" in text
    assert len(text.encode()) < 16 * 1024
    assert "https://chatgpt.com/c/c00000" in text
    assert "https://chatgpt.com/c/c00299" not in text  # bounded; the tail is summarised
    assert "more" in text


def test_render_text_states_why_an_account_is_unavailable():
    account = rep.finish_account(
        rep.mark_unavailable(rep.new_account_report(), "not-before", not_before=5.0)
    )
    text = rep.render_text(build({"home": account}))
    assert "unavailable" in text and "not-before" in text
