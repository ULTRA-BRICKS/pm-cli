"""Private, bounded state files for the task audit: atomic JSON documents and append-only logs.

Accessed via: ``audit_store`` (verdict cache, last-run report), ``audit_notify`` (delivery log)
and ``audit_validation`` (validation record). One place decides how audit state touches disk.

Invariants: every file is 0600 in a 0700 directory that we own and is never followed through a
symlink (``UnsafePath``). A JSON document is written whole to a sibling temp file, fsynced and
renamed over the target, so a reader sees the old or the new document and never a torn one; NaN,
Infinity and oversize documents are refused BEFORE anything is created. Reading is lenient by
design: a missing, corrupt, oversize, non-finite or symlinked document yields the caller's default
(the audit then simply has no cache), never an exception. An ``AppendLog`` holds one fsynced JSON
object per line under an exclusive ``flock``, skips a torn last line, starts the next append on a
fresh line, and rotates (keeping the newest entries) so it stays bounded.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path

from .ledger import LedgerError, ensure_private_dir, open_private

DEFAULT_MAX_BYTES = 1 << 20
LOG_SCHEMA = 1
LOG_MAX_BYTES = 256 * 1024
LOG_MAX_LINE = 4096


class UnsafePath(RuntimeError):
    """The path is a symlink, foreign-owned or otherwise not a private regular file."""


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def _dumps(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def read_json(path: Path, *, default: object = None, max_bytes: int = DEFAULT_MAX_BYTES):
    """The JSON document at ``path`` or ``default`` when it is absent or not trustworthy."""
    try:
        fd = open_private(Path(path), os.O_RDONLY, create=False)
    except LedgerError:
        return default
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(max_bytes + 1)
    except OSError:
        return default
    if len(raw) > max_bytes:
        return default
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return default


def write_json(path: Path, value: object, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
    """Atomically replace ``path`` with ``value``; ``ValueError`` before any file is created."""
    path = Path(path)
    data = _dumps(value)  # raises ValueError on NaN/Infinity
    if len(data) > max_bytes:
        raise ValueError(f"document exceeds {max_bytes} bytes")
    tmp = path.with_name(path.name + ".tmp")
    try:
        ensure_private_dir(path.parent)
        if os.path.islink(path):
            raise UnsafePath(f"{path.name} is a symlink")
        fd = open_private(tmp, os.O_WRONLY | os.O_TRUNC)
    except LedgerError as exc:
        raise UnsafePath(str(exc)) from exc
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


@contextlib.contextmanager
def file_lock(path: Path):
    """An exclusive ``flock`` on ``<path>.lock`` for one read-modify-write of ``path``."""
    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    try:
        ensure_private_dir(path.parent)
        fd = open_private(lock, os.O_RDWR)
    except LedgerError as exc:
        raise UnsafePath(str(exc)) from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the flock


class AppendLog:
    """A bounded append-only JSON-lines log with a closed set of event kinds."""

    def __init__(
        self,
        path: Path,
        *,
        kinds: set[str] | frozenset[str],
        max_bytes: int = LOG_MAX_BYTES,
        max_line: int = LOG_MAX_LINE,
    ) -> None:
        self.path = Path(path)
        self.kinds = frozenset(kinds)
        self.max_bytes = max_bytes
        self.max_line = max_line

    def _encode(self, event: dict) -> bytes:
        if not isinstance(event, dict) or event.get("kind") not in self.kinds:
            raise ValueError("event kind is not allowed")
        at = event.get("at")
        if type(at) not in (int, float) or not math.isfinite(at):
            raise ValueError("event needs a finite numeric 'at'")
        line = _dumps({**event, "v": LOG_SCHEMA}) + b"\n"
        if len(line) > self.max_line:
            raise ValueError(f"event exceeds {self.max_line} bytes")
        return line

    def _open(self, flags: int, *, create: bool = True) -> int:
        try:
            return open_private(self.path, flags, create=create)
        except LedgerError as exc:
            raise UnsafePath(str(exc)) from exc

    def _locked(self):
        return file_lock(self.path)

    def _tail_state(self) -> tuple[int, bool]:
        """(size, ends mid-line) of the log; a crash mid-write leaves a torn last line."""
        fd = self._open(os.O_RDONLY)
        try:
            size = os.fstat(fd).st_size
            return size, bool(size) and os.pread(fd, 1, size - 1) != b"\n"
        finally:
            os.close(fd)

    def _rotate(self, incoming: int) -> None:
        """Keep only the newest whole lines that fit, with ``incoming`` bytes, in half the bound."""
        fd = self._open(os.O_RDONLY)
        with os.fdopen(fd, "rb") as handle:
            lines = handle.read(self.max_bytes + self.max_line).splitlines(keepends=True)
        keep: list[bytes] = []
        size = incoming
        for line in reversed(lines):
            if not line.endswith(b"\n") or size + len(line) > self.max_bytes // 2:
                continue
            keep.append(line)
            size += len(line)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            out = open_private(tmp, os.O_WRONLY | os.O_TRUNC)
        except LedgerError as exc:
            raise UnsafePath(str(exc)) from exc
        with os.fdopen(out, "wb") as handle:
            handle.write(b"".join(reversed(keep)))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def append(self, event: dict) -> None:
        line = self._encode(event)
        with self._locked():
            size, torn = self._tail_state()
            if size + len(line) > self.max_bytes:
                self._rotate(len(line))  # rewrites whole lines only, so no torn tail remains
                torn = False
            fd = self._open(os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, (b"\n" if torn else b"") + line)
                os.fsync(fd)
            finally:
                os.close(fd)

    def read_all(self) -> list[dict]:
        """Every intact event in order; a torn or malformed line is skipped."""
        try:
            fd = self._open(os.O_RDONLY, create=False)
        except UnsafePath:
            return []
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(self.max_bytes + self.max_line + 1)
        events = []
        for line in raw.splitlines():
            try:
                event = json.loads(line.decode("utf-8"), parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                continue
            if isinstance(event, dict) and event.get("kind") in self.kinds:
                events.append(event)
        return events
