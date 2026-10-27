"""Fakes and builders for the audit collector/CLI tests: reader, classifier, clock, environment.

Everything is synthetic. The fake reader implements exactly the ``PageReader`` methods and fails
the test on ANY other attribute access, so a mutation call (unarchive, resume, send...) cannot slip
through unnoticed. Fake conversations carry SENTINEL texts so tests can prove no text escapes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control.audit_bindings import AccountBindings
from pmlib.chatgpt_control.audit_collector import AuditRequest, Environment
from pmlib.chatgpt_control.audit_guard import Limits, NotBeforeStore
from pmlib.chatgpt_control.audit_reader import ListScope, ReaderIdentity
from pmlib.chatgpt_control.audit_store import AuditStore
from tests.audit_support import NOW, laya

HOME_HASH = "a" * 64
GLIDE_HASH = "b" * 64
BENCH = "c" * 64
MODEL = "laya-0.23.0"
SENTINEL_REQUEST = "SENTINEL-REQUEST-TEXT"
SENTINEL_FINAL = "SENTINEL-FINAL-TEXT"
ALLOWED_CALLS = {"identity", "list_page", "list_projects", "read_conversation", "close"}


def project_id(n: int) -> str:
    return f"g-p-{n:032x}"


def listing_item(cid: str, age: float, *, archived: bool = False, project: str | None = None):
    return {"id": cid, "update_time": NOW - age, "is_archived": archived, "gizmo_id": project}


def conversation_payload(
    cid: str,
    *,
    age: float = 600,
    answered: bool = True,
    in_progress: bool = False,
    answer: str = SENTINEL_FINAL,
) -> dict:
    """A native payload: one user request and (optionally) one assistant final answer."""

    def node(nid, parent, role, text, at, **extra):
        message = {
            "id": nid,
            "author": {"role": role},
            "create_time": NOW - at,
            "content": {"content_type": "text", "parts": [text]},
            "status": extra.pop("status", "finished_successfully"),
            "end_turn": extra.pop("end_turn", None),
            "recipient": "all",
            "metadata": {},
        }
        return {"id": nid, "message": message, "parent": parent, "children": []}

    user = node(f"u-{cid}", "root", "user", SENTINEL_REQUEST, age + 100)
    nodes = [user]
    if answered:
        status = "in_progress" if in_progress else "finished_successfully"
        nodes.append(
            node(f"a-{cid}", user["id"], "assistant", answer, age, end_turn=True, status=status)
        )
    mapping = {n["id"]: n for n in nodes}
    mapping["root"] = {"id": "root", "message": None, "parent": None, "children": []}
    return {
        "conversation_id": cid,
        "title": "t",
        "update_time": NOW - age,
        "current_node": nodes[-1]["id"],
        "mapping": mapping,
    }


class Clock:
    """Deterministic time: ``sleep`` advances it, every sleep is recorded, nothing really waits."""

    def __init__(self, start: float = NOW) -> None:
        self.t = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


class FakeReader:
    """Serves listing scopes (label -> items) and conversation payloads; records every call."""

    def __init__(
        self,
        account_hash: str = HOME_HASH,
        *,
        clock: Clock | None = None,
        scopes: dict[str, list[dict]] | None = None,
        projects: list[str] | None = None,
        payloads: dict[str, dict] | None = None,
        raises: dict[tuple, Exception] | None = None,
        report_total: bool = True,
    ) -> None:
        self.account_hash = account_hash
        self.clock = clock
        self.scopes = scopes or {}
        self.projects = projects or []
        self.payloads = payloads or {}
        self.raises = raises or {}
        self.report_total = report_total
        self.calls: list[tuple] = []
        self.times: list[float] = []
        self.closed = False

    def _enter(self, name: str, *key) -> None:
        self.calls.append((name, *key))
        self.times.append(self.clock() if self.clock else 0.0)
        error = self.raises.get((name, *key[:1]) if key else (name,))
        if error is not None:
            raise error

    def _page(self, items: list, offset: int, limit: int) -> dict:
        page = {"items": items[offset : offset + limit]}
        page["total"] = len(items) if self.report_total else None
        return page

    def identity(self):
        self._enter("identity")
        return ReaderIdentity(self.account_hash)

    def list_page(self, scope: ListScope, offset: int, limit: int) -> dict:
        self._enter("list_page", scope.label, offset, limit)
        return self._page(self.scopes.get(scope.label, []), offset, limit)

    def list_projects(self, offset: int, limit: int) -> dict:
        self._enter("list_projects", offset, limit)
        return self._page([{"id": p} for p in self.projects], offset, limit)

    def read_conversation(self, conversation_id: str, turns: int) -> dict:
        self._enter("read_conversation", conversation_id, turns)
        return self.payloads.get(conversation_id) or conversation_payload(conversation_id)

    def close(self) -> None:
        self.closed = True

    def __getattr__(self, name: str):
        raise AssertionError(f"the audit called a method it must never call: {name}")

    def called(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]

    def requests(self) -> int:
        return len(self.calls)


class FakeClassifier:
    def __init__(self, result=None) -> None:
        self.result = result if result is not None else laya(model=MODEL)
        self.seen: list[str] = []

    def classify(self, evidence):
        self.seen.append(evidence.conversation_id)
        return self.result(evidence) if callable(self.result) else self.result


def unavailable_classification() -> clf.Classification:
    return clf.unavailable("socket-missing")


def invalid_classification() -> clf.Classification:
    return clf.invalid("bad-reply", MODEL)


@dataclass
class Rig:
    env: Environment
    clock: Clock
    store: AuditStore
    readers: dict[str, FakeReader]
    classifier: FakeClassifier
    directory: Path
    created: list[str] = field(default_factory=list)

    def run(self, **request):
        from pmlib.chatgpt_control.audit_collector import run_audit

        workspaces = request.pop("workspaces", tuple(self.readers))
        return run_audit(AuditRequest(tuple(workspaces), **request), self.env)


def rig(
    tmp_path: Path,
    readers: dict[str, FakeReader],
    *,
    classifier: FakeClassifier | None = None,
    model=MODEL,
    bound: tuple[str, ...] = ("home", "glide.vc"),
    limits: Limits | None = None,
    factory=None,
    clock: Clock | None = None,
    thresholds=None,
) -> Rig:
    directory = tmp_path / "audit"
    clock = clock or Clock()
    for reader in readers.values():
        reader.clock = reader.clock or clock
    store = AuditStore(directory)
    bindings = AccountBindings(directory / "bindings.json")
    hashes = {"home": HOME_HASH, "glide.vc": GLIDE_HASH}
    for account in bound:
        bindings.record(account, ReaderIdentity(hashes[account]))
    classifier = classifier or FakeClassifier()
    extra = {} if thresholds is None else {"thresholds": thresholds}
    env = Environment(
        reader_factory=factory or (lambda account: readers[account]),
        classifier=classifier,
        classifier_model=model if callable(model) else (lambda: model),
        store=store,
        not_before=NotBeforeStore(directory / "not_before.json"),
        bindings=bindings,
        lock_path=directory / "run.lock",
        clock=clock,
        sleep=clock.sleep,
        limits=limits or Limits(),
        bench_digest=BENCH,
        **extra,
    )
    return Rig(env, clock, store, readers, classifier, directory)


def simple_account(
    *,
    n: int = 3,
    account_hash: str = HOME_HASH,
    prefix: str = "c",
    clock: Clock | None = None,
    **kw,
) -> FakeReader:
    """``n`` answered conversations in ``recent`` (newest first), all inside the window."""
    ids = [f"{prefix}{i}" for i in range(n)]
    items = [listing_item(cid, 600 + 100 * i) for i, cid in enumerate(ids)]
    payloads = {cid: conversation_payload(cid, age=600 + 100 * i) for i, cid in enumerate(ids)}
    return FakeReader(account_hash, clock=clock, scopes={"recent": items}, payloads=payloads, **kw)
