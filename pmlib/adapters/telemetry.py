"""The telemetry adapter — build the executor ledger from a snapshot or best-effort `tg-ctl status`.

``parse_executors`` (PURE) reads a well-defined snapshot array — the primary, tested interface.
``parse_tgctl_status`` (PURE) best-effort-parses ``tg-ctl status`` human text into executors, marking
them ``no-telemetry`` when usage telemetry is not installed (so the router won't trust their quota).
``fetch_executors`` (EFFECTFUL) runs ``tg-ctl status``. There is no stable machine-readable usage
contract yet, so this is best-effort; a clean snapshot via ``--from-file`` is the reliable path.
"""

from __future__ import annotations

import re

from ..executors import Executor

# "<...> %<pane> (<harness>, <cwd>)" — the registration/target lines of `tg-ctl status`.
_REG_RE = re.compile(r"%(?P<pane>\d+)\s*\((?P<harness>[\w-]+),\s*(?P<cwd>[^)]+)\)")


def parse_executors(records: list) -> list[Executor]:
    """Build executors from a snapshot array. Records without an ``id`` are skipped."""
    out: list[Executor] = []
    for rec in records:
        if not isinstance(rec, dict) or not rec.get("id"):
            continue
        out.append(_executor_from_record(rec))
    return out


def _executor_from_record(rec: dict) -> Executor:
    reliability = str(rec.get("reliability") or "no-telemetry")
    return Executor(
        id=str(rec["id"]), harness=str(rec.get("harness", "")), model=str(rec.get("model", "")),
        pane_id=str(rec.get("pane_id", "")), repo=str(rec.get("repo", "")), reliability=reliability,
        limit_remaining=_opt_float(rec.get("limit_remaining")), reset_at=str(rec.get("reset_at", "")),
        context_pressure=_opt_float(rec.get("context_pressure")),
        active_tasks=int(rec.get("active_tasks") or 0), local=bool(rec.get("local", False)),
    )


def _opt_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_tgctl_status(text: str) -> list[Executor]:
    """Best-effort executors from ``tg-ctl status`` text (pane, harness, repo; no-telemetry quota)."""
    usage_absent = "usage telemetry: not installed" in text.lower()
    reliability = "no-telemetry" if usage_absent else "degraded"
    seen: dict[str, Executor] = {}
    for match in _REG_RE.finditer(text):
        pane = "%" + match.group("pane")
        seen[pane] = Executor(
            id=f"{match.group('harness')}:{pane}", harness=match.group("harness"),
            pane_id=pane, repo=match.group("cwd").strip(), reliability=reliability)
    return list(seen.values())


def fetch_executors() -> list[Executor]:
    """Run ``tg-ctl status`` and best-effort-parse it. Effectful and lazy; never raises on absence.

    Returns [] (not an error) when ``tg-ctl`` is missing or produces nothing parseable — routing then
    reports ``wait`` ("no executors known"), which is the honest state.
    """
    import subprocess

    try:
        proc = subprocess.run(["tg-ctl", "status"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return []
    return parse_tgctl_status(proc.stdout or "")
