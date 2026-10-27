"""Pure builders for the audit run report (schema 1): account blocks, totals, bounds, rendering.

Accessed via: ``audit_collector`` (fills account blocks), ``pm chatgpt audit`` (text/JSON output,
exit code) and ``audit_notify`` (reads the finished dict).

Invariants: no I/O, no clock. An account is ``unavailable`` (nothing could be audited), ``partial``
(anything missing: an incomplete scope, an error, a skipped conversation, a budget cut or a stop)
or ``ok`` (nothing missing); the run status is the worst of its accounts and an unknown status is
never a success. Reports carry ids, URLs, states, reasons and counts only, never conversation text.
A report is bounded twice: at most ``MAX_REPORTED`` conversations per account (the newest are kept,
the rest counted in ``omitted``) and at most ``MAX_JSON_BYTES`` serialized (conversations are dropped
from the end, never silently: ``omitted`` records them). ``totals`` always count EVERY conversation.
"""

from __future__ import annotations

import json
import math

MAX_REPORTED = 500
MAX_JSON_BYTES = 256 * 1024
MAX_PROJECT_SCOPES = 100
MAX_TEXT_LINES = 25
SCHEMA = 1
TOOL = "pm-chatgpt-audit"
STATUS_ORDER = ("ok", "partial", "unavailable")
TASK_STATES = ("done", "incomplete", "blocked", "working", "unknown")
PROJECT_SUMMARY = "project:*"


def new_account_report() -> dict:
    return {
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


def mark_unavailable(
    account: dict,
    reason: str,
    *,
    observed_account: str | None = None,
    not_before: float | None = None,
) -> dict:
    account["status"], account["reason"] = "unavailable", reason
    if observed_account is not None:
        account["observed_account"] = observed_account
    if not_before is not None:
        account["not_before"] = not_before
    return account


def _partial_reason(account: dict) -> str:
    cov = account["coverage"]
    if cov["stopped"] is not None:
        return f"http-{cov['stopped']['status']}"
    if cov["budget_exhausted"]:
        return "budget-exhausted"
    for scope in account["scopes"].values():
        if not scope["complete"]:
            return scope["reason"] or "incomplete"
    if cov["errors"] or cov["skipped"]:
        return "read-errors"
    return "incomplete"


def finish_account(account: dict) -> dict:
    """Decide the account's final status from its coverage and order its conversations."""
    cov = account["coverage"]
    account["conversations"].sort(key=lambda c: (-c["updated_at"], c["conversation_id"]))
    if account["status"] != "unavailable":
        incomplete = (
            any(not s["complete"] for s in account["scopes"].values())
            or cov["errors"] > 0
            or bool(cov["skipped"])
            or cov["budget_exhausted"]
            or cov["stopped"] is not None
        )
        account["status"] = "partial" if incomplete else "ok"
        account["reason"] = _partial_reason(account) if incomplete else ""
    cov["partial"] = account["status"] != "ok"
    return account


def _worst(statuses) -> str:
    worst = "ok"
    for status in statuses:
        if status not in STATUS_ORDER:
            return "unavailable"
        if STATUS_ORDER.index(status) > STATUS_ORDER.index(worst):
            worst = status
    return worst


def _totals(accounts: dict) -> dict:
    totals = {"conversations": 0, **dict.fromkeys(TASK_STATES, 0)}
    for account in accounts.values():
        for conversation in account["conversations"]:
            totals["conversations"] += 1
            state = conversation["task_state"]
            totals[state if state in TASK_STATES else "unknown"] += 1
    return totals


def _collapse_projects(account: dict) -> None:
    """Many clean project listings become one exact summary; any problem stays individual."""
    scopes = account["scopes"]
    clean = [
        k
        for k, v in scopes.items()
        if k.startswith("project:") and v["complete"] and not v["reason"]
    ]
    if len(clean) <= MAX_PROJECT_SCOPES:
        return
    summary = {"listed": 0, "pages": 0, "complete": True, "reason": "", "total": None}
    for key in clean:
        scope = scopes.pop(key)
        summary["listed"] += scope["listed"]
        summary["pages"] += scope["pages"]
    summary["count"] = len(clean)
    scopes[PROJECT_SUMMARY] = summary


def _dump(report: dict) -> str:
    return json.dumps(report, sort_keys=True, allow_nan=False)


def _drop_from_end(report: dict, count: int) -> None:
    """Drop ``count`` conversations from the end of the largest account list."""
    while count > 0:
        name = max(report["accounts"], key=lambda n: len(report["accounts"][n]["conversations"]))
        account = report["accounts"][name]
        if not account["conversations"]:
            return
        take = min(count, len(account["conversations"]))
        del account["conversations"][-take:]
        account["omitted"] += take
        count -= take


def fit(report: dict, max_bytes: int = MAX_JSON_BYTES) -> dict:
    """Shrink ``report`` in place until it serializes within ``max_bytes`` (idempotent)."""
    size = len(_dump(report).encode())
    while size > max_bytes:
        listed = sum(len(a["conversations"]) for a in report["accounts"].values())
        if listed == 0:
            break
        per = max(size // (listed + 1), 1)
        _drop_from_end(report, max(1, math.ceil((size - max_bytes) / per)))
        size = len(_dump(report).encode())
    return report


def build_report(
    *,
    generated_at: float,
    days: float,
    workspaces,
    accounts: dict,
    classifier: dict,
    thresholds_digest: str,
    auto_continue: dict,
    pm_version: str,
) -> dict:
    totals = _totals(accounts)
    for account in accounts.values():
        _collapse_projects(account)
        extra = len(account["conversations"]) - MAX_REPORTED
        if extra > 0:
            del account["conversations"][MAX_REPORTED:]
            account["omitted"] += extra
    report = {
        "schema": SCHEMA,
        "tool": TOOL,
        "pm_version": pm_version,
        "generated_at": generated_at,
        "days": days,
        "cutoff": generated_at - days * 86400,
        "workspaces": list(workspaces),
        "status": _worst(a["status"] for a in accounts.values()),
        "classifier": classifier,
        "thresholds_digest": thresholds_digest,
        "auto_continue": auto_continue,
        "accounts": accounts,
        "totals": totals,
    }
    return fit(report)


def exit_code(report: dict) -> int:
    return {"ok": 0, "partial": 3}.get(report.get("status"), 4)


def to_json(report: dict) -> str:
    """The one bounded, strict JSON document for ``--json`` (re-fitted: ``notify`` was added)."""
    return _dump(fit(report))


def _line(conversation: dict) -> str:
    flags = " archived" if conversation["archived"] else ""
    reason = ",".join(conversation["reasons"][:2])
    return (
        f"  {conversation['task_state']:<10} {conversation['url']}{flags}"
        f"  [{conversation['source']}] {reason}"
    ).rstrip()


def render_text(report: dict) -> str:
    """Compact human summary: coverage per account and the conversations needing attention."""
    totals = report["totals"]
    classifier = report["classifier"]
    gate = report["auto_continue"]
    summary = ", ".join(f"{totals[s]} {s}" for s in TASK_STATES)
    verb = "allowed" if gate["allowed"] else "denied"
    head = (
        f"pm chatgpt audit: {report['status']} ({report['days']:g} day window, "
        f"{totals['conversations']} conversations: {summary})"
    )
    model = (
        f"classifier: {classifier['status']} {classifier['model'] or '-'} "
        f"prompt {classifier['prompt_version']}; auto-continue {verb} ({gate['reason']})"
    )
    lines = [head, model]
    for name, account in report["accounts"].items():
        cov = account["coverage"]
        head = f"{name}: {account['status']}"
        if account["reason"]:
            head += f" ({account['reason']})"
        lines.append(head)
        if account["status"] == "unavailable":
            if account["not_before"] is not None:
                lines.append(f"  not before {account['not_before']:.0f}")
            continue
        skipped = sum(cov["skipped"].values())
        lines.append(
            f"  listed {cov['listed_total']}, in window {cov['in_window']}, read {cov['read']}, "
            f"cached {cov['cached']}, skipped {skipped}, errors {cov['errors']}, "
            f"requests {cov['requests']}"
        )
        shown = [c for c in account["conversations"] if c["task_state"] != "done"]
        for conversation in shown[:MAX_TEXT_LINES]:
            lines.append(_line(conversation))
        if len(shown) > MAX_TEXT_LINES:
            lines.append(f"  ... {len(shown) - MAX_TEXT_LINES} more not shown")
        if account["omitted"]:
            lines.append(f"  {account['omitted']} conversations omitted from the report")
    return "\n".join(lines)
