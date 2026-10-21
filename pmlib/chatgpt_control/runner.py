"""The ``pm chatgpt approvals run`` loop: propose, ask, execute, repeat; one worker at a time.

Accessed via: ``cli.run_approvals`` (and the systemd user unit template under ``packaging/``).

Invariants: a non-blocking ``flock`` on ``worker.lock`` guarantees a single worker process, so a
restart (or a second invocation) can never duplicate a question; at startup the worker resolves
what a previous process left open as uncertain instead of replaying it. Each tick runs the poller
(unless ``--no-propose``) and then drains at most ``max_requests`` queued requests one at a time.
One JSON line per tick goes to stdout; it carries receipts and counters only, never secrets.
Existing failed-recovery stays in Guardian and is independent of this loop.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Self

from ..chatgpt_tui import Action, run_action
from . import connector, poller
from .guardian_client import GuardianClient
from .ledger import ensure_private_dir
from .service import Services
from .tg_transport import ask_owner, discover_owner
from .worker import Worker

EXIT_BUSY = 75
MIN_INTERVAL = 30
MAX_INTERVAL = 3600


class WorkerBusy(RuntimeError):
    pass


class WorkerLock:
    """Exclusive, non-blocking, process-lifetime lock proving this is the only approvals worker."""

    def __init__(self, directory: Path) -> None:
        self._path = Path(directory) / "worker.lock"
        self._directory = Path(directory)
        self._fd: int | None = None

    def __enter__(self) -> Self:
        ensure_private_dir(self._directory)
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise WorkerBusy("another approvals worker is already running") from None
        self._fd = fd
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing releases the flock
            self._fd = None


def check_interval(value: object) -> int:
    if type(value) is not int or not MIN_INTERVAL <= value <= MAX_INTERVAL:
        raise ValueError(
            f"interval must be an integer from {MIN_INTERVAL} to {MAX_INTERVAL} seconds"
        )
    return value


def build_worker(
    services: Services,
    *,
    tg_argv: tuple[str, ...] = ("tg-ctl",),
    guardian=None,
    owner_ref: Callable[[], str | None] = discover_owner,
    run_control: Callable | None = None,
    install: Callable = connector.install,
    poll_seconds: int = 10,
    sleep: Callable[[float], None] = time.sleep,
) -> Worker:
    return Worker(
        services,
        transport=lambda question, timeout: ask_owner(question, timeout, argv_prefix=tg_argv),
        guardian=guardian or GuardianClient(),
        run_control=run_control
        or (lambda account, action, chat: run_action(Action(account, action, chat))),
        install=install,
        owner_ref=owner_ref,
        poll_seconds=poll_seconds,
        sleep=sleep,
    )


def run_loop(
    services: Services,
    worker: Worker,
    *,
    once: bool,
    interval: int,
    propose: bool = True,
    max_requests: int = 4,
    out=None,
    err=None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        with WorkerLock(services.state_dir):
            worker.recover()
            while True:
                line: dict = {"at": services.clock()}
                if propose:
                    line["poll"] = poller.poll_once(services)
                line["processed"] = worker.tick(max_requests)
                print(json.dumps(line, separators=(",", ":")), file=out, flush=True)
                if once:
                    return 0
                sleep(interval)
    except WorkerBusy as exc:
        print(f"error: {exc}", file=err)
        return EXIT_BUSY
    except KeyboardInterrupt:
        print("approvals worker interrupted; open requests stay as recorded", file=err)
        return 130
