"""Command-line entry points for the control plane: ``api``, ``mcp``, ``approvals``, ``connector``.

Accessed via: ``pmlib/commands/chatgpt.py`` (thin routing; every import here is lazy so the
read-only monitor commands stay stdlib-light).

``pm chatgpt api METHOD --params JSON`` is the frozen JSON API client: always exactly one JSON
envelope on stdout, exit 0 for ``ok`` and 1 otherwise, bounded input via argv or stdin, no shell
strings. It works without any service: it opens pm's own ledger directly. ``api serve`` and
``api token-init`` manage the optional loopback HTTP service.
"""

from __future__ import annotations

import json
import sys
from importlib import resources

from . import api
from .errors import ApiError
from .service import Services

MAX_PARAMS_BYTES = 64 * 1024
UNITS = {"api": "pm-chatgpt-api.service", "worker": "pm-chatgpt-approvals.service"}


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
