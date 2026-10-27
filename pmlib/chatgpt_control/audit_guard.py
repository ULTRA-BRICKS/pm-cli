"""Polite-run guards for the audit reader: one reader at a time, spaced, bounded, stoppable.

Accessed via: ``audit_collector`` (every request goes through ``GuardedReader``) and the CLI
(``RunLock`` around the whole run).

Invariants: a non-blocking ``flock`` makes a second concurrent audit exit instead of reading in
parallel. Consecutive requests start at least ``Limits.min_gap_seconds`` (4 s) apart, each request
has a per-request timeout (15 s, enforced by the reader), and the whole run has a budget well
under the 30-minute timer period; running out of budget raises ``BudgetExhausted`` BEFORE a
request is made and is reported as partial coverage, never as success. HTTP 401, 403 and 429 stop
the account immediately: ``AccountStopped`` latches (no later request is sent), the response's
``Retry-After`` (delta-seconds or HTTP-date, else an hour, capped at a day) becomes a persisted
per-account ``not_before``, and later runs skip that account until it passes. Other HTTP errors and
timeouts propagate for the collector to count. Time and sleep are injected, so every rule is
testable without waiting. The state files are 0600 in a 0700 directory, never through a symlink.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Self

from .audit_reader import ReaderHttpError
from .ledger import LedgerError, ensure_private_dir, open_private

STOP_STATUSES = (401, 403, 429)
DEFAULT_BACKOFF_SECONDS = 3600.0
MAX_BACKOFF_SECONDS = 86400.0
MIN_GAP_SECONDS = 4.0
MAX_BUDGET_SECONDS = 30 * 60


class LockBusy(RuntimeError):
    """Another audit already holds the run lock."""


class LockUnsafe(RuntimeError):
    """The lock path is a symlink or not owned by this user."""


class BudgetExhausted(RuntimeError):
    """The run budget would be exceeded by the next request; partial coverage is reported."""


class AccountStopped(RuntimeError):
    """HTTP 401/403/429: reading for this account stopped and a ``not_before`` was persisted."""

    def __init__(self, status: int, not_before: float) -> None:
        super().__init__(f"HTTP {status}; not before {not_before:.0f}")
        self.status = status
        self.not_before = not_before


@dataclass(frozen=True)
class Limits:
    min_gap_seconds: float = MIN_GAP_SECONDS
    request_timeout_seconds: float = 15.0
    budget_seconds: float = 20 * 60.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.min_gap_seconds) or self.min_gap_seconds < MIN_GAP_SECONDS:
            raise ValueError(f"min_gap_seconds must be at least {MIN_GAP_SECONDS}")
        if not math.isfinite(self.request_timeout_seconds) or not (
            0 < self.request_timeout_seconds <= 60
        ):
            raise ValueError("request_timeout_seconds must be within (0, 60]")
        if not math.isfinite(self.budget_seconds) or not (
            0 < self.budget_seconds < MAX_BUDGET_SECONDS
        ):
            raise ValueError("budget_seconds must be positive and below the 30-minute period")


class RunLock:
    """Exclusive, non-blocking, process-lifetime lock: only one audit reads at a time."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def __enter__(self) -> Self:
        try:
            ensure_private_dir(self.path.parent)
            fd = open_private(self.path, os.O_RDWR)
        except LedgerError as exc:
            raise LockUnsafe(str(exc)) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise LockBusy("another audit is already running") from None
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing releases the flock
            self._fd = None


def retry_after_seconds(header: str | None, now: float) -> float | None:
    """Delay in [0, MAX_BACKOFF_SECONDS] from a ``Retry-After`` value, or None when unusable."""
    if not isinstance(header, str):
        return None
    text = header.strip()
    if text.isascii() and text.isdigit():
        return min(float(int(text)), MAX_BACKOFF_SECONDS)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max(when.timestamp() - now, 0.0), MAX_BACKOFF_SECONDS)


class NotBeforeStore:
    """Per-account "do not read before" timestamps, persisted so later runs skip the account."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, float]:
        try:
            fd = open_private(self.path, os.O_RDONLY, create=False)
        except LedgerError:
            return {}
        try:
            with os.fdopen(fd, "rb") as handle:
                data = json.loads(handle.read(65536))
        except (OSError, ValueError, RecursionError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            k: float(v)
            for k, v in data.items()
            if isinstance(k, str) and type(v) in (int, float) and math.isfinite(v)
        }

    def get(self, account: str) -> float | None:
        return self._load().get(account)

    def active(self, account: str, now: float) -> float | None:
        """The pending ``not_before`` for ``account`` while it is still in the future."""
        value = self.get(account)
        return value if value is not None and value > now else None

    def _save(self, data: dict[str, float]) -> None:
        ensure_private_dir(self.path.parent)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = open_private(tmp, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "wb") as handle:
            handle.write(json.dumps(data, sort_keys=True, allow_nan=False).encode())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    def set(self, account: str, not_before: float) -> None:
        data = self._load()
        data[account] = not_before
        self._save(data)

    def clear(self, account: str) -> None:
        data = self._load()
        if data.pop(account, None) is not None:
            self._save(data)


class Budget:
    """Wall-clock budget for one run; ``exhausted`` is sticky once the budget has been hit."""

    def __init__(self, clock: Callable[[], float], total_seconds: float) -> None:
        self._clock = clock
        self.started = clock()
        self.deadline = self.started + total_seconds
        self.requests = 0
        self._hit = False

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def elapsed(self) -> float:
        return self._clock() - self.started

    def exhausted(self) -> bool:
        return self._hit or self.remaining() <= 0

    def require(self, needed: float = 0.0) -> None:
        """Raise ``BudgetExhausted`` unless ``needed`` more seconds fit before the deadline."""
        if self.remaining() - needed <= 0:
            self._hit = True
            raise BudgetExhausted("run budget exhausted")


class Pacer:
    """Keeps consecutive request STARTS at least ``min_gap`` seconds apart."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        min_gap: float = MIN_GAP_SECONDS,
    ) -> None:
        self._clock, self._sleep, self.min_gap = clock, sleep, min_gap
        self._last: float | None = None

    def needed(self) -> float:
        if self._last is None:
            return 0.0
        return max(0.0, self.min_gap - (self._clock() - self._last))

    def wait(self) -> None:
        delay = self.needed()
        if delay > 0:
            self._sleep(delay)
        self._last = self._clock()


class GuardedReader:
    """Wraps a ``PageReader`` so every call is budgeted, spaced and stop-status aware."""

    def __init__(
        self,
        reader,
        *,
        account: str,
        pacer: Pacer,
        budget: Budget,
        store: NotBeforeStore,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._reader, self.account = reader, account
        self._pacer, self._budget, self._store, self._clock = pacer, budget, store, clock
        self._stopped: AccountStopped | None = None

    def _call(self, name: str, *args):
        if self._stopped is not None:
            raise self._stopped
        self._budget.require(self._pacer.needed())
        self._pacer.wait()
        self._budget.requests += 1
        try:
            return getattr(self._reader, name)(*args)
        except ReaderHttpError as exc:
            if exc.status not in STOP_STATUSES:
                raise
            delay = retry_after_seconds(exc.retry_after, self._clock())
            not_before = self._clock() + (DEFAULT_BACKOFF_SECONDS if delay is None else delay)
            with contextlib.suppress(OSError, LedgerError):
                self._store.set(self.account, not_before)
            self._stopped = AccountStopped(exc.status, not_before)
            raise self._stopped from None

    def identity(self):
        return self._call("identity")

    def list_page(self, scope, offset, limit):
        return self._call("list_page", scope, offset, limit)

    def list_projects(self, offset, limit):
        return self._call("list_projects", offset, limit)

    def read_conversation(self, conversation_id, turns):
        return self._call("read_conversation", conversation_id, turns)

    def close(self) -> None:
        self._reader.close()
