"""Reports — the read-only reporting surface over the ledger (research #255's whole point).

Builders are PURE (projection/log + a clock -> a ``Report``). The design warns that
monthly reports become vanity metrics unless every number links to underlying events, so EVERY
count carries the item ids behind it (``ReportSection.item_ids``) — a reader can trace any number
back to the work items (and thence the log) that produced it.

- ``queue_report``  — the current queue, grouped by category and by health.
- ``stuck_report``  — what needs attention: blocked/exception states, no-progress, dependency
  cycles, expired timers, and dead-lettered intakes.
- ``retro_report``  — per-item retrospective facts for work that passed through a problem state
  (trigger, recovery, time-in-problem, project) + recurring-pattern counts.
- ``monthly_report``— a month grouped PRODUCT vs PROCESS, each metric linking its item ids.
- ``metrics_report``— average / p50 / p90 time-in-status per lifecycle category, from
  consecutive transition timestamps (re-entries accumulate; clock passed in).
- ``aging_report``  — items whose current-state age exceeds the state's ``sla_seconds``
  (default: the reminders no-progress window). Reuses ``current_state_age_seconds``.
- ``aging_report``  — items whose current-state age exceeds the state's ``sla_seconds``
  (default: the reminders no-progress window). Reuses ``current_state_age_seconds``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from .labels import pm_health
from .reminders import DEFAULT_NO_PROGRESS_SECONDS, compute_reminders, parse_iso
from .states import WorkStateCategory

if TYPE_CHECKING:  # annotation-only
    from .events import Event
    from .intake import IntakeEvent
    from .model import WorkItem
    from .states import StateMachine

_SHIPPED_STATES = ("ship-ready", "deployed", "done")
# Reminder kinds that mean "needs attention" (a problem) — NOT the positive "you can proceed" ones.
_STUCK_REMINDER_KINDS = {"no-progress", "dependency-cycle", "duplicate-open",
                         "superseded-open", "timer-expired"}


@dataclass
class ReportSection:
    """One metric: a labelled count plus the item ids behind it (the traceability link)."""

    label: str
    count: int
    item_ids: list[str] = field(default_factory=list)

    mean_seconds: int | None = None
    p50_seconds: int | None = None
    p90_seconds: int | None = None

    def to_dict(self) -> dict:
        out = {"label": self.label, "count": self.count, "item_ids": list(self.item_ids)}
        if self.mean_seconds is not None:
            out["mean_seconds"] = self.mean_seconds
        if self.p50_seconds is not None:
            out["p50_seconds"] = self.p50_seconds
        if self.p90_seconds is not None:
            out["p90_seconds"] = self.p90_seconds
        return out


@dataclass
class Report:
    """A rendered report. ``groups`` maps a group name to its sections; ``entries``/``patterns``
    carry detail rows for the stuck/retro reports. Every number traces through ``item_ids``.
    """

    kind: str
    scope: str = ""
    total: int = 0
    groups: dict[str, list[ReportSection]] = field(default_factory=dict)
    entries: list[dict] = field(default_factory=list)
    patterns: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "scope": self.scope, "total": self.total,
            "groups": {g: [s.to_dict() for s in secs] for g, secs in self.groups.items()},
            "entries": list(self.entries), "patterns": list(self.patterns),
        }


def _section(label: str, ids: list[str]) -> ReportSection:
    return ReportSection(label=label, count=len(ids), item_ids=sorted(ids))


def _is_problem(defn: object) -> bool:
    """A state that needs attention: an exception category, or a structurally-blocked state."""
    return bool(defn) and (defn.category is WorkStateCategory.EXCEPTION or defn.blocked)  # type: ignore[union-attr]


# ── queue ──────────────────────────────────────────────────────────────────────────────────
def queue_report(items: dict[str, WorkItem], machine: StateMachine) -> Report:
    by_category: list[ReportSection] = []
    for cat in WorkStateCategory:
        ids = [i.id for i in items.values()
               if (d := machine.get(i.state)) and d.category is cat]
        if ids:
            by_category.append(_section(cat.value, ids))
    by_health: dict[str, list[str]] = {}
    for item in items.values():
        by_health.setdefault(pm_health(item, machine), []).append(item.id)
    health_sections = [_section(k, v) for k, v in sorted(by_health.items())]
    return Report(kind="queue", total=len(items),
                  groups={"by_category": by_category, "by_health": health_sections})


# ── stuck ──────────────────────────────────────────────────────────────────────────────────
def stuck_report(items: dict[str, WorkItem], machine: StateMachine, now: datetime,
                 intake_ledger: dict[str, IntakeEvent]) -> Report:
    entries: dict[tuple, dict] = {}
    for rem in compute_reminders(items, machine, now):
        if rem.kind in _STUCK_REMINDER_KINDS:
            entries[(rem.kind, rem.item_id)] = {
                "kind": rem.kind, "item_id": rem.item_id, "detail": rem.detail}
    for item in items.values():
        defn = machine.get(item.state)
        if _is_problem(defn):
            kind = "blocked" if defn.blocked else "exception"  # type: ignore[union-attr]
            entries[(kind, item.id)] = {"kind": kind, "item_id": item.id,
                                        "detail": item.state, "since": item.updated_at}
    for intake in intake_ledger.values():
        if intake.status == "deadletter":
            entries[("deadletter", intake.id)] = {"kind": "deadletter", "item_id": intake.id,
                                                  "detail": intake.last_error}
    rows = sorted(entries.values(), key=lambda e: (e["kind"], e["item_id"]))
    return Report(kind="stuck", scope=now.isoformat(), total=len(rows),
                  groups={"summary": _summarize(rows)}, entries=rows)


def _summarize(rows: list[dict]) -> list[ReportSection]:
    by_kind: dict[str, list[str]] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row["item_id"])
    return [_section(k, v) for k, v in sorted(by_kind.items())]


# ── retro ──────────────────────────────────────────────────────────────────────────────────
def retro_report(items: dict[str, WorkItem], machine: StateMachine, now: datetime) -> Report:
    entries: list[dict] = []
    patterns: dict[str, list[str]] = {}
    for item in items.values():
        for entry in _retro_passes(item, machine, now):
            entries.append(entry)
            patterns.setdefault(entry["trigger_state"], []).append(item.id)
    entries.sort(key=lambda e: (e["item_id"], e["trigger_at"]))
    pattern_rows = [{"state": s, "count": len(set(ids)), "item_ids": sorted(set(ids))}
                    for s, ids in sorted(patterns.items())]
    return Report(kind="retro", scope=now.isoformat(), total=len(entries),
                  entries=entries, patterns=pattern_rows)


def _retro_passes(item: WorkItem, machine: StateMachine, now: datetime) -> list[dict]:
    """One retro fact per time the item ENTERED a problem state (trigger + recovery)."""
    out: list[dict] = []
    history = item.history
    for idx, trans in enumerate(history):
        if not _is_problem(machine.get(trans.to)):
            continue
        recovery = next((h for h in history[idx + 1:] if h.from_state == trans.to), None)
        end = parse_iso(recovery.ts) if recovery else now
        start = parse_iso(trans.ts)
        secs = int((end - start).total_seconds()) if start and end else None
        out.append({
            "item_id": item.id, "trigger_state": trans.to, "trigger_at": trans.ts,
            "recovered": recovery is not None,
            "recovered_at": recovery.ts if recovery else "",
            "time_in_problem_seconds": secs, "project": item.project_id,
        })
    return out


# ── metrics ────────────────────────────────────────────────────────────────────────────────
def category_dwell_seconds(item: WorkItem, machine: StateMachine, now: datetime) -> dict[str, int]:
    """Accumulated seconds per lifecycle category for one item. Re-entries sum.

    Duration in the category of ``trans.to`` is ``next.ts - trans.ts``; the current state's
    dwell is ``now - last.ts``. Categories with zero dwell are omitted.
    """
    totals: dict[str, int] = {}
    history = item.history
    for idx, trans in enumerate(history):
        start = parse_iso(trans.ts)
        if start is None:
            continue
        if idx + 1 < len(history):
            end = parse_iso(history[idx + 1].ts)
            if end is None:
                continue
        else:
            end = now
        defn = machine.get(trans.to)
        if defn is None:
            continue
        cat = defn.category.value
        totals[cat] = totals.get(cat, 0) + max(0, int((end - start).total_seconds()))
    return {k: v for k, v in totals.items() if v > 0}


def current_state_age_seconds(item: WorkItem, now: datetime) -> int | None:
    """Seconds in the current state: ``now - last.ts``, or ``created_at`` when history is empty."""
    stamp = item.history[-1].ts if item.history else item.created_at
    start = parse_iso(stamp)
    if start is None:
        return None
    return max(0, int((now - start).total_seconds()))


def _percentile(values: list[int], p: float) -> int:
    """Linear interpolation between closest ranks (Hyndman & Fan type 7); rounded to int seconds."""
    xs = sorted(values)
    n = len(xs)
    if n == 1:
        return xs[0]
    k = (n - 1) * p
    lo_i = math.floor(k)
    hi_i = math.ceil(k)
    if lo_i == hi_i:
        return int(xs[int(k)])
    lo, hi = xs[lo_i], xs[hi_i]
    return int(round(lo + (hi - lo) * (k - lo_i)))


def metrics_report(items: dict[str, WorkItem], machine: StateMachine, now: datetime) -> Report:
    """Per-category mean / p50 / p90 of per-item accumulated dwell. JSON seconds; clock in."""
    per_cat: dict[str, dict[str, int]] = {cat.value: {} for cat in WorkStateCategory}
    for item in items.values():
        for cat, secs in category_dwell_seconds(item, machine, now).items():
            per_cat[cat][item.id] = secs
    sections: list[ReportSection] = []
    for cat in WorkStateCategory:
        by_item = per_cat[cat.value]
        if not by_item:
            continue
        vals = list(by_item.values())
        sections.append(ReportSection(
            label=cat.value,
            count=len(vals),
            item_ids=sorted(by_item),
            mean_seconds=int(round(sum(vals) / len(vals))),
            p50_seconds=_percentile(vals, 0.5),
            p90_seconds=_percentile(vals, 0.9),
        ))
    return Report(kind="metrics", scope=now.isoformat(), total=len(items),
                  groups={"by_category": sections})



# Terminal delivery/post states plus ``deployed`` (not terminal, but finished shipping).
_AGING_SKIP_STATES = frozenset({"done", "deployed", "cancelled", "parked"})


def aging_report(
    items: dict[str, WorkItem],
    machine: StateMachine,
    now: datetime,
    default_seconds: int = DEFAULT_NO_PROGRESS_SECONDS,
) -> Report:
    """Items whose time in the CURRENT state exceeds the per-state SLA. Healthy states can age."""
    entries: list[dict] = []
    for item in sorted(items.values(), key=lambda i: i.id):
        defn = machine.get(item.state)
        if defn is None or defn.terminal or item.state in _AGING_SKIP_STATES:
            continue
        age = current_state_age_seconds(item, now)
        if age is None:
            continue
        threshold = defn.sla_seconds if defn.sla_seconds is not None else default_seconds
        if age <= threshold:
            continue
        entries.append({
            "item_id": item.id, "state": item.state, "category": defn.category.value,
            "age_seconds": age, "threshold_seconds": threshold,
        })
    ids = [e["item_id"] for e in entries]
    return Report(kind="aging", scope=now.isoformat(), total=len(entries),
                  groups={"summary": [_section("aging", ids)] if ids else []},
                  entries=entries)


# ── monthly ────────────────────────────────────────────────────────────────────────────────
def monthly_report(events: list[Event], items: dict[str, WorkItem], machine: StateMachine,
                   month: str) -> Report:
    in_month = [e for e in events if e.ts.startswith(month)]
    product = [
        _section("shipped", _items_entering(in_month, set(_SHIPPED_STATES))),
        _section("deployed", _items_entering(in_month, {"deployed"})),
        _section("parked", _items_entering(in_month, {"parked"})),
        _section("cancelled", _items_entering(in_month, {"cancelled"})),
    ]
    stuck = stuck_report(items, machine, _month_end(month), {})
    process = [
        _section("intake", _captured_ids(in_month)),
        _section("deadletter", _kind_ids(in_month, "intake_deadletter")),
        _section("fixing", _items_entering(in_month, {"fixing"})),
        _section("exceptions", _problem_entries(in_month, machine)),
        _section("stuck", [e["item_id"] for e in stuck.entries]),
    ]
    return Report(kind="monthly", scope=month,
                  groups={"product": product, "process": process})


def _items_entering(events: list[Event], targets: set[str]) -> list[str]:
    ids = {e.item_id for e in events
           if e.kind == "transition" and str(e.payload.get("to", "")) in targets}
    return list(ids)


def _problem_entries(events: list[Event], machine: StateMachine) -> list[str]:
    ids = {e.item_id for e in events
           if e.kind == "transition" and _is_problem(machine.get(str(e.payload.get("to", ""))))}
    return list(ids)


def _captured_ids(events: list[Event]) -> list[str]:
    return [str(e.payload.get("id") or e.id) for e in events if e.kind == "intake_captured"]


def _kind_ids(events: list[Event], kind: str) -> list[str]:
    return [e.item_id for e in events if e.kind == kind]


def _month_end(month: str) -> datetime:
    """The real last instant of ``month`` (handles 28/29/30/31), for the monthly stuck snapshot.

    Computed as (first of next month) - 1s so no days are truncated. Tolerant of a bad string.
    """
    try:
        year, mon = (int(part) for part in month.split("-", 1))
        first_next = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if mon == 12
                      else datetime(year, mon + 1, 1, tzinfo=timezone.utc))
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)
    return first_next - timedelta(seconds=1)
