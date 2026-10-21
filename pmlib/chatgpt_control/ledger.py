"""The single append-only approval ledger: every request and approval fact lives here.

Accessed via: ``service`` (submit), ``worker``/``poller`` (state changes), ``api`` (receipts).

Invariants: each ``append`` is one fsynced line written under an exclusive ``flock`` on a
sibling lock file, so concurrent processes never interleave and an acknowledged event survives a
crash. A crash mid-write leaves a torn last line: readers skip it and the next append starts on a
fresh line instead of gluing onto the fragment. The file is 0600 in a 0700 directory and is never
followed through a symlink. There is no mutable side store; ``projection.project`` derives every
status from these lines alone. A multi-step read-decide-append MUST run inside ``transaction()``;
the lock is not re-entrant, so never nest ``append`` or ``transaction`` inside it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import stat
from pathlib import Path

SCHEMA_VERSION = 1
MAX_LINE = 16384
KINDS = frozenset(
    {
        "request_created",
        "ask_started",
        "ask_abandoned",
        "ask_result",
        "request_expired",
        "request_invalidated",
        "dispatch_started",
        "dispatch_result",
        "continuation_status",
        "observation",
    }
)


class LedgerError(RuntimeError):
    """The ledger location is unsafe (symlink, foreign owner, not a regular file)."""


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) as 0700; refuse a symlink or foreign-owned leaf."""
    missing = []
    probe = path
    while not probe.exists() and not probe.is_symlink():
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        with contextlib.suppress(FileExistsError):  # another process may have just created it
            directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)  # mkdir is subject to the umask
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise LedgerError("directory must be a real directory owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def _open_private(path: Path, flags: int, *, create: bool = True) -> int:
    try:
        flags |= os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT if create else 0)
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise LedgerError(f"cannot open {path.name} safely: {exc.strerror}") from exc
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise LedgerError(f"{path.name} must be a regular file owned by the current user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.fchmod(fd, 0o600)
    return fd


def _encode(event: dict) -> bytes:
    kind = event.get("kind")
    if kind not in KINDS:
        raise ValueError(f"unknown ledger event kind {kind!r}")
    at = event.get("at")
    if type(at) not in (int, float) or not math.isfinite(at):
        raise ValueError("ledger event needs a finite numeric 'at'")
    line = json.dumps({"v": SCHEMA_VERSION, **event}, separators=(",", ":"), allow_nan=False)
    data = line.encode("utf-8") + b"\n"
    if len(data) > MAX_LINE:
        raise ValueError("ledger event is too large")
    return data


def _decode(raw: bytes) -> list[dict]:
    events = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if isinstance(event, dict) and isinstance(event.get("kind"), str):
            events.append(event)
    return events


class Transaction:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def events(self) -> list[dict]:
        chunks = []
        offset = 0
        while True:
            chunk = os.pread(self._fd, 1 << 20, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        return _decode(b"".join(chunks))

    def append(self, event: dict) -> dict:
        data = _encode(event)
        size = os.fstat(self._fd).st_size
        if size and os.pread(self._fd, 1, size - 1) != b"\n":
            data = b"\n" + data  # never glue onto a torn fragment
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]
        os.fsync(self._fd)
        return event


class Ledger:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / "approvals.jsonl"
        self.lock_path = self.directory / "approvals.lock"

    @contextlib.contextmanager
    def transaction(self):
        ensure_private_dir(self.directory)
        lock_fd = _open_private(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            data_fd = _open_private(self.path, os.O_RDWR | os.O_APPEND)
            try:
                yield Transaction(data_fd)
            finally:
                os.close(data_fd)
        finally:
            os.close(lock_fd)  # closing releases the flock

    def append(self, event: dict) -> dict:
        with self.transaction() as tx:
            return tx.append(event)

    def read_all(self) -> list[dict]:
        if not self.path.exists() and not self.path.is_symlink():
            return []
        fd = _open_private(self.path, os.O_RDONLY, create=False)
        try:
            return Transaction(fd).events()
        finally:
            os.close(fd)
