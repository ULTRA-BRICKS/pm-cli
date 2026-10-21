"""Loopback, authenticated HTTP front end: ``POST /v1/rpc`` with the same ``{method, params}``.

Accessed via: ``pm chatgpt api serve --port 8792`` and ``pm chatgpt api token-init``.

Invariants: the server binds only a literal loopback address and every request, whatever its
method or path, is authenticated with a per-user bearer token BEFORE anything else is read; the
token is compared in constant time and lives only in a private ``0600`` file that is never
printed, logged or echoed. Requests carrying an ``Origin`` header or a non-loopback ``Host`` are
refused (browser and DNS-rebinding abuse). Bodies are capped (``MAX_BODY_BYTES``), framing is
strict (``Content-Length`` JSON only), socket I/O has a deadline, and concurrent connections are
capped. Writes go through ``api.dispatch`` and can only QUEUE an owner approval; this module has
no path to Telegram, Guardian or any control. The service is optional: the CLI works without it.
"""

from __future__ import annotations

import contextlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api
from .ledger import ensure_private_dir
from .service import Services

TOKEN_FILE = "api-token"
MIN_TOKEN_CHARS = 32
MAX_TOKEN_FILE_BYTES = 256
MAX_BODY_BYTES = 64 * 1024
DEFAULT_MAX_CONNECTIONS = 8
DEFAULT_IO_TIMEOUT = 10.0
DEFAULT_PORT = 8792
_TOKEN_CHARS = re.compile(r"[A-Za-z0-9_-]+")


class TokenError(RuntimeError):
    """The token file is missing, unsafe or malformed; the message never contains the token."""


def token_path(state_dir: Path) -> Path:
    return Path(state_dir) / TOKEN_FILE


def init_token(state_dir: Path, *, rotate: bool = False) -> Path:
    """Create the private token file; refuses to replace an existing one unless ``rotate``."""
    state_dir = Path(state_dir)
    ensure_private_dir(state_dir)
    target = token_path(state_dir)
    temp = state_dir / f".{TOKEN_FILE}.{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if rotate:
            os.replace(temp, target)
        else:
            try:
                os.link(temp, target)
            except FileExistsError:
                raise TokenError(
                    "a token already exists; use --rotate to replace it (clients must be updated)"
                ) from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp)
    return target


def load_token(state_dir: Path) -> str:
    path = token_path(state_dir)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise TokenError("no API token yet; run: pm chatgpt api token-init") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise TokenError("the token file must be a regular file owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise TokenError("the token file must not be accessible to other users (chmod 600)")
    if info.st_size > MAX_TOKEN_FILE_BYTES:
        raise TokenError("the token file is malformed")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="ascii", errors="replace") as handle:
        token = handle.read().strip()
    if len(token) < MIN_TOKEN_CHARS or _TOKEN_CHARS.fullmatch(token) is None:
        raise TokenError("the token file is malformed; rotate it with --rotate")
    return token


def _failure_bytes(code: str, message: str) -> bytes:
    return json.dumps(api.failure(code, message), separators=(",", ":")).encode()


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.0"  # one request per connection: no keep-alive state to abuse
    server_version = "pm-chatgpt"
    sys_version = ""

    def setup(self) -> None:
        self.timeout = self.server.io_timeout
        super().setup()

    def log_message(self, format: str, *args) -> None:
        return  # no access log: request lines and headers can carry credentials

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _fail(self, status: int, code: str, message: str) -> None:
        self._send(status, _failure_bytes(code, message))

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        provided = header[7:] if header.startswith("Bearer ") else ""
        return hmac.compare_digest(provided.encode("utf-8", "replace"), self.server.token_bytes)

    def _origin_ok(self) -> bool:
        if self.headers.get("Origin") is not None:
            return False
        return (self.headers.get("Host") or "") in self.server.allowed_hosts

    def _handle(self) -> None:
        if not self._authorized():
            self._fail(401, "unauthorized", "missing or invalid bearer token")
            return
        if not self._origin_ok():
            self._fail(403, "invalid_request", "origin or host is not allowed")
            return
        path = self.path
        if path == "/v1/health":
            if self.command != "GET":
                self._fail(405, "invalid_request", "method not allowed")
                return
            self._send(200, json.dumps(api_ok({"status": "ok"}), separators=(",", ":")).encode())
            return
        if path != "/v1/rpc":
            self._fail(404, "not_found", "no such route")
            return
        if self.command != "POST":
            self._fail(405, "invalid_request", "method not allowed")
            return
        self._rpc()

    def _rpc(self) -> None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._fail(415, "invalid_request", "Content-Type must be application/json")
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._fail(411, "invalid_request", "Content-Length framing is required")
            return
        raw_length = self.headers.get("Content-Length") or ""
        if not raw_length.isascii() or not raw_length.isdigit() or len(raw_length) > 9:
            self._fail(411, "invalid_request", "a valid Content-Length is required")
            return
        length = int(raw_length)
        if length > MAX_BODY_BYTES:
            self.rfile.read(min(length, 4 * MAX_BODY_BYTES))  # drain so the client sees the 413
            self._fail(413, "payload_too_large", f"body exceeds {MAX_BODY_BYTES} bytes")
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._fail(400, "invalid_request", "truncated body")
            return
        try:
            obj = json.loads(body)
        except (ValueError, RecursionError):
            self._fail(400, "invalid_request", "body is not valid JSON")
            return
        envelope = api.dispatch_envelope(self.server.services, obj, source="http")
        code = None if envelope["ok"] else envelope["error"]["code"]
        status = 400 if code == "invalid_request" else 500 if code == "internal_error" else 200
        self._send(status, json.dumps(envelope, separators=(",", ":")).encode())

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


def api_ok(result: dict) -> dict:
    return {"ok": True, "result": result}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(
        self,
        address: tuple[str, int],
        *,
        services: Services,
        token: str,
        max_connections: int,
        io_timeout: float,
        family: int,
    ) -> None:
        self.address_family = family
        self.services = services
        self.token_bytes = token.encode("ascii")
        self.io_timeout = io_timeout
        self._slots = threading.BoundedSemaphore(max_connections)
        self.allowed_hosts: frozenset[str] = frozenset()
        super().__init__(address, _Handler)
        port = self.server_address[1]
        host = address[0]
        shown = f"[{host}]" if ":" in host else host
        names = {f"{shown}:{port}", f"localhost:{port}"}
        self.allowed_hosts = frozenset(names)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            with contextlib.suppress(OSError):
                request.settimeout(2)
                body = _failure_bytes("busy", "too many concurrent connections")
                request.sendall(
                    b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                    b"Connection: close\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body
                )
            self.shutdown_request(request)
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address) -> None:
        try:
            with contextlib.suppress(Exception):  # a broken client must not take the server down
                self.finish_request(request, client_address)
        finally:
            self._slots.release()
            self.shutdown_request(request)


def make_server(
    services: Services,
    port: int,
    token: str,
    *,
    host: str = "127.0.0.1",
    max_connections: int = DEFAULT_MAX_CONNECTIONS,
    io_timeout: float = DEFAULT_IO_TIMEOUT,
) -> ThreadingHTTPServer:
    """Build (not start) the server. Only a literal loopback address is accepted."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("host must be a literal loopback address (127.0.0.1 or ::1)") from None
    if not ip.is_loopback:
        raise ValueError("host must be a literal loopback address (127.0.0.1 or ::1)")
    if not isinstance(token, str) or len(token) < MIN_TOKEN_CHARS or not token.isascii():
        raise ValueError("API token is too short")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be 0-65535")
    family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
    return _Server(
        (host, port),
        services=services,
        token=token,
        max_connections=max_connections,
        io_timeout=io_timeout,
        family=family,
    )
