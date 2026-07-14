"""The executor ledger + read-only routing recommendations.

WHAT THIS FILE IS
    The ``Executor`` ledger row, ``capability_class`` (model -> senior/mid/junior), ``risk_tier``
    (work item -> low/normal/high), and ``recommend_routing`` (PURE: item + ledger -> a
    ``Recommendation``). It RECOMMENDS an executor; it never dispatches and writes no events.

WHAT IT IS NOT (research #255)
    NOT a cost accountant. The ecosystem has invocation stats, not reliable spend/pricing, so this
    guards QUOTA and LIMITS (headroom, reliability, reset), never dollar cost. Autonomous dispatch
    (with the privacy-aware fallback + kill-switch + the 10 adversarial tests) is a later release;
    here the privacy guard is a read-only WARNING (``blocked`` recommendation), not enforcement.

ROUTING POLICY
    - Risk tier (from labels, or an explicit override) picks the capability the work wants: high ->
      senior, normal -> mid, low -> junior (cost guard). Higher risk also demands more quota
      headroom before starting (long work must not begin on a near-limit executor).
    - Privacy: local-only work may only be routed to a ``local`` executor; if none, ``blocked``.
    - If no eligible executor has headroom, ``wait`` (schedule a reset wakeup).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation-only
    from .model import WorkItem

# Model-name substrings -> capability class. Unknown models are treated as ``mid``. JUNIOR is
# checked FIRST so a small variant of a senior family (``gpt-5-mini``, ``o3-mini``) reads junior,
# not senior. Ambiguous size needles (``8b``/``7b``) are omitted — they false-match ``128b``.
_SENIOR = ("opus", "fable", "sol", "gpt-5", "o3", "o1-pro")
# ``-mini`` (with the dash), NOT bare ``mini`` — bare ``mini`` matches ``geMINI`` and would
# misclassify ``gemini-2.5-pro`` as junior.
_JUNIOR = ("haiku", "flash", "-mini", "glm", "small", "-lite", "nano")

# Labels that mark a work item's risk tier.
_HIGH_RISK = {"security", "release", "architecture", "migration", "infra", "auth", "payments"}
_LOW_RISK = {"docs", "triage", "chore", "research", "typo", "cleanup"}

# Quota headroom a tier needs before it is safe to START work there (fraction of quota remaining).
_HEADROOM = {"high": 0.3, "normal": 0.15, "low": 0.05}

# How well each capability class fits each risk tier (higher = better).
_FIT = {
    "high": {"senior": 3, "mid": 2, "junior": 1},
    "normal": {"mid": 3, "senior": 2, "junior": 1},
    "low": {"junior": 3, "mid": 2, "senior": 1},
}

_ELIGIBLE_RELIABILITY = {"healthy", "degraded"}
_MAX_CONTEXT_PRESSURE = 0.9


@dataclass
class Executor:
    """One executor the PM agent could route work to (a harness+model on a pane/repo)."""

    id: str
    harness: str
    model: str = ""
    pane_id: str = ""
    repo: str = ""
    reliability: str = "no-telemetry"  # healthy | degraded | overloaded | limit-wait | no-telemetry
    limit_remaining: float | None = None  # fraction 0..1 of quota left; None = unknown
    reset_at: str = ""
    context_pressure: float | None = None  # fraction 0..1 of the context window used
    active_tasks: int = 0
    local: bool = False  # a locally-hosted model (privacy-safe for local-only work)

    def capability(self) -> str:
        return capability_class(self.model)

    def to_dict(self) -> dict:
        return {"id": self.id, "harness": self.harness, "model": self.model,
                "pane_id": self.pane_id, "repo": self.repo, "reliability": self.reliability,
                "limit_remaining": self.limit_remaining, "reset_at": self.reset_at,
                "context_pressure": self.context_pressure, "active_tasks": self.active_tasks,
                "local": self.local, "capability": self.capability()}


@dataclass(frozen=True)
class Recommendation:
    """A read-only routing verdict. ``action`` is route | wait | blocked."""

    item_id: str
    risk: str
    action: str
    executor_id: str
    reason: str

    def to_dict(self) -> dict:
        return {"item_id": self.item_id, "risk": self.risk, "action": self.action,
                "executor_id": self.executor_id, "reason": self.reason}


def capability_class(model: str) -> str:
    """Classify a model name into senior | mid | junior (unknown -> mid).

    JUNIOR is tested before SENIOR so ``gpt-5-mini`` / ``o3-mini`` classify as the cheap variant
    rather than matching their senior family. No senior model name carries a junior needle, so the
    order is safe.
    """
    name = model.lower()
    if any(j in name for j in _JUNIOR):
        return "junior"
    if any(s in name for s in _SENIOR):
        return "senior"
    return "mid"


def risk_tier(item: WorkItem) -> str:
    """The work item's risk tier from its labels (high | low | normal-default)."""
    labels = {label.strip().lower() for label in item.labels}
    if labels & _HIGH_RISK:
        return "high"
    if labels & _LOW_RISK:
        return "low"
    return "normal"


def recommend_routing(item: WorkItem, executors: list[Executor],
                      risk: str | None = None) -> Recommendation:
    """Recommend an executor for ``item`` (or wait/blocked). Pure; dispatches nothing."""
    tier = risk or risk_tier(item)
    candidates = executors
    # Privacy filter is by EXISTENCE, applied BEFORE the quota-eligibility filter: local-only work
    # is ``blocked`` only when NO local executor exists at all. A local executor that merely lacks
    # quota is a ``wait`` (it will recover), never a privacy block.
    if item.privacy == "local-only":
        if not executors:
            return Recommendation(item.id, tier, "wait", "", "no executors known")
        candidates = [e for e in executors if e.local]
        if not candidates:
            return Recommendation(item.id, tier, "blocked", "",
                                  "local-only work: no local executor exists (must not route remote)")
    eligible = [e for e in candidates if _eligible(e, tier)]
    if not eligible:
        return Recommendation(item.id, tier, "wait", "",
                              "no executor with quota headroom / telemetry; schedule a reset wakeup")
    best = _rank(eligible, tier)[0]
    return Recommendation(item.id, tier, "route", best.id,
                          f"{best.model or best.harness} ({best.capability()}) fits {tier}-risk work")


def _eligible(executor: Executor, tier: str) -> bool:
    if executor.reliability not in _ELIGIBLE_RELIABILITY:
        return False
    if executor.context_pressure is not None and executor.context_pressure > _MAX_CONTEXT_PRESSURE:
        return False
    if executor.limit_remaining is not None and executor.limit_remaining < _HEADROOM[tier]:
        return False
    return True


def _rank(executors: list[Executor], tier: str) -> list[Executor]:
    fit = _FIT[tier]

    def score(e: Executor) -> tuple:
        # best fit, then most headroom, then least context pressure, then fewest active tasks.
        return (fit.get(e.capability(), 0),
                e.limit_remaining if e.limit_remaining is not None else 1.0,
                -(e.context_pressure if e.context_pressure is not None else 0.0),
                -e.active_tasks)

    # reverse=True => best score first; ties break toward the lexicographically LARGEST id (stable).
    return sorted(executors, key=lambda e: (score(e), e.id), reverse=True)
