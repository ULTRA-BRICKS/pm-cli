"""Polite-run guards: one reader (flock), request spacing, 401/403/429 stop, Retry-After, budget."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC

import pytest

from pmlib.chatgpt_control import audit_guard as guard
from pmlib.chatgpt_control import audit_reader as rd
from tests.audit_support import NOW


class FakeTime:
    """Deterministic clock + sleep: sleeping advances the clock and is recorded."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Counting:
    """A PageReader stand-in whose calls take ``cost`` seconds and can raise a queued error."""

    def __init__(self, t: FakeTime, cost: float = 0.5) -> None:
        self.t, self.cost = t, cost
        self.calls: list[float] = []
        self.errors: list[Exception] = []

    def read_conversation(self, conversation_id, turns):
        self.calls.append(self.t.now)
        self.t.now += self.cost
        if self.errors:
            raise self.errors.pop(0)
        return {"id": conversation_id}


def make(tmp_path, t, **limits):
    store = guard.NotBeforeStore(tmp_path / "audit" / "not_before.json")
    budget = guard.Budget(t.clock, total_seconds=limits.pop("budget", 1200.0))
    pacer = guard.Pacer(t.clock, t.sleep, min_gap=limits.pop("gap", 4.0))
    return store, budget, pacer


def guarded(tmp_path, t, reader, account="home", **limits):
    store, budget, pacer = make(tmp_path, t, **limits)
    g = guard.GuardedReader(
        reader, account=account, pacer=pacer, budget=budget, store=store, clock=t.clock
    )
    return g, store, budget


def test_requests_are_spaced_at_least_four_seconds(tmp_path):
    t = FakeTime()
    reader = Counting(t, cost=0.5)
    g, _, _ = guarded(tmp_path, t, reader)
    for _ in range(4):
        g.read_conversation("c", 6)
    gaps = [b - a for a, b in zip(reader.calls, reader.calls[1:], strict=False)]
    assert all(gap >= 4.0 for gap in gaps)
    assert reader.calls[0] == NOW  # the first request never waits


def test_first_request_does_not_sleep(tmp_path):
    t = FakeTime()
    g, _, _ = guarded(tmp_path, t, Counting(t))
    g.read_conversation("c", 6)
    assert t.sleeps == []


@pytest.mark.parametrize("status", [401, 403, 429])
def test_stop_statuses_halt_immediately_and_persist_a_not_before(tmp_path, status):
    t = FakeTime()
    reader = Counting(t)
    reader.errors = [rd.ReaderHttpError(status, None)]
    g, store, _ = guarded(tmp_path, t, reader)
    with pytest.raises(guard.AccountStopped) as caught:
        g.read_conversation("c", 6)
    assert caught.value.status == status
    assert caught.value.not_before == pytest.approx(NOW + 0.5 + guard.DEFAULT_BACKOFF_SECONDS)
    assert store.get("home") == caught.value.not_before
    with pytest.raises(guard.AccountStopped):  # latched: no second request is ever made
        g.read_conversation("c2", 6)
    assert len(reader.calls) == 1


class ListingReader:
    """Listing calls only: records every call and raises one queued error."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.errors: list[Exception] = []

    def list_projects(self, offset, limit):
        self.calls.append(("projects", offset, limit))
        if self.errors:
            raise self.errors.pop(0)
        return {"items": []}


def test_project_listing_is_paced_and_halts_on_a_stop_status_like_every_other_read(tmp_path):
    t = FakeTime()
    reader = ListingReader()
    g, store, _ = guarded(tmp_path, t, reader)
    assert g.list_projects(0, 20) == {"items": []}
    reader.errors = [rd.ReaderHttpError(429, "30")]
    with pytest.raises(guard.AccountStopped):
        g.list_projects(20, 20)
    assert t.sleeps == [4.0]  # the second listing waited out the 4 s gap
    assert store.get("home") == pytest.approx(NOW + 4.0 + 30)
    with pytest.raises(guard.AccountStopped):
        g.list_projects(40, 20)
    assert len(reader.calls) == 2


def test_retry_after_seconds_is_honored(tmp_path):
    t = FakeTime()
    reader = Counting(t, cost=0)
    reader.errors = [rd.ReaderHttpError(429, "120")]
    g, _, _ = guarded(tmp_path, t, reader)
    with pytest.raises(guard.AccountStopped) as caught:
        g.read_conversation("c", 6)
    assert caught.value.not_before == NOW + 120


@pytest.mark.parametrize(
    "header,expected",
    [
        ("120", 120.0),
        ("0", 0.0),
        (" 30 ", 30.0),
        ("Wed, 28 Oct 2026 07:20:00 GMT", None),  # HTTP-date: checked separately below
        ("abc", None),
        ("-5", None),
        ("1e3", None),
        ("", None),
        (None, None),
        ("99999999999", guard.MAX_BACKOFF_SECONDS),
    ],
)
def test_retry_after_parsing(header, expected):
    got = guard.retry_after_seconds(header, NOW)
    if header and header.startswith("Wed"):
        assert got is not None and got > 0
    else:
        assert got == expected


def test_retry_after_http_date_is_relative_to_now():
    from datetime import datetime
    from email.utils import format_datetime

    when = datetime.fromtimestamp(NOW + 90, tz=UTC)
    assert guard.retry_after_seconds(format_datetime(when, usegmt=True), NOW) == pytest.approx(90)


def test_not_before_in_the_future_skips_the_account(tmp_path):
    t = FakeTime()
    store = guard.NotBeforeStore(tmp_path / "audit" / "not_before.json")
    store.set("home", NOW + 600)
    assert store.active("home", NOW) == NOW + 600
    assert store.active("home", NOW + 600) is None
    assert store.active("glide.vc", NOW) is None
    store.clear("home")
    assert store.get("home") is None
    assert t.now == NOW


def test_not_before_file_is_private_and_corruption_is_ignored(tmp_path):
    store = guard.NotBeforeStore(tmp_path / "audit" / "not_before.json")
    store.set("home", NOW + 5)
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    store.path.write_text("{not json")
    assert store.get("home") is None
    store.path.write_text(json.dumps({"home": float("nan")}).replace("NaN", "NaN"))
    assert store.get("home") is None
    store.path.write_text(json.dumps({"home": "tomorrow"}))
    assert store.get("home") is None


def test_budget_exhaustion_stops_before_the_next_request(tmp_path):
    t = FakeTime()
    reader = Counting(t, cost=0.5)
    g, _, budget = guarded(tmp_path, t, reader, budget=7.0)
    g.read_conversation("a", 6)
    g.read_conversation("b", 6)  # waits 3.5 s, now 4.5 s into the budget
    with pytest.raises(guard.BudgetExhausted):
        g.read_conversation("c", 6)  # would start at 8.0 s, past the 7 s deadline
    assert len(reader.calls) == 2 and budget.exhausted()


def test_budget_counts_requests(tmp_path):
    t = FakeTime()
    g, _, budget = guarded(tmp_path, t, Counting(t))
    g.read_conversation("a", 6)
    g.read_conversation("b", 6)
    assert budget.requests == 2


def test_other_http_errors_and_timeouts_propagate_without_a_stop(tmp_path):
    t = FakeTime()
    reader = Counting(t)
    reader.errors = [
        rd.ReaderHttpError(404, None),
        rd.ReaderTimeout("slow"),
        rd.ReaderHttpError(500, None),
    ]
    g, store, _ = guarded(tmp_path, t, reader)
    for expected in (rd.ReaderHttpError, rd.ReaderTimeout, rd.ReaderHttpError):
        with pytest.raises(expected):
            g.read_conversation("c", 6)
    assert store.get("home") is None
    assert g.read_conversation("c", 6) == {"id": "c"}


def test_not_before_is_capped(tmp_path):
    t = FakeTime()
    reader = Counting(t, cost=0)
    reader.errors = [rd.ReaderHttpError(429, "99999999999")]
    g, _, _ = guarded(tmp_path, t, reader)
    with pytest.raises(guard.AccountStopped) as caught:
        g.read_conversation("c", 6)
    assert caught.value.not_before == NOW + guard.MAX_BACKOFF_SECONDS


def test_run_lock_is_exclusive_and_releases(tmp_path):
    path = tmp_path / "audit" / "audit.lock"
    first = guard.RunLock(path)
    with first:
        with pytest.raises(guard.LockBusy), guard.RunLock(path):
            pass
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    with guard.RunLock(path):  # released after the first exits
        pass


def test_run_lock_refuses_a_symlink(tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("")
    (directory / "audit.lock").symlink_to(target)
    with pytest.raises(guard.LockUnsafe), guard.RunLock(directory / "audit.lock"):
        pass


def test_limits_defaults_match_the_politeness_contract():
    limits = guard.Limits()
    assert limits.min_gap_seconds == 4.0
    assert limits.request_timeout_seconds == 15.0
    assert limits.budget_seconds < 30 * 60
    with pytest.raises(ValueError):
        guard.Limits(min_gap_seconds=1.0)
    with pytest.raises(ValueError):
        guard.Limits(request_timeout_seconds=0)
    with pytest.raises(ValueError):
        guard.Limits(budget_seconds=0)
    with pytest.raises(ValueError):
        guard.Limits(budget_seconds=30 * 60)
