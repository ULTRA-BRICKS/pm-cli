"""Plan digest binding and the continuation guards (freshness, contradiction, running, blockers)."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control import policy
from tests.control_support import CHAT_A, CHAT_B, NOW, combined, row


def decide(rows, *, chat=CHAT_A, turn="turn-1", now=NOW, quiet=False, workspace="home", **acct):
    data = combined({workspace: rows}, **acct)
    return policy.evaluate_continue(data, workspace, chat, turn, now, require_quiet=quiet)


def test_eligible_baseline_passes():
    assert decide([row()]).ok


def test_plan_digest_binds_every_field():
    base = policy.plan_digest("home", CHAT_A, "turn-1", "continue")
    assert len(base) == 64 and base == policy.plan_digest("home", CHAT_A, "turn-1", "continue")
    assert base != policy.plan_digest("glide.vc", CHAT_A, "turn-1", "continue")
    assert base != policy.plan_digest("home", CHAT_B, "turn-1", "continue")
    assert base != policy.plan_digest("home", CHAT_A, "turn-2", "continue")
    assert base != policy.plan_digest("home", CHAT_A, "turn-1", "pause")


def test_dedup_key_depends_on_all_identity_fields_and_digest():
    digest = policy.plan_digest("home", CHAT_A, "t", "continue")
    key = policy.dedup_key("home", CHAT_A, "t", "continue", digest)
    assert key == policy.dedup_key("home", CHAT_A, "t", "continue", digest)
    assert key != policy.dedup_key("glide.vc", CHAT_A, "t", "continue", digest)
    assert key != policy.dedup_key("home", CHAT_A, "t2", "continue", digest)
    assert key != policy.dedup_key("home", CHAT_A, "t", "continue", "0" * 64)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"status": "working", "running": True}, "running"),
        ({"running": True}, "contradictory-completed-and-running"),
        ({"running": None}, "running-unknown"),
        ({"status": "Thinking failed"}, "failed-turn-uses-recovery"),
        ({"status": "quota-or-rate-limit"}, "quota"),
        ({"status": "mystery"}, "status-not-eligible"),
        ({"reason": "unresolved-send"}, "blocker"),
        ({"reason": "Astra quota is latched"}, "blocker"),
        ({"reason": "not-enrolled"}, "blocker"),
        ({"watched": False}, "not-enrolled"),
        ({"present": False}, "not-present"),
        ({"observation": "stale"}, "observation-not-current"),
        ({"observation": "missing"}, "observation-not-current"),
        ({"observation": None}, "observation-not-current"),
        ({"turn": ""}, "missing-turn"),
        ({"turn": "other-turn"}, "turn-changed"),
        ({"continuation": "running", "continuation_current": True}, "continuation-running"),
        ({"last_go_state": "uncertain"}, "send-uncertain"),
        ({"last_go_state": "reserved"}, "send-uncertain"),
        ({"draft": True}, "draft-or-attachments"),
        ({"attachments": 2}, "draft-or-attachments"),
        ({"auth_challenge": True}, "auth-challenge"),
        ({"error_detail": "auth-challenge"}, "auth-challenge"),
        ({"page_count": 2}, "ambiguous-pages"),
        ({"error": "observation-error"}, "observation-error"),
        ({"session_stale": True}, "session-stale"),
        ({"check_stale": True}, "session-stale"),
        ({"last_checked_at": None}, "session-stale"),
        ({"last_checked_at": NOW - 181}, "session-stale"),
        ({"last_checked_at": NOW - 100, "backoff_seconds": 300}, "ok"),
        ({"last_checked_at": NOW - 421, "backoff_seconds": 300}, "session-stale"),
        ({"last_checked_at": float("nan")}, "session-stale"),
        ({"recovery": {"enrolled": False, "blockers": ["not-enrolled"]}}, "recovery-blocked"),
        (
            {"recovery": {"enrolled": True, "blockers": ["hold"], "hold": "stopped"}},
            "recovery-blocked",
        ),
        ({"recovery": {"enrolled": True, "blockers": ["quota"]}}, "recovery-blocked"),
        ({"recovery": {"enrolled": True, "blockers": ["unresolved-send"]}}, "recovery-blocked"),
        ({"recovery": {"enrolled": True, "blockers": [], "draft": True}}, "recovery-blocked"),
        ({"recovery": {"enrolled": True, "blockers": [], "challenge": True}}, "recovery-blocked"),
        ({"recovery": "garbage"}, "recovery-blocked"),
        (
            {"completion_invalidated_at": NOW - 50, "completion_observed_at": NOW - 100},
            "completion-invalidated",
        ),
        (
            {"completion_invalidated_at": NOW - 50, "completion_observed_at": None},
            "completion-invalidated",
        ),
    ],
)
def test_guards_refuse_with_reason(overrides, reason):
    verdict = decide([row(**overrides)])
    if reason == "ok":
        assert verdict.ok
        return
    assert not verdict.ok
    assert verdict.reason == reason


def test_snapshot_staleness_and_account_failure():
    assert decide([row()], stale=True).reason == "account-stale"
    assert decide([row()], generated_at=NOW - 181).reason == "account-stale"
    assert decide([row()], generated_at=None).reason == "account-stale"
    data = combined({"home": None, "glide.vc": [row()]})
    verdict = policy.evaluate_continue(data, "home", CHAT_A, "turn-1", NOW, require_quiet=False)
    assert verdict.reason == "account-unavailable"


def test_missing_and_duplicate_rows():
    assert decide([row(CHAT_B)]).reason == "session-missing"
    assert decide([row(), row()]).reason == "session-duplicate"


def test_cross_account_chat_is_not_found_in_the_other_account():
    data = combined({"home": [row(CHAT_B)], "glide.vc": [row(CHAT_A)]})
    verdict = policy.evaluate_continue(data, "home", CHAT_A, "turn-1", NOW, require_quiet=False)
    assert verdict.reason == "session-missing"


def test_quiet_window_uses_last_progress():
    assert decide([row(last_progress_at=NOW - 119)], quiet=True).reason == "not-quiet"
    assert decide([row(last_progress_at=NOW - 120)], quiet=True).ok
    assert decide([row(last_progress_at=None)], quiet=True).reason == "not-quiet"
    assert decide([row(last_progress_at=NOW - 5)], quiet=False).ok


def test_unknown_status_with_current_observation_is_eligible():
    assert decide([row(status="unknown", running=False)]).ok


def test_non_finite_or_bool_times_are_not_fresh():
    assert decide([row()], generated_at=float("nan")).reason == "account-stale"
    assert decide([row()], generated_at=True).reason == "account-stale"


def test_expiry_helper():
    assert policy.TTL_SECONDS == 900
    assert policy.expires_at(NOW) == int(NOW) + 900
    assert policy.is_expired(int(NOW) + 900, int(NOW) + 900)
    assert not policy.is_expired(int(NOW) + 900, int(NOW) + 899)


def test_redone_completion_after_invalidation_is_eligible():
    recovered = row(completion_invalidated_at=NOW - 300, completion_observed_at=NOW - 100)
    assert decide([recovered]).ok


def test_account_effective_controls_veto_every_chat():
    assert (
        decide(
            [row()], effective_controls={"global_stop": "stopped", "quota_latched": False}
        ).reason
        == "global-stop"
    )
    assert (
        decide([row()], effective_controls={"global_stop": "", "quota_latched": True}).reason
        == "quota-latched"
    )
    assert decide([row()], effective_controls={"global_stop": "", "quota_latched": False}).ok


def test_healthy_recovery_object_is_accepted():
    healthy = {"enrolled": True, "enabled": True, "blockers": [], "hold": "", "draft": False}
    assert decide([row(recovery=healthy)]).ok


def test_real_passive_shape_is_eligible_to_ask():
    """The installed monitor always emits effort='' and model_verified=False (visible trigger text)."""
    passive = row(model="Thinking effortPro", effort="", model_verified=False)
    assert decide([passive]).ok
    assert decide([passive], quiet=True).ok
    assert decide([row(model="", effort="", model_verified=False)]).ok


def test_exact_turn_binding_refuses_a_newer_turn():
    assert decide([row(turn="turn-2", status="working", running=True)]).reason == "running"
    assert decide([row(turn="turn-2")]).reason == "turn-changed"
