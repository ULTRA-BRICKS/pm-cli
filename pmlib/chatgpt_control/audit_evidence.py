"""Typed, validating evidence for the task-completion audit of one ChatGPT conversation.

Accessed via: ``audit_native`` (builds it from a native read), ``audit_decision`` and
``audit_classifier`` (consume it), ``audit_store`` (caches it) and ``audit_bench`` (fixtures).

Invariants: construction IS validation. Every field is type-exact (``bool`` is never a number,
timestamps are finite), identifiers and the canonical URL are checked against ``audit_urls``,
and any malformed value raises ``EvidenceError`` so no partial evidence can exist. The evidence
carries ordered message REFERENCES (ids, roles, times, structural markers - no text) plus the
bounded text of ONLY the last substantive user request and the last final assistant answer, with
truncation flags. There is no field for hidden reasoning, tool output, cookies, bearer tokens or
any other role's content, and ``from_mapping`` refuses any key that names one. ``read_at`` is when
the conversation was read; ``verified_at`` is when it was last confirmed current (a cache hit
confirmed by an unchanged ``updated_at``), and freshness is judged on ``verified_at``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, fields

from . import audit_urls

SCHEMA_VERSION = 1
MAX_MESSAGES = 400
MAX_TEXT = 12000
MAX_ID = 128
MAX_ARTIFACTS = 1000
FINISHED = "finished_successfully"  # the status of a message that completed normally
UNFINISHED_CUES = ("", "continuation-announced", "next-step-promised", "not-yet-done", "plan-only")
DOUBT_CUES = ("", "plan-like", "deployment-unevidenced")
ROLES = ("user", "assistant")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_STATUS = re.compile(r"[a-z][a-z0-9_]{0,39}")
_FORBIDDEN_MARKERS = (
    "cookie",
    "token",
    "password",
    "secret",
    "bearer",
    "authorization",
    "reasoning",
    "thought",
    "credential",
    "apikey",
)
# English and Russian acknowledgements that carry no task ("go", "continue", "done"); the
# Russian words are written as escapes so the source stays ASCII.
_TRIVIAL = frozenset(
    {
        "go",
        "go on",
        "continue",
        "proceed",
        "next",
        "ok",
        "okay",
        "yes",
        "y",
        "sure",
        "done",
        "thanks",
        "thank you",
        "\u0433\u043e",
        "\u0433\u043e\u0442\u043e\u0432\u043e",
        "\u0434\u0430",
        "\u043e\u043a",
        "\u043b\u0430\u0434\u043d\u043e",
        "\u0434\u0430\u043b\u044c\u0448\u0435",
        "\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0430\u0439",
        "\u0441\u043f\u0430\u0441\u0438\u0431\u043e",
    }
)


class EvidenceError(ValueError):
    """The evidence is malformed; nothing was constructed."""


def _fail(message: str) -> None:
    raise EvidenceError(message)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _flag(name: str, value: object) -> None:
    if type(value) is not bool:
        _fail(f"{name} must be a boolean")


def _optional_time(name: str, value: object) -> None:
    if value is not None and not _finite(value):
        _fail(f"{name} must be a finite number or null")


def _required_time(name: str, value: object) -> None:
    if not _finite(value):
        _fail(f"{name} must be a finite number")


def _identifier(name: str, value: object) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        _fail(f"{name} must be 1-128 characters of [A-Za-z0-9_.:-]")


def bounded_text(raw: object, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """Untrusted text with control/format characters removed (newline and tab kept), bounded."""
    if not isinstance(raw, str):
        return "", False
    clean = "".join(c for c in raw if c in "\n\t" or unicodedata.category(c)[0] != "C")
    return clean[:limit], len(clean) > limit


def _check_text(name: str, value: object) -> None:
    if not isinstance(value, str) or len(value) > MAX_TEXT:
        _fail(f"{name} must be a string of at most {MAX_TEXT} characters")
    if any(c not in "\n\t" and unicodedata.category(c)[0] == "C" for c in value):
        _fail(f"{name} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(f"{name} must be valid unicode text")


def is_trivial_ack(text: object) -> bool:
    """True for a bare acknowledgement such as "go" or "continue" (no task content)."""
    if not isinstance(text, str):
        return False
    words = "".join(c if c.isalnum() or c.isspace() else " " for c in text.lower()).split()
    return bool(words) and " ".join(words) in _TRIVIAL


@dataclass(frozen=True)
class MessageRef:
    """One visible user/assistant message, by reference: ids, order and structure, no text."""

    id: str
    role: str
    created_at: float | None
    turn_uuid: str
    status: str
    end_turn: bool | None
    final: bool
    tool_success: bool
    recap: bool
    image_artifacts: int = 0  # generated/attached images on the message: a count, never content
    file_artifacts: int = 0

    def __post_init__(self) -> None:
        _identifier("message id", self.id)
        if self.role not in ROLES:
            _fail("message role must be user or assistant")
        _optional_time("message created_at", self.created_at)
        if not isinstance(self.turn_uuid, str) or (
            self.turn_uuid and _ID.fullmatch(self.turn_uuid) is None
        ):
            _fail("turn_uuid must be empty or 1-128 characters of [A-Za-z0-9_.:-]")
        if not isinstance(self.status, str) or _STATUS.fullmatch(self.status) is None:
            _fail("message status must be a short lower-case token")
        if self.end_turn is not None:
            _flag("end_turn", self.end_turn)
        for name in ("final", "tool_success", "recap"):
            _flag(name, getattr(self, name))
        for name in ("image_artifacts", "file_artifacts"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_ARTIFACTS:
                _fail(f"{name} must be an integer from 0 to {MAX_ARTIFACTS}")

    @property
    def artifacts(self) -> int:
        return self.image_artifacts + self.file_artifacts

    @property
    def in_progress(self) -> bool:
        return self.status == "in_progress"


MESSAGE_KEYS = frozenset(f.name for f in fields(MessageRef))


@dataclass(frozen=True)
class ConversationEvidence:
    account: str
    observed_account: str | None
    conversation_id: str
    url: str
    project_id: str | None
    updated_at: float | None
    archived: bool
    messages: tuple[MessageRef, ...]
    request_id: str | None
    request_text: str
    request_truncated: bool
    final_id: str | None
    final_text: str
    final_truncated: bool
    window_cut: bool
    context_truncated: bool
    read_at: float
    verified_at: float
    pending_approval: bool
    permission_expired: bool
    human_input_required: bool
    progress_indicator: bool
    continuation_announced: bool
    schema_version: int = SCHEMA_VERSION
    login_required: bool = False
    stop_intent: bool = False  # the latest substantive user request says stop / write a handoff
    end_turn_without_reply: bool = False  # the assistant ended its turn after a step with no reply
    unfinished_cue: str = ""  # audit_signals: announced or admitted unfinished work
    doubt_cue: str = ""  # audit_signals: a reason ``done`` cannot be claimed

    def __post_init__(self) -> None:
        if not isinstance(self.account, str) or _ACCOUNT.fullmatch(self.account) is None:
            _fail("account must be a short lower-case account id")
        if self.observed_account is not None and (
            not isinstance(self.observed_account, str)
            or _ACCOUNT.fullmatch(self.observed_account) is None
        ):
            _fail("observed_account must be a short lower-case account id or null")
        _identifier("conversation_id", self.conversation_id)
        if self.url != audit_urls.conversation_url(self.conversation_id):
            _fail("url must be the canonical conversation URL of conversation_id")
        if self.project_id is not None and not audit_urls.valid_project_id(self.project_id):
            _fail("project_id must be g-p-<32 hex> or null")
        _optional_time("updated_at", self.updated_at)
        _required_time("read_at", self.read_at)
        _required_time("verified_at", self.verified_at)
        for name in (
            "archived",
            "request_truncated",
            "final_truncated",
            "window_cut",
            "context_truncated",
            "pending_approval",
            "permission_expired",
            "human_input_required",
            "progress_indicator",
            "continuation_announced",
            "login_required",
            "stop_intent",
            "end_turn_without_reply",
        ):
            _flag(name, getattr(self, name))
        if self.unfinished_cue not in UNFINISHED_CUES:
            _fail("unfinished_cue is not a known cue")
        if self.doubt_cue not in DOUBT_CUES:
            _fail("doubt_cue is not a known cue")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            _fail("unsupported evidence schema_version")
        self._check_messages()
        _check_text("request_text", self.request_text)
        _check_text("final_text", self.final_text)

    def _check_messages(self) -> None:
        if not isinstance(self.messages, tuple) or not self.messages:
            _fail("messages must be a non-empty tuple")
        if len(self.messages) > MAX_MESSAGES:
            _fail(f"at most {MAX_MESSAGES} messages are accepted")
        if not all(isinstance(m, MessageRef) for m in self.messages):
            _fail("messages must be MessageRef values")
        by_id = {m.id: m for m in self.messages}
        if len(by_id) != len(self.messages):
            _fail("message ids must be unique")
        self._check_reference("request_id", self.request_id, "user", by_id, self.request_text)
        self._check_reference("final_id", self.final_id, "assistant", by_id, self.final_text)

    @staticmethod
    def _check_reference(
        name: str, ref: str | None, role: str, by_id: dict[str, MessageRef], text: object
    ) -> None:
        if ref is None:
            if text:
                _fail(f"{name} is absent but its text is not empty")
            return
        _identifier(name, ref)
        message = by_id.get(ref)
        if message is None or message.role != role:
            _fail(f"{name} must reference a {role} message in messages")

    @property
    def last_user(self) -> MessageRef | None:
        return next((m for m in reversed(self.messages) if m.role == "user"), None)

    @property
    def last_final(self) -> MessageRef | None:
        """The message ``final_id`` names (the last visible final assistant answer)."""
        return next((m for m in self.messages if m.id == self.final_id), None)

    def to_mapping(self) -> dict:
        wire = {f.name: getattr(self, f.name) for f in fields(self)}
        wire["messages"] = [{k: getattr(m, k) for k in MESSAGE_KEYS} for m in self.messages]
        return wire

    @classmethod
    def from_mapping(cls, data: object) -> ConversationEvidence:
        """Strict inverse of ``to_mapping``: exact keys, no credential/reasoning carriers."""
        if not isinstance(data, dict):
            _fail("evidence must be a JSON object")
        _reject_forbidden(data)
        names = {f.name for f in fields(cls)}
        unknown = set(data) - names
        if unknown:
            _fail("unknown evidence key " + _shown(min(unknown)))
        missing = names - {"schema_version"} - set(data)
        if missing:
            _fail("missing evidence key " + _shown(min(missing)))
        messages = data["messages"]
        if not isinstance(messages, list):
            _fail("messages must be a list")
        refs = []
        for raw in messages:
            if not isinstance(raw, dict):
                _fail("each message must be an object")
            _reject_forbidden(raw)
            if set(raw) != MESSAGE_KEYS:
                _fail("message keys must be exactly " + ", ".join(sorted(MESSAGE_KEYS)))
            refs.append(MessageRef(**raw))
        values = {k: v for k, v in data.items() if k != "messages"}
        return cls(messages=tuple(refs), **values)

    def digest(self) -> str:
        """Content digest independent of the (volatile) read and verification times."""
        wire = self.to_mapping()
        del wire["read_at"], wire["verified_at"]
        return hashlib.sha256(
            json.dumps(wire, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()

    def fingerprint(self) -> str:
        """Identity of what a verdict judged: the latest request, final and last message.

        Hashes ids, structure and SHA-256 digests of the two quoted texts (never the text itself),
        and ignores every clock, so a cached verdict is current exactly while this value is equal.
        """
        last = self.messages[-1]
        wire = {
            "account": self.account,
            "conversation_id": self.conversation_id,
            "request_id": self.request_id,
            "request_sha": hashlib.sha256(self.request_text.encode()).hexdigest(),
            "final_id": self.final_id,
            "final_sha": hashlib.sha256(self.final_text.encode()).hexdigest(),
            "last_message_id": last.id,
            "last_message_status": last.status,
            "last_message_artifacts": last.artifacts,
            "message_count": len(self.messages),
            "schema_version": self.schema_version,
        }
        return hashlib.sha256(
            json.dumps(wire, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def _shown(key: object) -> str:
    return repr("".join(c for c in str(key)[:40] if unicodedata.category(c)[0] != "C"))


def _reject_forbidden(obj: dict) -> None:
    for key in obj:
        lowered = "".join(c for c in str(key).lower() if c.isalnum())
        if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
            _fail("forbidden evidence key " + _shown(key))
