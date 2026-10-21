"""The fixed, reviewed ``guardian`` connector for target ``chatgpt``: plan, local install, state.

Accessed via: ``service.submit_connector`` (plan digest), ``worker`` (install after a verified
owner approval), ``api`` (``connectors.list``) and ``pm chatgpt connector`` (status, tunnel id).

Scope: a local stdio MCP launcher plus a manifest and an onboarding note, written under
``<pm state>/chatgpt/connectors/guardian-chatgpt/``. There are no downloads, no network listener
and no credentials in any file. The content is a compile-time constant, so the plan digest the
owner approves is exactly what gets installed and what ``verify`` later checks.

Invariants: every path component below the state root is opened with ``O_NOFOLLOW`` relative to a
directory fd, so a planted symlink cannot redirect a write; files are written to a temp name and
renamed; directories are 0700, the launcher 0700, everything else 0600; an existing target that is
not a regular file is refused, never replaced silently. ``state`` is reported from what is on disk
and in the ledger and can never be ``connected``: ChatGPT workspace registration, OAuth and the
platform risk warning belong to the owner, and only a real ChatGPT tool call could verify them.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path

from .ledger import LedgerError, ensure_private_dir
from .projection import ACTIVE

CONNECTOR = "guardian"
TARGET = "chatgpt"
PLAN_VERSION = 1
SUBDIRS = ("connectors", "guardian-chatgpt")
LAUNCHER = "launch-mcp.sh"
MANIFEST = "manifest.json"
ONBOARDING = "ONBOARDING.md"
TUNNEL_FILE = "tunnel.json"
TUNNEL_ID = re.compile(r"tunnel_[A-Za-z0-9]{8,64}")
PROFILE = "pm-chatgpt"
TUNNEL_SETTINGS = "https://platform.openai.com/settings/organization/tunnels"
DOCS = (
    "https://developers.openai.com/api/docs/guides/secure-mcp-tunnels",
    "https://developers.openai.com/plugins/deploy/connect-chatgpt",
)
SCOPE = (
    "Install a local stdio MCP launcher, manifest and onboarding note for pm's own "
    "'pm chatgpt mcp' server under pm's state directory (owner-only permissions). No downloads, "
    "no network listener, no credentials. Does NOT register anything with ChatGPT: workspace "
    "permissions, OAuth and the risk warning remain the owner's platform steps."
)

_LAUNCHER = b"""#!/bin/sh
# pm guardian/chatgpt connector, plan v1: local stdio MCP server only.
# No network listener, no downloads, no credentials. pm verifies this file's digest.
PATH="$HOME/.local/bin:$PATH"
export PATH
exec pm chatgpt mcp
"""

_ONBOARDING = f"""# pm guardian connector for ChatGPT: setup notes

This directory holds a local stdio MCP launcher (`{LAUNCHER}`). Installing it does NOT connect
anything to ChatGPT. The platform steps below are yours; pm never performs or forges them.

Official references:
- {DOCS[0]}
- {DOCS[1]}

1. In Platform tunnel settings ({TUNNEL_SETTINGS}) create or choose a tunnel and associate it
   with the target ChatGPT workspace. You need Tunnels Read + Manage (create) and Read + Use (run).
2. Record the tunnel's non-secret id for pm: `pm chatgpt connector tunnel-id <tunnel_id>`.
3. Enter the tunnel-client runtime API key locally through a secure input or environment
   reference (`CONTROL_PLANE_API_KEY`). Never put it in chat, command arguments, config files or logs.
4. `tunnel-client init --sample sample_mcp_stdio_local --profile {PROFILE} --tunnel-id <tunnel_id> --mcp-command "<path to {LAUNCHER}>"`
5. `tunnel-client doctor --profile {PROFILE} --explain`, then `tunnel-client run --profile {PROFILE}`
   and keep it running.
6. In ChatGPT Plugins choose Add custom MCP server, Connection: Tunnel, select the tunnel,
   configure authentication, review the risk warning, then Create as a plugin.

Setup is complete only after a real ChatGPT tool call succeeds. Until then pm reports
`awaiting_platform_connection` and never `connected`.
""".encode()


class ConnectorError(Exception):
    """An unsafe or invalid connector operation; the message is safe to show."""


@dataclass(frozen=True)
class InstallResult:
    changed: bool
    files: tuple[str, ...]


def _manifest() -> bytes:
    from .mcp_tools import READ_TOOL_NAMES, REQUEST_TOOL_NAMES

    document = {
        "schema": PLAN_VERSION,
        "connector": CONNECTOR,
        "target": TARGET,
        "transport": "stdio",
        "launcher": LAUNCHER,
        "command": ["pm", "chatgpt", "mcp"],
        "listens": False,
        "downloads": False,
        "tools": {"read": list(READ_TOOL_NAMES), "request": list(REQUEST_TOOL_NAMES)},
        "approval": "every write tool only queues a request; the owner approves in Telegram",
        "tunnel": {
            "tunnel_id": None,
            "tunnel_id_source": TUNNEL_SETTINGS,
            "runtime_key_env": "CONTROL_PLANE_API_KEY",
            "docs": list(DOCS),
        },
        "platform": {
            "workspace_registration": "owner, in ChatGPT Plugins",
            "oauth_and_risk_warning": "owner, in ChatGPT",
            "verified_connected": False,
        },
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def render() -> dict[str, tuple[bytes, int]]:
    """Every file of the plan as ``name -> (content, mode)``."""
    return {
        LAUNCHER: (_LAUNCHER, 0o700),
        MANIFEST: (_manifest(), 0o600),
        ONBOARDING: (_ONBOARDING, 0o600),
    }


def plan() -> dict:
    files = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "mode": f"{mode:04o}"}
        for name, (data, mode) in render().items()
    }
    canonical = json.dumps(
        {"v": PLAN_VERSION, "files": files, "scope": SCOPE}, sort_keys=True, separators=(",", ":")
    )
    return {
        "connector": CONNECTOR,
        "target": TARGET,
        "scope": SCOPE,
        "files": files,
        "plan_digest": hashlib.sha256(canonical.encode()).hexdigest(),
    }


def _open_dir(parent_fd: int | None, name: str | Path, *, create: bool) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent_fd)
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError:
        raise ConnectorError(f"{Path(name).name} is missing or not a plain directory") from None
    info = os.fstat(fd)
    if info.st_uid != os.getuid():
        os.close(fd)
        raise ConnectorError(f"{Path(name).name} is not owned by the current user")
    if create and stat.S_IMODE(info.st_mode) & 0o077:
        os.fchmod(fd, 0o700)
    return fd


def _walk(state_dir: Path, *, create: bool) -> int:
    """An fd for the connector directory, descending without ever following a symlink."""
    state_dir = Path(state_dir)
    if create:
        try:
            ensure_private_dir(state_dir)
        except LedgerError as exc:
            raise ConnectorError(str(exc)) from exc
    fd = _open_dir(None, state_dir, create=False)
    try:
        for part in SUBDIRS:
            child = _open_dir(fd, part, create=create)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_regular(dir_fd: int, name: str) -> tuple[bytes, int] | None:
    """``(content, mode)`` of a regular file, ``None`` if absent; refuses anything else."""
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd
        )
    except FileNotFoundError:
        return None
    except OSError:
        raise ConnectorError(f"{name} is a symlink or cannot be opened safely") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ConnectorError(f"{name} is not a regular file owned by the current user")
        return os.read(fd, 1 << 20), stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def _write_atomic(dir_fd: int, name: str, data: bytes, mode: int) -> None:
    temp = f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    fd = os.open(
        temp,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=dir_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short write made no progress")
            view = view[written:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(temp, dir_fd=dir_fd)
        raise
    os.close(fd)
    os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    os.fsync(dir_fd)


def install(state_dir: Path) -> InstallResult:
    """Write the fixed plan under ``state_dir``; idempotent, refuses symlinks and odd files."""
    dir_fd = _walk(state_dir, create=True)
    try:
        changed = []
        for name, (data, mode) in render().items():
            current = _read_regular(dir_fd, name)
            if current == (data, mode):
                continue
            _write_atomic(dir_fd, name, data, mode)
            changed.append(name)
        return InstallResult(bool(changed), tuple(changed))
    finally:
        os.close(dir_fd)


def _verify(state_dir: Path) -> tuple[bool, str | None]:
    """``(ok, problem)``: ``(False, None)`` means not installed, a problem string means invalid."""
    try:
        dir_fd = _walk(state_dir, create=False)
    except ConnectorError:
        if not Path(state_dir, *SUBDIRS).exists() and not Path(state_dir, *SUBDIRS).is_symlink():
            return False, None
        return False, "connector directory is not a plain directory"
    try:
        for name, (data, mode) in render().items():
            try:
                current = _read_regular(dir_fd, name)
            except ConnectorError as exc:
                return False, f"{name}: {exc}"
            if current is None:
                return False, f"{name} is missing"
            if current != (data, mode):
                return False, f"{name} does not match the approved plan"
        return True, None
    finally:
        os.close(dir_fd)


def read_tunnel_id(state_dir: Path) -> str | None:
    try:
        dir_fd = _walk(state_dir, create=False)
    except ConnectorError:
        return None
    try:
        current = _read_regular(dir_fd, TUNNEL_FILE)
    except ConnectorError:
        return None
    finally:
        os.close(dir_fd)
    if current is None or current[1] != 0o600:
        return None
    try:
        value = json.loads(current[0]).get("tunnel_id")
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and TUNNEL_ID.fullmatch(value) else None


def set_tunnel_id(state_dir: Path, tunnel_id: str) -> None:
    """Record the non-secret tunnel id; only after a verified local install."""
    if not isinstance(tunnel_id, str) or TUNNEL_ID.fullmatch(tunnel_id) is None:
        raise ConnectorError(
            "tunnel_id must look like tunnel_<letters and digits>; no secrets here"
        )
    ok, problem = _verify(state_dir)
    if not ok:
        raise ConnectorError(problem or "install the connector first (request and approve it)")
    dir_fd = _walk(state_dir, create=False)
    try:
        _write_atomic(
            dir_fd,
            TUNNEL_FILE,
            (json.dumps({"tunnel_id": tunnel_id}) + "\n").encode(),
            0o600,
        )
    finally:
        os.close(dir_fd)


def setup_steps(state: str, tunnel_id: str | None, launcher: Path | None) -> list[str]:
    """Prepared, state-dependent onboarding steps; never a secret, never a fabricated id."""
    if state in ("not_installed", "awaiting_approval", "local_invalid"):
        return [
            (
                "Request the install with connectors.request {connector:'guardian',"
                "target:'chatgpt'} and approve the Telegram card; pm then writes the fixed local "
                "launcher and manifest."
            )
        ]
    steps = []
    if tunnel_id is None:
        steps.append(
            f"Create or choose a tunnel in Platform tunnel settings ({TUNNEL_SETTINGS}), associate "
            "it with the target ChatGPT workspace, then record its non-secret id: "
            "pm chatgpt connector tunnel-id <tunnel_id>"
        )
    steps.append(
        "Enter the tunnel-client runtime API key locally through a secure input or environment "
        "reference (CONTROL_PLANE_API_KEY); never paste it into chat, argv, config files or logs."
    )
    shown = tunnel_id or "<tunnel_id>"
    command = str(launcher) if launcher else LAUNCHER
    steps.append(
        f"tunnel-client init --sample sample_mcp_stdio_local --profile {PROFILE} "
        f'--tunnel-id {shown} --mcp-command "{command}"'
    )
    steps.append(f"tunnel-client doctor --profile {PROFILE} --explain")
    steps.append(f"tunnel-client run --profile {PROFILE}  (keep it running)")
    steps.append(
        "In ChatGPT Plugins: Add custom MCP server, Connection: Tunnel, select the tunnel, "
        "configure authentication, review the risk warning and continue, then Create as a plugin. "
        "These are the owner's platform steps; pm does not perform or forge them."
    )
    steps.append(
        "Setup is complete only after a real ChatGPT tool call succeeds; until then the state "
        "stays awaiting_platform_connection and is never reported as connected."
    )
    return steps


def local_state(state_dir: Path) -> dict:
    """The on-disk part of the connector state (approval state is layered on by ``list_*``)."""
    ok, problem = _verify(state_dir)
    if not ok:
        state = "not_installed" if problem is None else "local_invalid"
        return {
            "state": state,
            "problem": problem,
            "tunnel_id": None,
            "launcher": None,
            "setup_steps": setup_steps(state, None, None),
        }
    tunnel = read_tunnel_id(state_dir)
    launcher = Path(state_dir, *SUBDIRS, LAUNCHER)
    state = "local_ready" if tunnel is None else "awaiting_platform_connection"
    return {
        "state": state,
        "problem": None,
        "tunnel_id": tunnel,
        "launcher": str(launcher),
        "setup_steps": setup_steps(state, tunnel, launcher),
    }


def list_connectors(services) -> dict:
    local = local_state(services.state_dir)
    proj = services.snapshot()
    requests = [s for s in proj.requests.values() if s.action == "connector-install"]
    latest = requests[-1] if requests else None
    state = local["state"]
    if state == "not_installed" and any(s.status in ACTIVE for s in requests):
        state = "awaiting_approval"
    entry = {
        "connector": CONNECTOR,
        "target": TARGET,
        "state": state,
        "problem": local["problem"],
        "plan_digest": plan()["plan_digest"],
        "request": latest.receipt() if latest else None,
        "tunnel_id": local["tunnel_id"],
        "launcher": local["launcher"],
        "platform_verified": False,
        "docs": list(DOCS),
        "setup_steps": local["setup_steps"]
        if state != "awaiting_approval"
        else setup_steps(state, None, None),
    }
    return {"connectors": [entry]}
