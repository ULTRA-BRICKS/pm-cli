"""Read-only Guardian snapshot client; terminal presentation is a separate module."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys

REQUIRED = (
    "workspace",
    "generated_at",
    "heartbeat_at",
    "connection",
    "stale",
    "max_pages",
    "page_count",
    "summary",
    "sessions",
)
COUNTS = ("enrolled", "present", "running", "blocked", "sent_24h", "resumed_24h", "completed_24h")
ACCOUNTS = ("home", "glide.vc")
SERVICE_UNITS = {
    "home": "chatgpt-guardian-home.service",
    "glide.vc": "chatgpt-guardian-glide.service",
}
# Guardian's permanent chat URL contract; '*' (bulk) and anything else is not a chat target.
CHAT_URL = re.compile(r"https://chatgpt\.com/c/[A-Za-z0-9_-]+/?")


class MonitorError(ValueError):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def validate_snapshot(data, workspace):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise MonitorError("Unsupported Guardian snapshot schema version")
    if any(k not in data for k in REQUIRED):
        raise MonitorError("Guardian snapshot is missing required fields")
    if data["workspace"] != workspace:
        raise MonitorError("Guardian snapshot belongs to another workspace")
    if not isinstance(data["sessions"], list) or any(
        not isinstance(s, dict) for s in data["sessions"]
    ):
        raise MonitorError("Guardian sessions must be a list of objects")
    summary = data["summary"]
    if not isinstance(summary, dict) or any(
        type(summary.get(k)) is not int or summary[k] < 0 for k in COUNTS
    ):
        raise MonitorError("Guardian summary counters are missing or invalid")
    if any(not isinstance(s.get("chat"), str) for s in data["sessions"]):
        raise MonitorError("Guardian session identity is missing")
    if not isinstance(data.get("events", []), list) or any(
        not isinstance(e, dict) for e in data.get("events", [])
    ):
        raise MonitorError("Guardian events must be a list of objects")
    for key in ("generated_at", "heartbeat_at"):
        value = data[key]
        if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
            raise MonitorError("Guardian timestamp is invalid")
    if type(data["stale"]) is not bool or not isinstance(data["connection"], str):
        raise MonitorError("Guardian freshness fields are invalid")
    if not isinstance(data.get("service", {}), dict):
        raise MonitorError("Guardian service metadata is invalid")
    if not isinstance(data.get("limitations", []), list) or any(
        not isinstance(x, str) for x in data.get("limitations", [])
    ):
        raise MonitorError("Guardian limitations must be text entries")
    for row in data["sessions"]:
        for key in ("title", "status", "reason", "continuation", "model", "effort"):
            value = row.get(key)
            if value is not None and not isinstance(value, str):
                raise MonitorError("Guardian session text field is invalid")
        for key in ("last_checked_at", "last_go_at", "next_check_at"):
            value = row.get(key)
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
                raise MonitorError("Guardian session timestamp is invalid")
    return data


def fetch_snapshot(workspace, state=None):
    command = ["chatgpt-guardian", "--workspace", workspace]
    if state is not None:
        command.append(f"--state={state}")
    command += ["monitor", "--json"]
    try:
        timeout = float(os.environ.get("PMLIB_MONITOR_TIMEOUT", "10"))
        if not math.isfinite(timeout) or not 0.1 <= timeout <= 30:
            raise ValueError("Monitor timeout must be 0.1 to 30 seconds")
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        raise MonitorError(
            "chatgpt-guardian unavailable; install it in the pm environment", 127
        ) from None
    except subprocess.TimeoutExpired:
        raise MonitorError("Snapshot read timed out; service state is unconfirmed") from None
    except OSError as exc:
        raise MonitorError("Cannot read Guardian snapshot: " + type(exc).__name__) from exc
    if len(result.stdout.encode()) > 1048576:
        raise MonitorError("Guardian snapshot exceeds 1 MiB")
    if result.returncode:
        if result.returncode == 2 and "invalid choice" in result.stderr:
            raise MonitorError("Update chatgpt-guardian: monitor command is not installed")
        raise MonitorError("Guardian snapshot unavailable: " + result.stderr.strip()[:500])
    try:
        return validate_snapshot(json.loads(result.stdout), workspace)
    except (ValueError, TypeError) as exc:
        raise MonitorError("Invalid Guardian snapshot: " + str(exc)) from exc


def _fetch_one(workspace):
    try:
        return workspace, fetch_snapshot(workspace), None
    except Exception as exc:  # noqa: BLE001 - one account's failure must never hide the other
        return workspace, None, str(exc) or type(exc).__name__


def combine(results):
    """Merge per-account ``(workspace, snapshot|None, error|None)`` into one labelled view.

    Every row and event carries its ``account``; nothing is shared between accounts, so an
    identical chat ID in two accounts stays two distinct targets.
    """
    accounts, sessions, events = [], [], []
    for workspace, snapshot, error in results:
        if snapshot is None:
            accounts.append(
                {
                    "workspace": workspace,
                    "ok": False,
                    "error": error or "no snapshot available",
                    "stale": True,
                    "generated_at": None,
                    "summary": None,
                    "service": {},
                }
            )
            continue
        entry = {k: v for k, v in snapshot.items() if k not in ("sessions", "events")}
        entry.update(workspace=workspace, ok=True, error=None)
        accounts.append(entry)
        sessions.extend(dict(row, account=workspace) for row in snapshot["sessions"])
        events.extend(dict(event, account=workspace) for event in snapshot.get("events", []))
    return {"workspace": "all", "accounts": accounts, "sessions": sessions, "events": events}


def fetch_combined(accounts=ACCOUNTS):
    """Read every account concurrently; a failed account becomes an error entry, never a raise."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        return combine(list(pool.map(_fetch_one, accounts)))


def _unknown_service(unit, error):
    return {"unit": unit, "configured": None, "active": "unknown", "error": error}


def service_state(workspace):
    """Read-only, bounded systemd user-unit probe; an unreadable unit is unknown, not stopped."""
    unit = SERVICE_UNITS[workspace]
    command = [
        "systemctl",
        "--user",
        "--no-pager",
        "show",
        unit,
        "--property=LoadState",
        "--property=ActiveState",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except FileNotFoundError:
        return _unknown_service(unit, "systemctl unavailable")
    except subprocess.TimeoutExpired:
        return _unknown_service(unit, "systemctl timed out")
    except OSError as exc:
        return _unknown_service(unit, "systemctl failed: " + type(exc).__name__)
    if result.returncode:
        return _unknown_service(unit, f"systemctl exited {result.returncode}")
    props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    load, active = props.get("LoadState"), props.get("ActiveState")
    if load is None or active is None:
        return _unknown_service(unit, "unrecognized systemctl output")
    if load == "not-found":
        return {"unit": unit, "configured": False, "active": "not-installed", "error": None}
    if load != "loaded":
        return _unknown_service(unit, f"unit load state {load}")
    return {"unit": unit, "configured": True, "active": active, "error": None}


def accounts_report(accounts=ACCOUNTS):
    """Per-account configuration plus observed monitor counts; missing data stays ``None``."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=2 * len(accounts)) as pool:
        services = [pool.submit(service_state, ws) for ws in accounts]
        snapshots = [pool.submit(_fetch_one, ws) for ws in accounts]
        rows = []
        for workspace, service, snapshot in zip(accounts, services, snapshots, strict=True):
            state, (_, data, error) = service.result(), snapshot.result()
            if data is None:
                monitor = {
                    "ok": False,
                    "error": error,
                    "stale": None,
                    "generated_at": None,
                    "sessions": None,
                    "enrolled": None,
                    "present": None,
                    "running": None,
                    "blocked": None,
                }
            else:
                summary = data["summary"]
                monitor = {
                    "ok": True,
                    "error": None,
                    "stale": data["stale"],
                    "generated_at": data["generated_at"],
                    "sessions": len(data["sessions"]),
                    "enrolled": summary["enrolled"],
                    "present": summary["present"],
                    "running": summary["running"],
                    "blocked": summary["blocked"],
                }
            rows.append(
                {
                    "workspace": workspace,
                    "unit": SERVICE_UNITS[workspace],
                    "configured": state["configured"],
                    "service": {"active": state["active"], "error": state["error"]},
                    "monitor": monitor,
                }
            )
    return {"accounts": rows}


def configure(parser):
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--json", dest="monitor_json", action="store_true", help="One read-only JSON snapshot"
    )
    modes.add_argument("--tui", action="store_true", help="Interactive terminal monitor")
    modes.add_argument("--follow", action="store_true", help="Interactive terminal monitor")
    parser.add_argument(
        "--interval",
        type=int,
        choices=range(2, 61),
        default=5,
        metavar="2..60",
        help="Display refresh seconds; does not change browser polling",
    )


def print_snapshot(data, label=None):
    """Plain table for one account; ``label`` prefixes each row when accounts are combined."""
    from .chatgpt_tui import clean, stamp

    s = data["summary"]
    print(
        f"ChatGPT Guardian | {clean(data['workspace'])} | source {stamp(data['generated_at'])} Europe/Belgrade"
    )
    if data.get("stale"):
        print("WARNING: STALE snapshot; this is not a current progress confirmation")
    print(
        f"enrolled={s['enrolled']} present={s['present']} available={s.get('available', 'unknown')} running={s['running']} blocked={s['blocked']}"
    )
    print(f"24h sent={s['sent_24h']} continued={s['resumed_24h']} completed={s['completed_24h']}")
    print("SESSION | STATUS | LAST GO / STATE | CONTINUATION | LAST CHECK -> NEXT CHECK")
    prefix = [f"[{clean(label)}]"] if label else []
    for row in data["sessions"]:
        print(
            *prefix,
            clean(row["chat"].rsplit("/", 1)[-1]),
            clean(row.get("title", ""))[:48],
            "|",
            clean(row.get("status", "unknown")),
            "|",
            stamp(row.get("last_go_at")),
            clean(row.get("last_go_state", "")),
            "|",
            clean(row.get("continuation", "unknown")),
            "|",
            stamp(row.get("last_checked_at")),
            "->",
            stamp(row.get("next_check_at")),
            "|",
            clean(row.get("reason", "")),
        )
    print(clean(data.get("coverage_note", "Coverage is limited to observed sessions.")))
    for message in data.get("limitations", []):
        print("LIMITATION:", clean(message))
    if len(data["sessions"]) > 64:
        print(
            f"warning: {len(data['sessions'])} sessions; use TUI search to navigate",
            file=sys.stderr,
        )


def print_combined(data):
    from .chatgpt_tui import clean

    for account in data["accounts"]:
        name = account["workspace"]
        if not account["ok"]:
            print(f"{clean(name)}: UNAVAILABLE {clean(account['error'])}")
            continue
        rows = [r for r in data["sessions"] if r["account"] == name]
        print_snapshot(dict(account, sessions=rows), label=name)


def run_accounts(args):
    from .chatgpt_tui import clean, stamp

    if args.state is not None or args.workspace not in (None, "all"):
        print(
            "error: accounts lists every account; omit --state and --workspace",
            file=sys.stderr,
        )
        return 2
    report = accounts_report()
    if args.accounts_json:
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0
    print("ACCOUNT | CONFIGURED | SERVICE | MONITOR | SESSIONS | ENROLLED | RUNNING | BLOCKED")
    for row in report["accounts"]:
        monitor, service = row["monitor"], row["service"]
        configured = {True: "yes", False: "no"}.get(row["configured"], "unknown")
        unit = clean(service["active"])
        if service["error"]:
            unit += f" ({clean(service['error'])})"
        if monitor["ok"]:
            state = f"ok source {stamp(monitor['generated_at'], True)}" + (
                " STALE" if monitor["stale"] else ""
            )
        else:
            state = f"unavailable ({clean(monitor['error'])})"
        counts = [
            "unknown" if monitor[key] is None else monitor[key]
            for key in ("sessions", "enrolled", "running", "blocked")
        ]
        print(clean(row["workspace"]), configured, unit, state, *counts, sep=" | ")
    return 0


def run(args):
    from .chatgpt_tui import clean

    interactive = (
        args.guardian_action == "tui"
        or args.tui
        or args.follow
        or (sys.stdin.isatty() and sys.stdout.isatty() and not args.monitor_json)
    )
    if interactive and args.monitor_json:
        print("error: --json cannot be combined with interactive TUI", file=sys.stderr)
        return 2
    workspace = args.workspace or ("all" if interactive and args.state is None else "home")
    if workspace == "all" and args.state is not None:
        print(
            "error: --state selects one Guardian database; it cannot be combined with "
            "--workspace all",
            file=sys.stderr,
        )
        return 2
    if workspace == "all":
        fetch = fetch_combined
    else:

        def fetch():
            return fetch_snapshot(workspace, args.state)

    if interactive:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "error: TUI requires a terminal; use monitor or monitor --json for pipes",
                file=sys.stderr,
            )
            return 2
        from .chatgpt_tui import run_action, run_tui

        # A custom --state would make the actions (which always use Guardian's default) hit
        # a different database than the one displayed, so that view stays read-only.
        return run_tui(
            fetch, interval=args.interval, act=None if args.state is not None else run_action
        )
    try:
        data = fetch()
        if args.monitor_json:
            print(json.dumps(data, ensure_ascii=False, allow_nan=False))
        elif workspace == "all":
            print_combined(data)
        else:
            print_snapshot(data)
        if workspace == "all" and not any(a["ok"] for a in data["accounts"]):
            return 1
        return 0
    except (MonitorError, ValueError) as exc:
        print("error: " + clean(str(exc)), file=sys.stderr)
        return getattr(exc, "code", 1)
