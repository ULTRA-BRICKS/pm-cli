"""One audit run: list every conversation of each requested account, read what changed, decide.

Accessed via: ``pm chatgpt audit`` (``cli.run_audit``). Every collaborator (reader factory,
classifier, store, bindings, clock, sleep) is injected through ``Environment`` so the whole run is
exercised with fakes; nothing here imports a browser, a model or a network client.

Invariants: the run holds the ``RunLock`` for its whole duration and reads through ONE shared
``Budget`` and ONE shared ``Pacer`` (requests at least ``limits.min_gap_seconds`` apart across all
accounts). Nothing about an account is read until its browser session is proven (through the
recorded binding) to be that account, so another account's chats are never audited as this one. A
HTTP 401/403/429 stops the account at once and persists a ``not_before``. Archived chats are only
READ: the reader has no call that could unarchive, resume or write. An unavailable, stopped or
partly-read account is reported as exactly that, never as empty or fully audited; the coverage
counters obey ``in_window == read + cached + sum(skipped.values())``. A cached verdict is reused
only when it was produced under the current prompt version, thresholds and model, for unchanged
listing metadata, and is never a working/unknown one; reuse re-stamps ``verified_at`` so the
poller's freshness window stays honest. No conversation text is ever stored or reported.
"""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import audit_classifier, audit_native, audit_report, audit_urls, audit_validation
from .audit_bindings import AccountBindings
from .audit_decision import DEFAULT_THRESHOLDS, Thresholds, decide, thresholds_digest
from .audit_guard import (
    AccountStopped,
    Budget,
    BudgetExhausted,
    GuardedReader,
    Limits,
    NotBeforeStore,
    Pacer,
    RunLock,
)
from .audit_reader import (
    ListScope,
    PageReader,
    ReaderError,
    ReaderHttpError,
    ReaderTimeout,
    ReaderUnavailable,
)
from .audit_store import AuditStore, CachedVerdict

PAGE_LIMIT = 100
MAX_PAGES_PER_SCOPE = 50
CLASSIFIER_WAIT_SECONDS = 150.0
CLASSIFIER_POLL_SECONDS = 5.0
REUSABLE_STATES = ("done", "incomplete", "blocked")
ORIGINS = ("any", "tpp", "non-tpp")


class Classifier(Protocol):
    def classify(self, evidence): ...


@dataclass(frozen=True)
class AuditRequest:
    workspaces: tuple[str, ...]
    days: float = 14.0
    turns: int = 4
    include_text: bool = False


@dataclass(frozen=True)
class Environment:
    reader_factory: Callable[[str], PageReader]
    classifier: Classifier
    classifier_model: Callable[[], str]  # current Laya model name, "" while no socket exists
    store: AuditStore
    not_before: NotBeforeStore
    bindings: AccountBindings
    lock_path: Path
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    thresholds: Thresholds = DEFAULT_THRESHOLDS
    limits: Limits = field(default_factory=Limits)
    bench_digest: str | None = None


def reason_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "unavailable"


def _reader_failure(exc: Exception) -> str:
    if isinstance(exc, ReaderHttpError):
        return f"http-{exc.status}"
    if isinstance(exc, ReaderTimeout):
        return "timeout"
    return "unusable"


class _Classification:
    """Run-wide classifier use: one warm-up wait, one-call circuit breaker, status summary."""

    def __init__(self, env: Environment) -> None:
        self._env = env
        self._warmed = False
        self._outage: audit_classifier.Classification | None = None
        self.calls: list[audit_classifier.Classification] = []

    def call(self, evidence) -> audit_classifier.Classification:
        if self._outage is not None:
            return self._outage
        self._warm_up()
        result = self._env.classifier.classify(evidence)
        self.calls.append(result)
        if result.status == "unavailable":
            self._outage = result
        return result

    def _warm_up(self) -> None:
        # The systemd unit starts the model before this process; here we only wait for its socket
        # and then continue with whatever exists (unavailable gives ``unknown``, never ``done``).
        if self._warmed:
            return
        self._warmed = True
        waited = 0.0
        while not self._env.classifier_model() and waited < CLASSIFIER_WAIT_SECONDS:
            self._env.sleep(CLASSIFIER_POLL_SECONDS)
            waited += CLASSIFIER_POLL_SECONDS

    def summary(self, fallback_model: str) -> dict:
        statuses = {c.status for c in self.calls}
        status = next(
            (s for s in ("unavailable", "invalid") if s in statuses),
            "ok" if statuses else "not_needed",
        )
        worst = next((c for c in self.calls if c.status == status), None)
        model = next((c.model for c in self.calls if c.model), fallback_model)
        return {
            "status": status,
            "model": model,
            "prompt_version": audit_classifier.PROMPT_VERSION,
            "reason": worst.reason if worst is not None and status != "ok" else "",
        }


class _Account:
    """State of one account's audit: its report block, the union of listed conversations."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.report = audit_report.new_account_report()
        self.union: dict[str, audit_native.ListedConversation] = {}
        self.pages_ok = 0
        self.halt: str | None = None  # "budget-exhausted" | "stopped"
        self.stopped: AccountStopped | None = None

    @property
    def coverage(self) -> dict:
        return self.report["coverage"]

    def skip(self, reason: str, count: int = 1) -> None:
        skipped = self.coverage["skipped"]
        skipped[reason] = skipped.get(reason, 0) + count

    def absorb(self, item: audit_native.ListedConversation) -> None:
        old = self.union.get(item.conversation_id)
        if old is None:
            self.union[item.conversation_id] = item
            return
        self.union[item.conversation_id] = dataclasses.replace(
            old,
            archived=old.archived or item.archived,
            project_id=old.project_id or item.project_id,
            updated_at=max(old.updated_at or 0.0, item.updated_at or 0.0),
        )


def run_audit(request: AuditRequest, env: Environment) -> dict:
    """The report dict for ``request``; ``LockBusy``/``LockUnsafe`` propagate to the caller."""
    from .. import __version__

    with RunLock(env.lock_path):
        generated_at = env.clock()
        cutoff = generated_at - request.days * 86400
        run = _Run(request, env, generated_at, cutoff)
        accounts = {name: run.audit_account(name) for name in request.workspaces}
        return audit_report.build_report(
            generated_at=generated_at,
            days=request.days,
            workspaces=request.workspaces,
            accounts=accounts,
            classifier=run.classification.summary(run.model),
            thresholds_digest=thresholds_digest(env.thresholds),
            auto_continue=run.gate.report_field(),
            pm_version=__version__,
        )


class _Run:
    def __init__(self, request: AuditRequest, env: Environment, now: float, cutoff: float) -> None:
        self.request, self.env, self.now, self.cutoff = request, env, now, cutoff
        self.budget = Budget(env.clock, env.limits.budget_seconds)
        self.pacer = Pacer(env.clock, env.sleep, env.limits.min_gap_seconds)
        self.classification = _Classification(env)
        self.model = env.classifier_model()
        self.gate = self._load_gate()

    def _load_gate(self) -> audit_validation.Gate:
        return audit_validation.load_gate(
            self.env.store.directory,
            model=self.model,
            thresholds=self.env.thresholds,
            bench_digest=self.env.bench_digest,
        )

    # -- per account -------------------------------------------------------------------------

    def audit_account(self, name: str) -> dict:
        account = _Account(name)
        report = account.report
        pending = self.env.not_before.active(name, self.env.clock())
        if pending is not None:
            return self._finish(
                audit_report.mark_unavailable(report, "not-before", not_before=pending)
            )
        if self.budget.exhausted():
            return self._finish(audit_report.mark_unavailable(report, "budget-exhausted"))
        try:
            reader = self.env.reader_factory(name)
        except ReaderUnavailable as exc:
            return self._finish(audit_report.mark_unavailable(report, reason_slug(exc.reason)))
        guard = GuardedReader(
            reader,
            account=name,
            pacer=self.pacer,
            budget=self.budget,
            store=self.env.not_before,
            clock=self.env.clock,
        )
        started = self.budget.requests
        try:
            self._audit_with(guard, account)
        finally:
            account.coverage["requests"] = self.budget.requests - started
            guard.close()
        return self._finish(report)

    def _finish(self, report: dict) -> dict:
        return audit_report.finish_account(report)

    def _audit_with(self, guard: GuardedReader, account: _Account) -> None:
        report = account.report
        if not self._verify_identity(guard, account):
            return
        self._list_everything(guard, account)
        self._record_stop(account)
        if account.pages_ok == 0:
            first = next(iter(report["scopes"].values()), None)
            reason = (
                f"http-{account.stopped.status}"
                if account.stopped is not None
                else "budget-exhausted"
                if account.halt == "budget-exhausted"
                else (first["reason"] if first else "") or "unusable"
            )
            audit_report.mark_unavailable(report, reason)
            return
        self._read_conversations(guard, account)

    def _record_stop(self, account: _Account) -> None:
        if account.stopped is not None:
            stop = account.stopped
            account.coverage["stopped"] = {"status": stop.status, "not_before": stop.not_before}
            account.report["not_before"] = stop.not_before
        if account.halt == "budget-exhausted":
            account.coverage["budget_exhausted"] = True

    def _verify_identity(self, guard: GuardedReader, account: _Account) -> bool:
        report = account.report
        try:
            identity = guard.identity()
        except BudgetExhausted:
            audit_report.mark_unavailable(report, "budget-exhausted")
            return False
        except AccountStopped as stop:
            account.stopped = stop
            self._record_stop(account)
            audit_report.mark_unavailable(report, f"http-{stop.status}")
            return False
        except ReaderUnavailable as exc:
            audit_report.mark_unavailable(report, reason_slug(exc.reason))
            return False
        except (ReaderHttpError, ReaderTimeout) as exc:
            account.coverage["errors"] += 1
            audit_report.mark_unavailable(report, _reader_failure(exc))
            return False
        observed = self.env.bindings.observed_account(identity, account.name)
        if self.env.bindings.bound(account.name) is None:
            audit_report.mark_unavailable(report, "account-not-bound")
            return False
        if observed != account.name:
            audit_report.mark_unavailable(report, "account-mismatch")
            return False
        report["observed_account"] = observed
        return True

    # -- listing -----------------------------------------------------------------------------

    def _list_everything(self, guard: GuardedReader, account: _Account) -> None:
        for kind in ("recent", "archived"):
            for origin in ORIGINS:
                scope = ListScope(kind, origin)
                self._scan(
                    account,
                    scope.label,
                    lambda off, scope=scope: guard.list_page(scope, off, PAGE_LIMIT),
                    archived=kind == "archived",
                )
                if account.halt:
                    return
        projects: list[str] = []
        self._scan(
            account,
            "projects",
            lambda off: guard.list_projects(off, PAGE_LIMIT),
            projects=projects,
        )
        for project in projects:
            if account.halt:
                return
            scope = ListScope("project", project_id=project)
            self._scan(
                account,
                scope.label,
                lambda off, scope=scope: guard.list_page(scope, off, PAGE_LIMIT),
                archived=False,
            )

    def _scan(
        self,
        account: _Account,
        label: str,
        fetch: Callable[[int], dict],
        *,
        archived: bool = False,
        projects: list[str] | None = None,
    ) -> None:
        """Page one scope to its end, recording exactly why it stopped."""
        record = {"listed": 0, "pages": 0, "complete": False, "reason": "", "total": None}
        account.report["scopes"][label] = record
        offset = 0
        while True:
            if record["pages"] >= MAX_PAGES_PER_SCOPE:
                record["reason"] = "page-cap"
                return
            try:
                payload = fetch(offset)
            except BudgetExhausted:
                record["reason"], account.halt = "budget-exhausted", "budget-exhausted"
                return
            except AccountStopped as stop:
                record["reason"], account.halt, account.stopped = (
                    f"http-{stop.status}",
                    "stopped",
                    stop,
                )
                return
            except (ReaderHttpError, ReaderTimeout, ReaderUnavailable) as exc:
                missing = projects is not None and getattr(exc, "status", None) == 404
                record["reason"] = "unverified" if missing else _reader_failure(exc)
                account.coverage["errors"] += 1
                return
            try:
                if projects is None:
                    page = audit_native.parse_list_page(payload, archived=archived)
                    items, raw, total, malformed = (
                        page.items,
                        page.raw_count,
                        page.total,
                        page.malformed,
                    )
                else:
                    ppage = audit_native.parse_project_page(payload)
                    items = ppage.project_ids
                    raw, malformed = ppage.raw_count, ppage.malformed
                    total = payload.get("total") if type(payload.get("total")) is int else None
            except audit_native.NativeError:
                record["reason"] = "unusable"
                account.coverage["errors"] += 1
                return
            account.pages_ok += 1
            record["pages"] += 1
            record["listed"] += len(items)
            record["total"] = total if total is not None else record["total"]
            account.coverage["errors"] += malformed
            if projects is None:
                for item in items:
                    account.absorb(item)
            else:
                projects.extend(p for p in items if p not in projects)
            if raw == 0:
                record["complete"] = total is None or offset >= total
                record["reason"] = "" if record["complete"] else "short-listing"
                account.coverage["errors"] += 0 if record["complete"] else 1
                return
            offset += raw
            if (total is not None and offset >= total) or (total is None and raw < PAGE_LIMIT):
                record["complete"] = True
                return
            if projects is None and items and all(i.updated_at < self.cutoff for i in items):
                record["complete"] = True
                return

    # -- reading -----------------------------------------------------------------------------

    def _read_conversations(self, guard: GuardedReader, account: _Account) -> None:
        cov = account.coverage
        cov["listed_total"] = len(account.union)
        window = sorted(
            (c for c in account.union.values() if c.updated_at >= self.cutoff),
            key=lambda c: (-c.updated_at, c.conversation_id),
        )
        cov["in_window"] = len(window)
        for listed in window:
            if self._reuse(account, listed):
                continue
            if account.halt:
                account.skip(account.halt)
                continue
            self._read_one(guard, account, listed)

    def _entry_valid(self, entry: CachedVerdict) -> bool:
        return (
            entry.prompt_version == audit_classifier.PROMPT_VERSION
            and entry.thresholds_digest == thresholds_digest(self.env.thresholds)
            and (not entry.model or entry.model in audit_classifier.profile_members(self.model))
        )

    def _reusable(self, entry: CachedVerdict | None, listed) -> bool:
        if entry is None or not self._entry_valid(entry):
            return False
        verdict = entry.verdict
        if entry.updated_at != listed.updated_at or entry.task_state not in REUSABLE_STATES:
            return False
        count = verdict.get("remaining_steps_count")
        shape = (
            isinstance(verdict.get("reasons"), list)
            and isinstance(verdict.get("evidence_ids"), list)
            and type(count) is int
            and all(isinstance(verdict.get(k), str) for k in _VERDICT_STRINGS)
        )
        return shape and not (self.request.include_text and count > 0)

    def _reuse(self, account: _Account, listed) -> bool:
        entry = self.env.store.get(account.name, listed.conversation_id)
        if not self._reusable(entry, listed):
            return False
        now = self.env.clock()
        self.env.store.put(dataclasses.replace(entry, verified_at=now))
        account.coverage["cached"] += 1
        account.report["conversations"].append(
            self._conversation(listed, entry.verdict, entry.fingerprint, "cache", now)
        )
        return True

    def _read_evidence(self, guard: GuardedReader, account: _Account, listed):
        """The evidence of one conversation, or ``None`` when it could not be read.

        A bare "go" in a cut window has no goal to judge, so while the context is truncated the
        read is repeated with the next bounded turn count (``audit_urls.TURN_STEPS``; the last
        step is a hard cap) through the same guarded reader, so every request keeps the minimum
        gap. A failed expansion read keeps the narrower evidence: its truncated context makes the
        decision unknown, which is honest, while a stop or the budget ends the account's pass.
        """
        cid, turns, evidence = listed.conversation_id, self.request.turns, None
        while turns is not None:
            try:
                payload = guard.read_conversation(cid, turns)
            except BudgetExhausted:
                account.halt = "budget-exhausted"
                account.coverage["budget_exhausted"] = True
                account.skip("budget-exhausted")
                return None
            except AccountStopped as stop:
                account.halt, account.stopped = "stopped", stop
                self._record_stop(account)
                account.skip("stopped")
                return None
            except ReaderError as exc:
                if evidence is not None:
                    return evidence
                account.skip(_reader_failure(exc))
                account.coverage["errors"] += 1
                return None
            try:
                evidence = audit_native.build_evidence(
                    payload,
                    listed=listed,
                    account=account.name,
                    observed_account=account.report["observed_account"],
                    now=self.env.clock(),
                    running=False,
                )
            except audit_native.NativeError:
                if evidence is not None:
                    return evidence
                account.skip("malformed")
                account.coverage["errors"] += 1
                return None
            turns = audit_urls.next_turns(turns) if evidence.context_truncated else None
        return evidence

    def _read_one(self, guard: GuardedReader, account: _Account, listed) -> None:
        cid = listed.conversation_id
        evidence = self._read_evidence(guard, account, listed)
        if evidence is None:
            return
        now = evidence.read_at
        cached = self.env.store.get(account.name, cid)
        verdict = decide(
            evidence,
            lambda: self._classify(evidence, cached),
            now=now,
            thresholds=self.env.thresholds,
        )
        entry = CachedVerdict.from_run(evidence, verdict, self.env.thresholds, verified_at=now)
        self.env.store.put(entry)
        account.coverage["read"] += 1
        wire = verdict.to_mapping(include_text=self.request.include_text)
        account.report["conversations"].append(
            self._conversation(listed, wire, evidence.fingerprint(), "read", now, entry)
        )

    def _classify(self, evidence, cached: CachedVerdict | None):
        """The classifier's answer; a verdict already scored for this exact content is reused."""
        if (
            cached is not None
            and cached.fingerprint == evidence.fingerprint()
            and self._entry_valid(cached)
            and cached.model in audit_classifier.profile_members(self.model)
            and cached.verdict.get("classifier_status") == "ok"
            and cached.verdict.get("scores")
        ):
            return audit_classifier.Classification(
                "ok",
                dict(cached.verdict["scores"]),
                cached.model,
                cached.prompt_version,
                "cached",
                dict(cached.verdict.get("classifier_meta") or {}),
            )
        result = self.classification.call(evidence)
        current = self.env.classifier_model()
        if current != self.model:  # the warm-up found the model: judge under it from now on
            self.model = current
            self.gate = self._load_gate()
        return result

    def _conversation(
        self,
        listed,
        verdict: dict,
        fingerprint: str,
        source: str,
        verified_at: float,
        entry: CachedVerdict | None = None,
    ) -> dict:
        matches = entry is None or entry.fingerprint == fingerprint
        wire = {
            "conversation_id": listed.conversation_id,
            "url": audit_urls.conversation_url(listed.conversation_id),
            "project_id": listed.project_id,
            "archived": listed.archived,
            "updated_at": listed.updated_at,
            "source": source,
            "task_state": verdict["task_state"],
            "response_state": verdict["response_state"],
            "recovery_state": verdict["recovery_state"],
            "reasons": list(verdict["reasons"]),
            "evidence_ids": list(verdict["evidence_ids"]),
            "remaining_steps_count": verdict["remaining_steps_count"],
            "fingerprint": fingerprint,
            "classifier_status": verdict["classifier_status"],
            "verified_at": verified_at,
            "auto_continue": self.gate.auto_continue(fingerprint_matches=matches),
        }
        if self.request.include_text and "remaining_steps" in verdict:
            wire["remaining_steps"] = list(verdict["remaining_steps"])
        return wire


_VERDICT_STRINGS = ("response_state", "task_state", "recovery_state", "classifier_status")
