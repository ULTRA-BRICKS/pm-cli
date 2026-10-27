"""Owner-private Telegram notification for significant task-audit changes.

Accessed via: ``pm chatgpt audit`` after a run report is built (``notify_report``).

Invariants:
- ONE message per run, only for blocked/incomplete conversations that are new or changed since
  the previous run's report, and for accounts that newly became unavailable. It is one-way: the
  existing ``tg`` CLI sends to the configured owner chat (``tg --automated --tag report
  --format plain TEXT``); no question, button, call, file or reply.
- The text carries only ids, canonical URLs, state tokens, reason tokens and counts. Every
  interpolated value must match a strict token pattern or it is dropped; conversation text,
  titles, scores and remaining steps never reach the message.
- Delivery is at-most-once per dedup key. A ``sent`` key is never resent. A key whose delivery is
  ``uncertain`` (``tg`` ran but the outcome is unknown) is NEVER replayed. Only definite failures
  are retried, after a cooldown and a bounded number of attempts.
- A send failure never raises; a log-append failure after a send is reported, never replayed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .audit_guard import LockBusy, RunLock
from .audit_store import AuditStore
from .audit_urls import conversation_url
from .tg_transport import discover_owner

SENT = "sent"
UNCERTAIN = "uncertain"
FAILED = "failed"

NOTIFY_LOCK = "notify.lock"
TG_TIMEOUT = 30
OUTPUT_LIMIT = 4096
MAX_TEXT = 3500
KEY_SCHEMA = "pm-audit-notify-v1"
_SAFE = re.compile(r"[A-Za-z0-9._:/_-]{1,128}")
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_TG_ARGS = ("--automated", "--tag", "report", "--format", "plain")

# "ChatGPT task audit"; the header guarantees the text never starts with "-".
HEADER = "\u0410\u0443\u0434\u0438\u0442 \u0437\u0430\u0434\u0430\u0447 ChatGPT"
UNAVAILABLE_LABEL = "unavailable"

Send = Callable[[str], str]
_RECEIPT = re.compile(rb"#(\d{1,12})\b")


class Delivery(str):
    """A delivery verdict that still equals its plain token and may carry the ``tg`` receipt."""

    receipt: str | None = None

    def __new__(cls, verdict: str, receipt: str | None = None):
        self = super().__new__(cls, verdict)
        self.receipt = receipt
        return self


def _safe(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE.fullmatch(value) else None


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def _key(*parts: str) -> str:
    return hashlib.sha256("\0".join((KEY_SCHEMA, *parts)).encode()).hexdigest()[:32]


def _text_for(items: list[dict], extra: int) -> str:
    lines = [item["line"] for item in items]
    while True:
        tail = [f"+{extra} more"] if extra else []
        text = "\n".join([HEADER, *lines, *tail])
        if len(text) <= MAX_TEXT or not lines:
            return text[:MAX_TEXT]
        lines.pop()
        extra += 1


def _previous_conversations(previous: dict | None) -> dict[tuple[str, str], tuple[object, object]]:
    found: dict[tuple[str, str], tuple[object, object]] = {}
    accounts = previous.get("accounts") if isinstance(previous, dict) else None
    if not isinstance(accounts, dict):
        return found
    for account, body in accounts.items():
        for conv in (body.get("conversations") if isinstance(body, dict) else None) or []:
            if isinstance(conv, dict):
                found[(account, conv.get("conversation_id"))] = (
                    conv.get("task_state"),
                    conv.get("fingerprint"),
                )
    return found


def _previous_unavailable(previous: dict | None, account: str) -> str | None:
    accounts = previous.get("accounts") if isinstance(previous, dict) else None
    body = accounts.get(account) if isinstance(accounts, dict) else None
    if isinstance(body, dict) and body.get("status") == "unavailable":
        return str(body.get("reason", ""))
    return None


def significant_items(
    report: dict, previous: dict | None, retry: frozenset[str] = frozenset()
) -> list[dict]:
    """Deltas worth a message, as ``{"key", "line"}``; unsafe values are dropped, not escaped."""
    seen = _previous_conversations(previous)
    out: list[dict] = []
    accounts = report.get("accounts") if isinstance(report, dict) else None
    if not isinstance(accounts, dict):
        return out
    for account, body in accounts.items():
        name = _safe(account)
        if name is None or not isinstance(body, dict):
            continue
        if body.get("status") == "unavailable":
            reason = body.get("reason", "")
            prior = _previous_unavailable(previous, account)
            token = _safe(reason) if reason else "unspecified"
            key = _key(account, "unavailable", str(reason))
            if token is not None and (prior != reason or key in retry):
                out.append(
                    {
                        "key": key,
                        "line": f"{name} | {UNAVAILABLE_LABEL} | {token}",
                    }
                )
            continue
        if body.get("status") not in ("ok", "partial"):
            continue
        for conv in body.get("conversations") or []:
            if not isinstance(conv, dict) or conv.get("task_state") not in (
                "blocked",
                "incomplete",
            ):
                continue
            cid, state, fp = (
                conv.get("conversation_id"),
                conv["task_state"],
                conv.get("fingerprint"),
            )
            if _safe(cid) is None or not isinstance(fp, str):
                continue
            before = seen.get((account, cid))
            key = _key(account, cid, state, fp)
            if key not in retry and before is not None and before == (state, fp):
                continue
            recovery = _token(conv.get("recovery_state")) or "unknown"
            reasons = (
                [r for r in (conv.get("reasons") or []) if _safe(r) is not None][:6]
                if isinstance(conv.get("reasons"), list)
                else []
            )
            line = f"{name} | {state}/{recovery} | {','.join(reasons) or '-'} | {conversation_url(cid)}"
            out.append({"key": key, "line": line})
    return out


def _history(events: list[dict]) -> dict[str, dict]:
    book: dict[str, dict] = {}
    for event in events:
        key, kind, at = event.get("key"), event.get("kind"), event.get("at")
        if not isinstance(key, str) or type(at) not in (int, float):
            continue
        entry = book.setdefault(
            key,
            {"sent": False, "uncertain": False, "failed": 0, "last_failed": None, "pending": False},
        )
        if kind == "delivery_intent":
            entry["pending"] = True
            continue
        entry["pending"] = False
        if kind == "sent":
            entry["sent"] = True
        elif kind == "delivery_uncertain":
            entry["uncertain"] = True
        elif kind == "delivery_failed":
            entry["failed"] += 1
            entry["last_failed"] = (
                at if entry["last_failed"] is None else max(entry["last_failed"], at)
            )
    for entry in book.values():
        # An intent with no outcome means the process died around the send: never replay it.
        entry["uncertain"] = entry["uncertain"] or entry["pending"]
    return book


def make_tg_sender(run=subprocess.run, owner=discover_owner, binary: str = "tg") -> Send:
    """The default sender: owner check, local dry-run, then one bounded real send."""

    def send(text: str) -> str:
        if owner() is None:
            return FAILED
        argv = [binary, *_TG_ARGS, text]
        common = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
        }
        try:
            dry = run([binary, "--dry-run", *_TG_ARGS, text], timeout=TG_TIMEOUT, **common)
        except (OSError, subprocess.SubprocessError):
            return FAILED  # nothing was sent: the dry run has no network
        if dry.returncode != 0:
            return FAILED
        try:
            proc = run(argv, timeout=TG_TIMEOUT, **common)
        except FileNotFoundError:
            return FAILED
        except Exception:  # noqa: BLE001 - after launch the delivery state is unknown
            return UNCERTAIN
        out = proc.stdout or b""
        head = out[:OUTPUT_LIMIT].splitlines()[:1]
        first = head[0].decode("utf-8", "replace") if head else ""
        if proc.returncode == 0 and first.startswith("OK"):
            found = _RECEIPT.search(head[0])
            return Delivery(SENT, found.group(1).decode() if found else None)
        if proc.returncode in (78, 2):
            return FAILED
        return UNCERTAIN

    return send


def _result(outcome: str, **fields) -> dict:
    base = {
        "outcome": outcome,
        "significant": 0,
        "sent_items": 0,
        "suppressed_uncertain": 0,
        "suppressed_cooldown": 0,
        "suppressed_sent": 0,
        "keys": [],
    }
    base.update(fields)
    return base


def _claim(report, previous, store, now, cooldown_seconds, max_items, max_attempts):
    """Decide what is eligible and persist its delivery intents; returns a result or the claim.

    Runs under the notification lock so two overlapping audits can never both claim a key.
    """
    log = store.notification_log()
    try:
        book = _history(log.read_all())
    except Exception:  # noqa: BLE001 - unreadable history must not become a replay
        return _result("error", log="read-failed")
    retry = frozenset(
        k for k, h in book.items() if h["failed"] and not h["sent"] and not h["uncertain"]
    )
    items = significant_items(report, previous, retry)
    if not items:
        return _result("none-significant")
    counts = {"suppressed_uncertain": 0, "suppressed_cooldown": 0, "suppressed_sent": 0}
    eligible: list[dict] = []
    for item in items:
        h = book.get(item["key"])
        if h is None:
            eligible.append(item)
        elif h["sent"]:
            counts["suppressed_sent"] += 1
        elif h["uncertain"]:
            counts["suppressed_uncertain"] += 1
        elif h["failed"] >= max_attempts or now - h["last_failed"] < cooldown_seconds:
            counts["suppressed_cooldown"] += 1
        else:
            eligible.append(item)
    base = {"significant": len(items), **counts}
    if not eligible:
        return _result("suppressed", **base)
    batch = eligible[:max_items]
    text = _text_for(batch, len(eligible) - len(batch))
    try:
        for item in eligible:
            log.append({"kind": "delivery_intent", "at": now, "key": item["key"]})
    except Exception:  # noqa: BLE001 - without a durable intent nothing may be sent
        return _result("error", **base, log="intent-append-failed")
    return log, eligible, text, base


def notify_report(
    report: dict,
    previous: dict | None,
    store: AuditStore,
    *,
    now: float,
    send: Send | None = None,
    cooldown_seconds: float = 3600.0,
    max_items: int = 20,
    max_attempts: int = 3,
) -> dict:
    try:
        with RunLock(Path(store.directory) / NOTIFY_LOCK):
            claimed = _claim(
                report, previous, store, now, cooldown_seconds, max_items, max_attempts
            )
    except LockBusy:
        return _result("busy")  # another run is claiming deliveries; its intents dedup this one
    except Exception:  # noqa: BLE001 - an unusable lock must never become an unguarded send
        return _result("error", log="lock-failed")
    if isinstance(claimed, dict):
        return claimed
    log, eligible, text, base = claimed
    try:
        verdict = (send or make_tg_sender())(text)
    except Exception:  # noqa: BLE001 - a raising sender has an unknown delivery state
        verdict = UNCERTAIN
    kind, outcome = {
        SENT: ("sent", "sent"),
        FAILED: ("delivery_failed", "delivery_failed"),
    }.get(verdict, ("delivery_uncertain", "delivery_uncertain"))
    receipt = getattr(verdict, "receipt", None) if kind == "sent" else None
    fields = {
        **base,
        "sent_items": len(eligible) if kind == "sent" else 0,
        "keys": [i["key"] for i in eligible][:20],
    }
    if receipt:
        fields["receipt"] = receipt
    try:
        for item in eligible:
            event = {"kind": kind, "at": now, "key": item["key"]}
            if receipt:
                event["receipt"] = receipt
            log.append(event)
    except Exception:  # noqa: BLE001 - the send already happened; report, never replay
        fields["log"] = "append-failed"
    return _result(outcome, **fields)
