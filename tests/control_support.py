"""Shared builders for the ChatGPT control-plane tests: fake telemetry, clock and services.

Nothing here touches Guardian, Telegram or the real state directory; every collaborator is a
plain in-memory fake that records its calls.
"""

from __future__ import annotations

from pathlib import Path

NOW = 1792254000.0
CHAT_A = "https://chatgpt.com/c/alpha"
CHAT_B = "https://chatgpt.com/c/beta"
ACCOUNTS = ("home", "glide.vc")


def row(chat: str = CHAT_A, **kw) -> dict:
    """A fresh, enrolled, complete, quiet, blocker-free session row (the eligible baseline)."""
    base = {
        "chat": chat,
        "title": "Canvas work",
        "watched": True,
        "present": True,
        "page_count": 1,
        "observation": "current",
        "error": "",
        "error_detail": "",
        "status": "complete",
        "failure_evidence": "",
        "reason": "",
        "running": False,
        "last_checked_at": NOW - 10,
        "last_go_at": None,
        "last_go_state": None,
        "continuation": "completed",
        "continuation_current": True,
        "continuation_turn": "turn-1",
        "last_progress_at": NOW - 600,
        "no_visible_progress": False,
        "next_check_at": NOW + 50,
        "model": "Sol",
        "effort": "Extra High",
        "turn": "turn-1",
    }
    base.update(kw)
    return base


def account(workspace: str, **kw) -> dict:
    base = {
        "workspace": workspace,
        "ok": True,
        "error": None,
        "stale": False,
        "generated_at": NOW - 5,
        "heartbeat_at": NOW - 5,
        "connection": "connected",
        "summary": {"enrolled": 1, "present": 1, "running": 0, "blocked": 0},
        "service": {"active": "active"},
    }
    base.update(kw)
    return base


def down_account(workspace: str, error: str = "telemetry down") -> dict:
    return {
        "workspace": workspace,
        "ok": False,
        "error": error,
        "stale": True,
        "generated_at": None,
        "summary": None,
        "service": {},
    }


def combined(per_account: dict[str, list[dict] | None], **account_kw) -> dict:
    """``None`` rows mean the account is down; otherwise rows are tagged with their account."""
    accounts, sessions = [], []
    for workspace, rows in per_account.items():
        if rows is None:
            accounts.append(down_account(workspace))
            continue
        accounts.append(account(workspace, **account_kw))
        sessions.extend(dict(r, account=workspace) for r in rows)
    return {"workspace": "all", "accounts": accounts, "sessions": sessions, "events": []}


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTelemetry:
    """Returns ``data`` filtered to the requested accounts, exactly like ``fetch_combined``."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, accounts) -> dict:
        wanted = tuple(accounts)
        self.calls.append(wanted)
        return {
            "workspace": "all",
            "accounts": [a for a in self.data["accounts"] if a["workspace"] in wanted],
            "sessions": [s for s in self.data["sessions"] if s["account"] in wanted],
            "events": [e for e in self.data["events"] if e["account"] in wanted],
        }


def make_services(tmp_path: Path, data: dict | None = None, clock: Clock | None = None):
    from pmlib.chatgpt_control.service import Services

    telemetry = FakeTelemetry(
        data if data is not None else combined({"home": [row()], "glide.vc": [row(CHAT_B)]})
    )
    clock = clock or Clock()
    services = Services(
        state_dir=tmp_path / "state",
        clock=clock,
        fetch=telemetry,
        accounts=lambda: {"accounts": [{"workspace": w} for w in ACCOUNTS]},
    )
    return services, telemetry, clock
