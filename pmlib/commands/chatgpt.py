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
        "'all' is accepted only by monitor, tui and accounts.",
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
    if args.guardian_action in ("monitor", "tui"):
        from ..chatgpt_monitor import run as run_monitor

        return run_monitor(args)
    if args.guardian_action == "accounts":
        from ..chatgpt_monitor import run_accounts

        return run_accounts(args)
    if args.workspace == "all":
        print(
            "error: --workspace all is only for monitor, tui and accounts; "
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
