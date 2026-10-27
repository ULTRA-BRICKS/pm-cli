"""Command-line entry points for the control plane: ``api``, ``mcp``, ``approvals``, ``connector``.

Accessed via: ``pmlib/commands/chatgpt.py`` (thin routing; every import here is lazy so the
read-only monitor commands stay stdlib-light).

``pm chatgpt api METHOD --params JSON`` is the frozen JSON API client: always exactly one JSON
envelope on stdout, exit 0 for ``ok`` and 1 otherwise, bounded input via argv or stdin, no shell
strings. It works without any service: it opens pm's own ledger directly. ``api serve`` and
``api token-init`` manage the optional loopback HTTP service.
"""

from __future__ import annotations

import importlib
import json
import sys
from importlib import resources

from . import api
from .errors import ApiError
from .service import Services

MAX_PARAMS_BYTES = 64 * 1024
UNITS = {
    "api": "pm-chatgpt-api.service",
    "worker": "pm-chatgpt-approvals.service",
    "audit-service": "pm-chatgpt-audit.service",
    "audit-timer": "pm-chatgpt-audit.timer",
}


def _print_envelope(envelope: dict, out=None) -> int:
    print(json.dumps(envelope, separators=(",", ":")), file=out or sys.stdout)
    return 0 if envelope["ok"] else 1


def _read_params(raw: str | None, stdin) -> object:
    if raw is None:
        return {}
    if raw == "-":
        text = stdin.read(MAX_PARAMS_BYTES + 1)
    else:
        text = raw
    if len(text.encode("utf-8", "replace")) > MAX_PARAMS_BYTES:
        raise ApiError("payload_too_large", f"--params exceeds {MAX_PARAMS_BYTES} bytes")
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        raise ApiError("invalid_request", "--params is not valid JSON") from None


def run_api(args, services: Services | None = None, stdin=None) -> int:
    method = args.api_method
    if method == "serve":
        return _serve(args, services)
    if method == "token-init":
        return _token_init(args, services)
    services = services or Services()
    try:
        params = _read_params(args.params, stdin or sys.stdin)
    except ApiError as exc:
        return _print_envelope(api.failure(exc.code, exc.message))
    return _print_envelope(api.dispatch(services, method, params, source="cli"))


def _token_init(args, services: Services | None) -> int:
    from . import http_api

    services = services or Services()
    try:
        path = http_api.init_token(services.state_dir, rotate=args.rotate)
    except (http_api.TokenError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"token file: {path} (mode 0600; the token is never printed)")
    return 0


def _serve(args, services: Services | None) -> int:
    from . import http_api

    services = services or Services()
    port = args.port
    if not 1 <= port <= 65535:
        print("error: --port must be 1-65535", file=sys.stderr)
        return 2
    try:
        token = http_api.load_token(services.state_dir)
        server = http_api.make_server(services, port, token)
    except (http_api.TokenError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"listening on http://127.0.0.1:{port}/v1/rpc (loopback only, bearer token)",
        file=sys.stderr,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run_mcp(args, services: Services | None = None, stdin=None, stdout=None) -> int:
    from . import mcp

    return mcp.serve(services or Services(), stdin or sys.stdin, stdout or sys.stdout)


def run_approvals(args, services: Services | None = None) -> int:
    from . import runner

    try:
        interval = runner.check_interval(args.interval)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    services = services or Services()
    worker = runner.build_worker(services)
    return runner.run_loop(
        services, worker, once=args.once, interval=interval, propose=not args.no_propose
    )


def run_connector(args, services: Services | None = None) -> int:
    from . import connector

    services = services or Services()
    if args.connector_action == "tunnel-id":
        try:
            connector.set_tunnel_id(services.state_dir, args.tunnel_id)
        except connector.ConnectorError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("tunnel id recorded (non-secret); state: awaiting_platform_connection")
        return 0
    return _print_envelope(api.dispatch(services, "connectors.list", {}, source="cli"))


def run_unit(args) -> int:
    text = resources.files(__package__).joinpath("units", UNITS[args.which]).read_text("utf-8")
    sys.stdout.write(text)
    return 0


# -- ``pm chatgpt audit``: semantic task-completion audit of ChatGPT conversations ---------------

EXIT_BUSY = 75
BUSY_DOCUMENT = {"schema": 1, "status": "busy", "reason": "lock-busy"}


def audit_accounts(args) -> tuple[str, ...]:
    """The accounts to audit: the audit's own ``--workspace``, else the group's, else all."""
    from ..chatgpt_monitor import ACCOUNTS

    choice = args.audit_workspace or args.workspace or "all"
    return tuple(ACCOUNTS) if choice in ("all", "global") else (choice,)


def _site_packages(args) -> str | None:
    name = (
        "bind_playwright_site_packages"
        if args.audit_action == "bind"
        else "playwright_site_packages"
    )
    return getattr(args, name, None)


def default_environment(args):
    """Real wiring: pm's audit directory, the Laya socket classifier and the CDP browser reader."""
    from . import audit_classifier, audit_store
    from .audit_bindings import AccountBindings
    from .audit_collector import Environment
    from .audit_guard import NotBeforeStore

    directory = audit_store.default_dir()
    site_packages = _site_packages(args)

    def reader_factory(account: str):
        # Imported only here so help/doctor/--json never load Playwright's wrapper.
        browser = importlib.import_module(f"{__package__}.audit_browser")
        return browser.make_reader(account, playwright_site_packages=site_packages)

    classifier = audit_classifier.LayaClassifier()
    return Environment(
        reader_factory=reader_factory,
        classifier=classifier,
        classifier_model=classifier.profile,
        store=audit_store.AuditStore(directory),
        not_before=NotBeforeStore(directory / "not_before.json"),
        bindings=AccountBindings(directory / "bindings.json"),
        lock_path=directory / "run.lock",
    )


def _notify(report: dict, env, notify) -> dict:
    """Notify after the audit; whatever happens the report and exit code are unaffected."""
    try:
        if notify is None:
            notify = importlib.import_module(f"{__package__}.audit_notify").notify_report
        result = notify(report, env.store.last_report(), env.store, now=env.clock())
        if not isinstance(result, dict):
            raise TypeError("notify result must be an object")
        json.dumps(result, allow_nan=False)
    except Exception:  # noqa: BLE001 - a notifier failure must never lose the report
        return {"outcome": "error"}
    return result


def run_audit(args, env=None, *, notify=None) -> int:
    """Run one audit; stdout is one bounded JSON document (``--json``) or a compact summary."""
    from . import audit_report
    from .audit_collector import AuditRequest
    from .audit_collector import run_audit as collect
    from .audit_files import UnsafePath
    from .audit_guard import LockBusy, LockUnsafe

    env = env or default_environment(args)
    request = AuditRequest(
        audit_accounts(args), days=args.days, turns=args.turns, include_text=args.include_text
    )
    try:
        report = collect(request, env)
    except LockBusy:
        print(json.dumps(BUSY_DOCUMENT, separators=(",", ":")))
        return EXIT_BUSY
    except LockUnsafe as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    if args.notify:
        report["notify"] = _notify(report, env, notify)
    try:
        env.store.put_report(report)
    except (OSError, ValueError, UnsafePath) as exc:
        print(f"error: cannot store the audit report: {exc}", file=sys.stderr)
    if args.audit_json:
        print(audit_report.to_json(report))
    else:
        print(audit_report.render_text(report))
        if "notify" in report:
            print(f"notify: {report['notify'].get('outcome', 'unknown')}")
    return audit_report.exit_code(report)


def run_audit_bind(args, env=None) -> int:
    """Record which Guardian account the owned browser session is (an explicit owner action)."""
    from .audit_bindings import BindingError
    from .audit_collector import reason_slug
    from .audit_guard import (
        AccountStopped,
        Budget,
        BudgetExhausted,
        GuardedReader,
        LockBusy,
        LockUnsafe,
        Pacer,
        RunLock,
    )
    from .audit_reader import ReaderError, ReaderUnavailable

    env = env or default_environment(args)
    account = args.bind_workspace
    try:
        with RunLock(env.lock_path):
            pending = env.not_before.active(account, env.clock())
            if pending is not None:
                print(
                    f"error: {account} is paused until {pending:.0f} (not-before)", file=sys.stderr
                )
                return 4
            try:
                reader = env.reader_factory(account)
            except ReaderUnavailable as exc:
                print(f"error: {reason_slug(exc.reason)}", file=sys.stderr)
                return 4
            guard = GuardedReader(
                reader,
                account=account,
                pacer=Pacer(env.clock, env.sleep, env.limits.min_gap_seconds),
                budget=Budget(env.clock, env.limits.budget_seconds),
                store=env.not_before,
                clock=env.clock,
            )
            try:
                identity = guard.identity()
            except ReaderUnavailable as exc:
                print(f"error: {reason_slug(exc.reason)}", file=sys.stderr)
                return 4
            except (AccountStopped, BudgetExhausted, ReaderError) as exc:
                print(f"error: cannot read the browser identity: {exc}", file=sys.stderr)
                return 4
            finally:
                guard.close()
            try:
                env.bindings.record(account, identity)
            except BindingError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    except LockBusy:
        print(json.dumps(BUSY_DOCUMENT, separators=(",", ":")))
        return EXIT_BUSY
    except LockUnsafe as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    print(f"bound {account} to this browser session (identity {identity.account_hash[:8]})")
    return 0


def run_audit_bench(args) -> int:
    bench = importlib.import_module(f"{__package__}.audit_bench")
    kwargs = {} if args.bench_budget is None else {"budget_seconds": args.bench_budget}
    return bench.run(record=args.bench_record, as_json=args.bench_json, **kwargs)


def run_audit_command(args) -> int:
    action = args.audit_action
    if action == "bench":
        return run_audit_bench(args)
    if action == "bind":
        return run_audit_bind(args)
    return run_audit(args)
