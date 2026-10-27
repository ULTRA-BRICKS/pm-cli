"""Self-registered Guardian control client reached through ``pm chatgpt``.

Only the installed CLI owns Guardian state and browser lifecycle. Startup uses stdlib;
all durable controls require explicit opt-in, and no arbitrary flags are forwarded.
Chat identities are validated before delegation so literal arguments cannot become options.
Service controls address only fixed systemd user units; recovery policy stays in Guardian.
Nonblocking requests and bounded observation must never force-kill a client or unit.
Non-follow journal pipes belong to pm so a stalled reader cannot hold caller output open.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys

from ..chatgpt_monitor import ACCOUNTS, CHAT_URL, SERVICE_UNITS
from ..chatgpt_monitor import configure as configure_monitor

NAME = "chatgpt"
HELP = "Observe local ChatGPT Guardian accounts; controls require explicit --apply."


def _chat_url(value: str) -> str:
    """Match Guardian's permanent URL contract without importing its package."""
    if CHAT_URL.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "Use an exact permanent https://chatgpt.com/c/<id> URL "
            "(ASCII letters, digits, '_' or '-', optional trailing '/', no query or fragment)."
        )
    return value


def _chat_target(value: str) -> str:
    return value if value == "*" else _chat_url(value)


def _log_lines(value: str) -> int:
    try:
        lines = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("--lines must be an integer from 1 to 500.") from None
    if not 1 <= lines <= 500:
        raise argparse.ArgumentTypeError("--lines must be an integer from 1 to 500.")
    return lines


def configure(parser: argparse.ArgumentParser) -> None:
    parser.allow_abbrev = False
    parser.add_argument(
        "--workspace",
        choices=(*ACCOUNTS, "all"),
        default=None,
        help="Guardian account scope (default: home; the interactive TUI defaults to all). "
        "'all' is accepted only by monitor, tui, accounts and audit.",
    )
    parser.add_argument("--state", help="Shared Guardian database (otherwise Guardian's default).")
    subs = parser.add_subparsers(dest="guardian_action")
    accounts = subs.add_parser(
        "accounts",
        help="List supported accounts, whether configured, and observed counts.",
        allow_abbrev=False,
    )
    accounts.add_argument(
        "--json", dest="accounts_json", action="store_true", help="One read-only JSON report"
    )
    for name in ("monitor", "tui"):
        monitor = subs.add_parser(
            name, help="Session progress monitor (TUI has confirmed actions)", allow_abbrev=False
        )
        configure_monitor(monitor)
    subs.add_parser("status", help="Observe status; no recovery sends.", allow_abbrev=False)
    _configure_control(subs)
    _configure_audit(subs)
    logs = subs.add_parser(
        "logs",
        help="Read the fixed workspace user-service journal.",
        allow_abbrev=False,
    )
    logs.add_argument(
        "--lines",
        type=_log_lines,
        default=50,
        help="Initial journal entries, 1 to 500 (default: 50).",
    )
    logs.add_argument("--follow", action="store_true", help="Stream entries until interrupted.")
    service = subs.add_parser(
        "service",
        help="Observe or control the installed systemd user service.",
        description="Default: read-only status. Service changes require --apply; "
        "installation and recovery policy remain owned by Guardian.",
        allow_abbrev=False,
    )
    service_subs = service.add_subparsers(dest="service_action")
    service_subs.add_parser("status", help="Observe service state (default).", allow_abbrev=False)
    for action in ("start", "stop", "restart"):
        cp = service_subs.add_parser(
            action,
            help="Request a service change with --apply.",
            allow_abbrev=False,
        )
        cp.add_argument(
            "--apply",
            action="store_true",
            required=True,
            help="Authorize this systemd user service lifecycle change.",
        )
    for action in ("watch", "unwatch", "sol-opt-in", "sol-opt-out", "stop", "resume"):
        cp = subs.add_parser(
            action, help="Change a durable control with --apply.", allow_abbrev=False
        )
        if action in ("stop", "resume"):
            cp.add_argument(
                "--chat",
                required=True,
                type=_chat_target,
                help="Exact permanent https://chatgpt.com/c/<id> URL, or '*' for all.",
            )
        else:
            cp.add_argument(
                "chat",
                type=_chat_url,
                help="Exact permanent https://chatgpt.com/c/<id> URL; no query or fragment.",
            )
        cp.add_argument(
            "--apply",
            action="store_true",
            required=True,
            help="Authorize this control change; does not enable Guardian recovery sends.",
        )


def _configure_control(subs) -> None:
    """Owner-approval control plane: JSON API, HTTP service, MCP server, worker, connector."""
    api = subs.add_parser(
        "api",
        help="Frozen JSON API: METHOD --params JSON (or 'serve', 'token-init').",
        description="One JSON envelope on stdout. Methods: accounts.list sessions.list "
        "sessions.get actions.request requests.list requests.get connectors.list "
        "connectors.request. Writes only QUEUE an owner approval. 'serve' runs the optional "
        "loopback authenticated HTTP service; 'token-init' creates its private token file.",
        allow_abbrev=False,
    )
    api.add_argument("api_method", metavar="METHOD", help="API method, 'serve' or 'token-init'.")
    api.add_argument(
        "--params", default=None, help="JSON object (at most 64 KiB), or '-' to read stdin."
    )
    api.add_argument("--port", type=int, default=8792, help="serve: loopback port (8792).")
    api.add_argument("--rotate", action="store_true", help="token-init: replace the token.")
    subs.add_parser(
        "mcp",
        help="Stdio MCP server: read tools plus request-only approval tools.",
        allow_abbrev=False,
    )
    approvals = subs.add_parser("approvals", help="Owner-approval worker.", allow_abbrev=False)
    approvals_subs = approvals.add_subparsers(dest="approvals_action", required=True)
    run_cmd = approvals_subs.add_parser(
        "run",
        help="Propose, ask the owner in Telegram, and execute approved requests.",
        allow_abbrev=False,
    )
    run_cmd.add_argument("--once", action="store_true", help="One tick, then exit.")
    run_cmd.add_argument("--interval", type=int, default=60, help="Seconds per tick (30-3600).")
    run_cmd.add_argument(
        "--no-propose",
        action="store_true",
        help="Process queued requests only; do not propose new continuations.",
    )
    connector = subs.add_parser(
        "connector", help="Fixed guardian/chatgpt connector state.", allow_abbrev=False
    )
    connector_subs = connector.add_subparsers(dest="connector_action")
    connector_subs.add_parser("status", help="Show state and prepared setup steps (default).")
    tunnel = connector_subs.add_parser("tunnel-id", help="Record the non-secret tunnel id.")
    tunnel.add_argument("tunnel_id", help="tunnel_<letters and digits>; never a secret.")
    unit = subs.add_parser(
        "unit", help="Print a systemd user unit template (not installed).", allow_abbrev=False
    )
    unit.add_argument("which", choices=("api", "worker", "audit-service", "audit-timer"))


def _days(value: str) -> float:
    try:
        days = float(value)
    except ValueError:
        days = math.nan
    if not math.isfinite(days) or not 0 < days <= 60:
        raise argparse.ArgumentTypeError("--days must be a number above 0 and at most 60.")
    return days


def _turns(value: str) -> int:
    try:
        turns = int(value)
    except ValueError:
        turns = 0
    if not 1 <= turns <= 50:
        raise argparse.ArgumentTypeError("--turns must be an integer from 1 to 50.")
    return turns


def _configure_audit(subs) -> None:
    """Semantic task-completion audit; reads only, never writes to ChatGPT."""
    audit = subs.add_parser(
        "audit",
        help="Audit whether ChatGPT conversations finished their task (read-only).",
        description="Read recent conversations through the owned, already-logged-in browser and "
        "classify each as done, incomplete, blocked, working or unknown. Reads only: nothing is "
        "sent, resumed, unarchived or opened. Exit 0 = every account fully audited, 3 = partial, "
        "4 = an account unavailable, 75 = another audit is running, 2 = usage error.",
        allow_abbrev=False,
    )
    audit.add_argument(
        "--workspace",
        dest="audit_workspace",
        choices=(*ACCOUNTS, "all", "global"),
        default=None,
        help="Account to audit (default: all; 'global' is an alias of 'all').",
    )
    audit.add_argument(
        "--days", type=_days, default=14.0, help="Window in days, above 0 and at most 60 (14)."
    )
    audit.add_argument(
        "--turns",
        type=_turns,
        default=4,
        help="Turns requested per chat read, 1-50 (4); a bare 'go' is re-read with 8, then 16.",
    )
    audit.add_argument(
        "--json", dest="audit_json", action="store_true", help="One bounded JSON report."
    )
    audit.add_argument(
        "--notify", action="store_true", help="Send a deduplicated Telegram summary afterwards."
    )
    audit.add_argument(
        "--include-text",
        action="store_true",
        help="Add each chat's short remaining-steps list (never the chat text itself).",
    )
    audit.add_argument(
        "--playwright-site-packages",
        metavar="DIR",
        default=None,
        help="Directory providing the playwright package when it is not importable.",
    )
    actions = audit.add_subparsers(dest="audit_action")
    bench = actions.add_parser(
        "bench",
        help="Run the classifier validation benchmark.",
        allow_abbrev=False,
    )
    bench.add_argument(
        "--record", dest="bench_record", action="store_true", help="Persist a passing result."
    )
    bench.add_argument("--json", dest="bench_json", action="store_true", help="JSON result.")
    bind = actions.add_parser(
        "bind",
        help="Record which account the owned browser session is logged into.",
        allow_abbrev=False,
    )
    bind.add_argument("--workspace", dest="bind_workspace", choices=ACCOUNTS, required=True)
    bind.add_argument(
        "--playwright-site-packages",
        dest="bind_playwright_site_packages",
        metavar="DIR",
        default=None,
        help="Directory providing the playwright package when it is not importable.",
    )


_CONTROL = {"api", "mcp", "approvals", "connector", "unit", "audit"}


def _run_control(args: argparse.Namespace) -> int:
    from ..chatgpt_control import cli

    action = args.guardian_action
    if action == "api":
        return cli.run_api(args)
    if action == "mcp":
        return cli.run_mcp(args)
    if action == "approvals":
        return cli.run_approvals(args)
    if action == "connector":
        args.connector_action = args.connector_action or "status"
        return cli.run_connector(args)
    if action == "audit":
        return cli.run_audit_command(args)
    return cli.run_unit(args)


def _systemctl(command: list[str]) -> tuple[int, str]:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        output, _ = process.communicate(timeout=10)
    except FileNotFoundError:
        print(
            "error: systemctl is unavailable; a systemd user manager is required.", file=sys.stderr
        )
        return 127, ""
    except subprocess.TimeoutExpired:
        print(
            "error: systemctl timed out; the request may still be pending. "
            "No client or service was killed; inspect service status before retrying.",
            file=sys.stderr,
        )
        return 124, ""
    except OSError:
        print(
            "error: cannot execute systemctl; check its installation and permissions.",
            file=sys.stderr,
        )
        return 126, ""
    code = process.returncode
    if code:
        code = code if code >= 0 else 128 - code
        print(
            f"error: systemctl failed (exit {code}); inspect the user manager and "
            "whether the Guardian unit is installed. Service state is unconfirmed.",
            file=sys.stderr,
        )
    return code, output


def _service(args: argparse.Namespace) -> int:
    if args.state is not None:
        print(
            "error: --state does not configure an installed service; omit it for service controls.",
            file=sys.stderr,
        )
        return 2
    unit = SERVICE_UNITS[args.workspace]
    action = args.service_action or "status"
    if action != "status":
        code, _ = _systemctl(["systemctl", "--user", "--no-block", action, unit])
        if code:
            return code
        print(f"{unit}: {action} request accepted; observing service state.")
    code, output = _systemctl(
        [
            "systemctl",
            "--user",
            "--no-pager",
            "show",
            unit,
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=Job",
        ]
    )
    if code:
        return code
    properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    load = properties.get("LoadState")
    if load == "not-found":
        print(
            f"error: {unit} is not installed; deployment must install it separately.",
            file=sys.stderr,
        )
        return 4
    active, sub = properties.get("ActiveState"), properties.get("SubState")
    job = properties.get("Job", "missing").split(" ", 1)[0]
    if (
        load not in {"loaded", "masked", "error", "bad-setting", "merged", "stub"}
        or active
        not in {
            "active",
            "reloading",
            "inactive",
            "failed",
            "activating",
            "deactivating",
            "maintenance",
            "refreshing",
        }
        or sub
        not in {
            "running",
            "dead",
            "exited",
            "failed",
            "start",
            "start-pre",
            "start-post",
            "auto-restart",
            "auto-restart-queued",
            "stop",
            "stop-sigterm",
            "stop-sigkill",
            "stop-post",
            "final-sigterm",
            "final-sigkill",
            "reload",
            "reload-signal",
            "reload-notify",
            "condition",
            "clean",
            "cleaning",
            "abandoned",
            "stop-watchdog",
            "final-watchdog",
            "dead-before-auto-restart",
            "failed-before-auto-restart",
            "dead-resources-pinned",
        }
        or (job and not job.isascii())
        or (job and not job.isdigit())
    ):
        print(
            f"error: {unit}: unrecognized systemctl state; service state is unconfirmed.",
            file=sys.stderr,
        )
        return 1
    if load != "loaded" or active == "failed":
        print(f"{unit}: {load}, {active}/{sub}; service failed or unavailable.")
        return 1
    pending = bool(job and int(job)) or active in {
        "activating",
        "deactivating",
        "reloading",
        "refreshing",
        "maintenance",
    }
    if action != "status":
        target = "inactive" if action == "stop" else "active"
        pending = pending or active != target
    print(f"{unit}: {load}, {active}/{sub}" + ("; pending." if pending else "; observed."))
    return 3 if pending else 0


def _logs(args: argparse.Namespace) -> int:
    if args.state is not None:
        print(
            "error: --state does not select a service journal; omit it for logs.", file=sys.stderr
        )
        return 2
    command = [
        "journalctl",
        "--user",
        "--unit",
        SERVICE_UNITS[args.workspace],
        "--lines",
        str(args.lines),
        "--no-pager",
    ]
    if args.follow:
        command.append("--follow")
    try:
        process = (
            subprocess.Popen(command)
            if args.follow
            else subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    except FileNotFoundError:
        print("error: journalctl is unavailable; install systemd journal tools.", file=sys.stderr)
        return 127
    except OSError:
        print(
            "error: cannot execute journalctl; check its installation and permissions.",
            file=sys.stderr,
        )
        return 126
    try:
        if args.follow:
            code = process.wait(timeout=None)
        else:
            output, errors = process.communicate(timeout=10)
            if output:
                sys.stdout.buffer.write(output)
                sys.stdout.buffer.flush()
            if errors:
                sys.stderr.buffer.write(errors)
                sys.stderr.buffer.flush()
            code = process.returncode
    except subprocess.TimeoutExpired:
        try:
            process.terminate()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
            pass
        print(
            "error: journalctl timed out; no client or service was force-killed.", file=sys.stderr
        )
        return 124
    except KeyboardInterrupt:
        # Only the journal reader belongs to this invocation; the service keeps running.
        try:
            process.terminate()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt):
            pass
        return 130
    finally:
        if not args.follow:
            process.stdout.close()
            process.stderr.close()
    code = code if code >= 0 else 128 - code
    if code:
        print(
            f"error: journalctl failed (exit {code}); check user journal access.", file=sys.stderr
        )
    return code


def run(args: argparse.Namespace) -> int:
    if args.guardian_action in _CONTROL:
        return _run_control(args)
    if args.guardian_action in ("monitor", "tui"):
        from ..chatgpt_monitor import run as run_monitor

        return run_monitor(args)
    if args.guardian_action == "accounts":
        from ..chatgpt_monitor import run_accounts

        return run_accounts(args)
    if args.workspace == "all":
        print(
            "error: --workspace all is only for monitor, tui, accounts and audit; "
            "choose home or glide.vc for this command.",
            file=sys.stderr,
        )
        return 2
    args.workspace = args.workspace or "home"
    if args.guardian_action == "logs":
        return _logs(args)
    if args.guardian_action == "service":
        return _service(args)
    action = args.guardian_action or "status"
    command = ["chatgpt-guardian", "--workspace", args.workspace]
    if args.state is not None:
        command.append(f"--state={args.state}")
    command.append(action)
    if action in ("stop", "resume"):
        command.extend(["--chat", args.chat])
    elif action != "status":
        command.append(args.chat)
    try:
        result = subprocess.run(command, check=False, timeout=30)
    except FileNotFoundError:
        print(
            "error: chatgpt-guardian is unavailable; install the reviewed agenttools-chatgpt "
            "package in your CLI environment and ensure chatgpt-guardian is on PATH.",
            file=sys.stderr,
        )
        return 127
    except subprocess.TimeoutExpired:
        print(
            "error: Guardian control timed out; inspect its status before retrying a change.",
            file=sys.stderr,
        )
        return 124
    except OSError:
        print("error: cannot execute chatgpt-guardian; check its installation.", file=sys.stderr)
        return 126
    return result.returncode if result.returncode >= 0 else 128 - result.returncode
