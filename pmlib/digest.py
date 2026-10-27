"""Weekly flow-metrics digest — HTML body built from the same report functions ``pm report`` uses.

Accessed via: ``bin/pm-metrics-digest`` -> ``python -m pmlib.digest`` (or ``weekly_digest_html``).
Numbers come from ``metrics_report`` / ``aging_report`` / ``queue_report``; this module does not
recompute time-in-status. Throughput is items entering ``_SHIPPED_STATES`` in the last 7 days.
Pure: clock passed in.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .reminders import parse_iso
from .reports import (
    _SHIPPED_STATES,
    _items_entering,
    aging_report,
    metrics_report,
    queue_report,
)

if TYPE_CHECKING:
    from .events import Event
    from .model import WorkItem
    from .states import StateMachine

_THROUGHPUT_DAYS = 7


def shipped_ids_since(events: list[Event], now: datetime, days: int = _THROUGHPUT_DAYS) -> list[str]:
    """Item ids that entered a shipped state in the last ``days`` days."""
    cutoff = now - timedelta(days=days)
    in_window = []
    for event in events:
        ts = parse_iso(event.ts)
        if ts is not None and ts >= cutoff:
            in_window.append(event)
    return sorted(_items_entering(in_window, set(_SHIPPED_STATES)))


def render_weekly_digest(
    metrics: dict,
    aging: dict,
    queue: dict,
    shipped_ids: list[str],
) -> str:
    """HTML body. Copies numbers from the report dicts; does not recompute them."""
    lines: list[str] = ["<b>Weekly flow metrics</b>", ""]
    lines.append("<b>Time in status</b>")
    sections = (metrics.get("groups") or {}).get("by_category") or []
    if not sections:
        lines.append("• (no dwell data)")
    for section in sections:
        label = html.escape(str(section.get("label", "")))
        mean = section.get("mean_seconds", 0)
        p50 = section.get("p50_seconds", 0)
        p90 = section.get("p90_seconds", 0)
        n = section.get("count", 0)
        lines.append(f"• {label} — mean {mean}s p50 {p50}s p90 {p90}s (n={n})")
    lines.append("")
    lines.append("<b>Throughput (7d)</b>")
    if shipped_ids:
        ids = ", ".join(html.escape(i) for i in shipped_ids)
        lines.append(f"• shipped: {len(shipped_ids)} — {ids}")
    else:
        lines.append("• shipped: 0")
    lines.append("")
    lines.append("<b>WIP</b>")
    wip = (queue.get("groups") or {}).get("by_category") or []
    if not wip:
        lines.append("• (empty queue)")
    for section in wip:
        label = html.escape(str(section.get("label", "")))
        n = section.get("count", 0)
        ids = ", ".join(html.escape(i) for i in (section.get("item_ids") or [])[:8])
        extra = f" — {ids}" if ids else ""
        lines.append(f"• {label}: {n}{extra}")
    lines.append("")
    lines.append("<b>Aging</b>")
    entries = aging.get("entries") or []
    if not entries:
        lines.append("• (none past threshold)")
    for entry in entries:
        iid = html.escape(str(entry.get("item_id", "")))
        state = html.escape(str(entry.get("state", "")))
        age = entry.get("age_seconds", 0)
        thresh = entry.get("threshold_seconds", 0)
        lines.append(f"• <code>{iid}</code> — {state} age {age}s (threshold {thresh}s)")
    return '\n'.join(lines)


def weekly_digest_html(
    items: dict[str, WorkItem],
    events: list[Event],
    machine: StateMachine,
    now: datetime,
) -> str:
    """Build the three reports and render. Digest numbers match ``pm report`` exactly."""
    metrics = metrics_report(items, machine, now).to_dict()
    aging = aging_report(items, machine, now).to_dict()
    queue = queue_report(items, machine).to_dict()
    shipped = shipped_ids_since(events, now)
    return render_weekly_digest(metrics, aging, queue, shipped)


def main() -> int:
    from .commands._shared import load_items, machine, open_store
    store = open_store()
    m = machine()
    events = store.read_all()
    items = load_items(store, m)
    now = datetime.now(UTC)
    print(weekly_digest_html(items, events, m, now))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
