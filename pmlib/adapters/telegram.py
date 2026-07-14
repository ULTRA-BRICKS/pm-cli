"""The telegram adapter — capture inbound Telegram messages into the intake dead-letter queue.

WHAT THIS FILE IS
    ``plan_capture`` (PURE: tg records + the current ledger -> ``intake_captured`` events, deduped
    by Telegram message id) + ``fetch_tg_records`` (EFFECTFUL: shell out to ``tg replies``) +
    ``default_processor`` (turns a captured intake into a WorkItem payload).

WHY tg-cli NEEDS NO CHANGE
    tg-cli already exposes a durable, machine-readable inbound stream: ``tg replies user --json``
    returns ``[{ts, id, direction, from, text, pane}]`` per message, backed by an append-only
    JSONL. pm consumes it read-only, exactly like the task adapter consumes ``task list --json``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..events import Event, EventKind, new_event

if TYPE_CHECKING:  # annotation-only
    from ..intake import IntakeEvent

SOURCE = "telegram"

# tg-cli's `tg replies` piped default is 20 sends; ask for an effectively unbounded window so a
# busy day of messages is not silently truncated (dedupe by id makes the overlap harmless).
_FETCH_LIMIT = "100000"

_TITLE_MAX = 80


def plan_capture(records: Iterable[dict], ledger: dict[str, IntakeEvent]) -> list[Event]:
    """``intake_captured`` events for every inbound message not already in the ledger."""
    seen = set(ledger)
    events: list[Event] = []
    for rec in records:
        external_id = str(rec.get("id") or "")
        if not external_id:
            continue  # a record with no message id can't be identified
        intake_id = f"tg:{external_id}"
        if intake_id in seen:
            continue  # dedupe: already captured
        seen.add(intake_id)
        events.append(new_event(EventKind.INTAKE_CAPTURED, "", {
            "id": intake_id, "source": SOURCE, "external_id": external_id,
            "sender": str(rec.get("from") or ""), "text": str(rec.get("text") or ""),
            "ts_ms": rec.get("ts"), "pane": str(rec.get("pane") or ""),
        }))
    return events


def default_processor(intake: IntakeEvent) -> dict:
    """Turn a captured intake into a WorkItem ``intake`` payload (M3: no classifier yet).

    Trivial and total — it never raises, so the happy path always processes. The bounded-retry /
    dead-letter machinery is exercised when a real (fallible) classifier replaces this later.
    """
    text = intake.text.strip()
    title = (text[:_TITLE_MAX] or "(no text)")
    payload = {
        "id": f"intake:{intake.id}",
        "title": title,
        "state": "seen",
        "evidence": [{"kind": "telegram-message", "uri": intake.external_id}],
    }
    return payload


def fetch_tg_records(since: str = "") -> list[dict]:
    """Shell out to ``tg replies user --all-sessions --json`` and return the inbound records.

    Effectful and lazy (``subprocess`` imported here). ``since`` is an optional tg date filter
    (ISO date/datetime or relative like ``3d``). Raises ``RuntimeError`` with an actionable message
    if ``tg`` is missing or the call fails, so the CLI renders a structured error, not a traceback.
    """
    import subprocess

    cmd = ["tg", "replies", "user", "--all-sessions", "--json", "-n", _FETCH_LIMIT]
    if since:
        cmd += ["--since", since]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("`tg` CLI not found on PATH — install tg-cli to ingest Telegram") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"`tg replies` failed (exit {proc.returncode}): {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "[]")
    if not isinstance(data, list):
        raise RuntimeError("`tg replies --json` did not return a JSON array")
    return [r for r in data if isinstance(r, dict)]
