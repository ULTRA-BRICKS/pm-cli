"""Private cache of audit verdicts, the last-run report and notification dedup keys.

Accessed via: ``audit_collector`` (writes after every read, reuses an unchanged conversation),
``audit_notify`` (delivery log), the CLI (last report) and the poller (``trusted``).

Invariants: state lives under ``<pm store>/chatgpt-audit`` as 0600 files in a 0700 directory,
never through a symlink, written atomically and fsynced (``audit_files``), and bounded: the verdict
cache keeps at most ``MAX_ENTRIES`` conversations (least recently verified evicted) and the report
at most ``MAX_REPORT_BYTES``. An entry holds ids, SHA-256 digests, the verdict and the versions it
was produced under; never conversation text, credentials or model scores' inputs.

A cached verdict is TRUSTED only through ``trusted``: it must be fresh, produced by the current
prompt version and (when the model decided anything) the current model, judged under the current
threshold digest, and describe the same latest request/final fingerprint the caller observes. A
classifier outage therefore never authorizes anything: an entry made without the model carries an
empty ``model`` and a non-``done`` task state, and a model change voids every model-dependent entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path

from ..store import resolve_store_dir
from . import audit_files
from .audit_classifier import profile_members
from .audit_decision import FUTURE_SKEW_SECONDS, Thresholds, Verdict, thresholds_digest
from .audit_evidence import ConversationEvidence

SCHEMA = 2  # 2: entries carry ``turn_ids`` (the latest turn's native identifiers)
STORE_DIR = "chatgpt-audit"
VERDICTS_FILE = "verdicts.json"
REPORT_FILE = "last-report.json"
NOTIFY_FILE = "notifications.jsonl"
NOTIFY_KINDS = frozenset({"delivery_intent", "sent", "delivery_uncertain", "delivery_failed"})
MAX_ENTRIES = 2000
MAX_REPORT_BYTES = 512 * 1024
MAX_VERDICTS_BYTES = 8 * 1024 * 1024


def default_dir() -> Path:
    return resolve_store_dir() / STORE_DIR


def _key(account: str, conversation_id: str) -> str:
    return f"{account}/{conversation_id}"


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _turn_ids(e: ConversationEvidence) -> tuple[str, ...]:
    """Every native identifier of the latest turn, so a poller that only sees Guardian's rendered
    turn can prove it is still the turn this verdict was made for."""
    user, final = e.last_user, e.last_final
    candidates = (
        user.id if user else None,
        user.turn_uuid if user else None,
        final.id if final else None,
        final.turn_uuid if final else None,
    )
    return tuple(dict.fromkeys(c for c in candidates if isinstance(c, str) and c))


@dataclass(frozen=True)
class CachedVerdict:
    account: str
    conversation_id: str
    fingerprint: str
    evidence_digest: str
    request_id: str | None
    final_id: str | None
    turn_ids: tuple[str, ...]  # native ids of the latest turn (user/final message + turn uuid)
    last_message_id: str
    updated_at: float | None
    verified_at: float
    prompt_version: str
    model: str
    thresholds_digest: str
    verdict: dict

    @classmethod
    def from_run(
        cls,
        e: ConversationEvidence,
        verdict: Verdict,
        thresholds: Thresholds,
        *,
        verified_at: float | None = None,
    ) -> CachedVerdict:
        from .audit_classifier import PROMPT_VERSION

        return cls(
            account=e.account,
            conversation_id=e.conversation_id,
            fingerprint=e.fingerprint(),
            evidence_digest=e.digest(),
            request_id=e.request_id,
            final_id=e.final_id,
            turn_ids=_turn_ids(e),
            last_message_id=e.messages[-1].id,
            updated_at=e.updated_at,
            verified_at=e.verified_at if verified_at is None else verified_at,
            prompt_version=PROMPT_VERSION,
            model=verdict.model,
            thresholds_digest=thresholds_digest(thresholds),
            verdict=verdict.to_mapping(),
        )

    def to_mapping(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_mapping(cls, data: object) -> CachedVerdict | None:
        """The entry, or ``None`` when ``data`` is not exactly a well-formed entry."""
        names = {f.name for f in fields(cls)}
        if not isinstance(data, dict) or set(data) != names:
            return None
        strings = ("account", "conversation_id", "fingerprint", "evidence_digest")
        strings += ("last_message_id", "prompt_version", "model", "thresholds_digest")
        if not all(isinstance(data[k], str) for k in strings):
            return None
        optional = (data["request_id"], data["final_id"])
        if not all(v is None or isinstance(v, str) for v in optional):
            return None
        if not _finite(data["verified_at"]):
            return None
        if data["updated_at"] is not None and not _finite(data["updated_at"]):
            return None
        verdict = data["verdict"]
        if not isinstance(verdict, dict) or not isinstance(verdict.get("task_state"), str):
            return None
        ids = data["turn_ids"]
        if not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) and i for i in ids):
            return None
        return cls(**{**data, "turn_ids": tuple(ids)})

    @property
    def task_state(self) -> str:
        return self.verdict["task_state"]


class AuditStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_dir()

    @property
    def verdicts_path(self) -> Path:
        return self.directory / VERDICTS_FILE

    def _entries(self) -> dict[str, dict]:
        data = audit_files.read_json(self.verdicts_path, max_bytes=MAX_VERDICTS_BYTES)
        if not isinstance(data, dict) or data.get("v") != SCHEMA:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}

    def get(self, account: str, conversation_id: str) -> CachedVerdict | None:
        return CachedVerdict.from_mapping(self._entries().get(_key(account, conversation_id)))

    def put(self, entry: CachedVerdict) -> None:
        with audit_files.file_lock(self.verdicts_path):
            entries = self._entries()
            entries[_key(entry.account, entry.conversation_id)] = entry.to_mapping()
            if len(entries) > MAX_ENTRIES:
                order = sorted(
                    entries,
                    key=lambda k: (
                        entries[k].get("verified_at")
                        if _finite(entries[k].get("verified_at"))
                        else -math.inf
                    ),
                )
                for stale in order[: len(entries) - MAX_ENTRIES]:
                    del entries[stale]
            audit_files.write_json(
                self.verdicts_path, {"v": SCHEMA, "entries": entries}, max_bytes=MAX_VERDICTS_BYTES
            )

    def trusted(
        self,
        account: str,
        conversation_id: str,
        *,
        now: float,
        prompt_version: str,
        model: str,
        thresholds_digest: str,
        max_age: float,
        fingerprint: str | None,
    ) -> CachedVerdict | None:
        """The cached verdict when every current condition still holds, else ``None``.

        ``fingerprint`` is ``ConversationEvidence.fingerprint()`` of what the caller observes now;
        ``None`` is for a caller that cannot read the conversation (the poller) and must add its own
        activity cross-check on top of the freshness and version checks done here.
        """
        entry = self.get(account, conversation_id)
        if entry is None:
            return None
        age = now - entry.verified_at
        if age > max_age or age < -FUTURE_SKEW_SECONDS:
            return None
        if entry.prompt_version != prompt_version or entry.thresholds_digest != thresholds_digest:
            return None
        if entry.model and entry.model not in profile_members(model):
            return None
        if fingerprint is not None and entry.fingerprint != fingerprint:
            return None
        return entry

    def put_report(self, report: dict) -> None:
        audit_files.write_json(self.directory / REPORT_FILE, report, max_bytes=MAX_REPORT_BYTES)

    def last_report(self) -> dict | None:
        data = audit_files.read_json(self.directory / REPORT_FILE, max_bytes=MAX_REPORT_BYTES)
        return data if isinstance(data, dict) else None

    def notification_log(self) -> audit_files.AppendLog:
        return audit_files.AppendLog(self.directory / NOTIFY_FILE, kinds=NOTIFY_KINDS)
