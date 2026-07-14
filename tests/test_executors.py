"""The routing engine — capability classes, risk tiers, read-only recommendations."""

from __future__ import annotations

from pmlib.executors import Executor, capability_class, recommend_routing, risk_tier
from pmlib.model import WorkItem


def _ex(iid: str, model: str, reliability: str = "healthy", limit: float | None = 0.8,
        local: bool = False, ctx: float = 0.2) -> Executor:
    return Executor(id=iid, harness="claude-code", model=model, reliability=reliability,
                    limit_remaining=limit, local=local, context_pressure=ctx)


def _item(privacy: str = "remote-allowed", labels: list | None = None) -> WorkItem:
    return WorkItem(id="w", privacy=privacy, labels=labels or [])


def test_capability_class() -> None:
    assert capability_class("claude-opus-4-8") == "senior"
    assert capability_class("claude-fable-5") == "senior"
    assert capability_class("gemini-2.5-flash") == "junior"
    assert capability_class("claude-sonnet-4") == "mid"
    assert capability_class("") == "mid"
    # a small variant of a senior family reads junior (junior checked first), and an ambiguous
    # size string doesn't false-match a size needle
    assert capability_class("gpt-5-mini") == "junior"
    assert capability_class("o3-mini") == "junior"
    assert capability_class("llama-128b") == "mid"
    # "mini" must not match "geMINI" — gemini-pro is a capable mid model, not junior
    assert capability_class("gemini-2.5-pro") == "mid"
    assert capability_class("gemini-1.5-flash") == "junior"  # flash is genuinely junior


def test_risk_tier_from_labels() -> None:
    assert risk_tier(_item(labels=["security"])) == "high"
    assert risk_tier(_item(labels=["release"])) == "high"
    assert risk_tier(_item(labels=["docs"])) == "low"
    assert risk_tier(_item(labels=["feature"])) == "normal"
    assert risk_tier(_item()) == "normal"


def test_high_risk_routes_to_senior() -> None:
    rec = recommend_routing(_item(labels=["security"]),
                            [_ex("j", "gemini-2.5-flash"), _ex("s", "claude-opus-4-8")])
    assert rec.action == "route" and rec.executor_id == "s" and rec.risk == "high"


def test_low_risk_routes_to_junior_for_cost_guard() -> None:
    rec = recommend_routing(_item(labels=["docs"]),
                            [_ex("j", "gemini-2.5-flash"), _ex("s", "claude-opus-4-8")])
    assert rec.executor_id == "j"


def test_normal_risk_routes_to_mid() -> None:
    rec = recommend_routing(_item(),
                            [_ex("j", "flash"), _ex("m", "claude-sonnet-4"), _ex("s", "opus")])
    assert rec.executor_id == "m"


def test_wait_when_all_executors_near_limit() -> None:
    rec = recommend_routing(_item(), [_ex("a", "sonnet", limit=0.05), _ex("b", "opus", limit=0.02)])
    assert rec.action == "wait"


def test_wait_when_all_executors_unhealthy() -> None:
    rec = recommend_routing(_item(), [_ex("a", "sonnet", reliability="limit-wait"),
                                      _ex("b", "opus", reliability="no-telemetry")])
    assert rec.action == "wait"


def test_no_executors_is_wait() -> None:
    assert recommend_routing(_item(), []).action == "wait"


def test_blocked_when_local_only_has_no_local_executor() -> None:
    rec = recommend_routing(_item(privacy="local-only"), [_ex("a", "opus", local=False)])
    assert rec.action == "blocked"
    assert "local" in rec.reason.lower()


def test_local_only_routes_to_local_executor() -> None:
    rec = recommend_routing(_item(privacy="local-only"),
                            [_ex("remote", "opus", local=False), _ex("local", "llama-70b", local=True)])
    assert rec.action == "route" and rec.executor_id == "local"


def test_local_only_with_exhausted_local_executor_is_wait_not_blocked() -> None:
    """A local executor exists but lacks quota -> wait (it recovers), never a false privacy block."""
    rec = recommend_routing(_item(privacy="local-only"), [_ex("local", "llama-70b", local=True,
                                                              limit=0.01)])
    assert rec.action == "wait"


def test_local_only_with_no_executors_is_wait_not_blocked() -> None:
    assert recommend_routing(_item(privacy="local-only"), []).action == "wait"


def test_explicit_risk_override_wins() -> None:
    rec = recommend_routing(_item(labels=["docs"]),  # would be low from labels
                            [_ex("j", "flash"), _ex("s", "opus")], risk="high")
    assert rec.risk == "high" and rec.executor_id == "s"


def test_high_risk_needs_more_headroom_than_low() -> None:
    # limit 0.2: enough for low-risk (needs >=0.05) but not high-risk (needs >=0.3)
    execs = [_ex("s", "opus", limit=0.2)]
    assert recommend_routing(_item(labels=["docs"]), execs).action == "route"
    assert recommend_routing(_item(labels=["security"]), execs).action == "wait"
