"""Chat and project URL canonicalization, and the native read endpoint, for the task audit.

Accessed via: ``audit_native`` (listing identities), ``audit_evidence`` (evidence identity),
``audit_browser`` (the read path) and the CLI (``--workspace``-scoped output). One place decides
what a ChatGPT URL means.

Invariants: only ``https`` on ``chatgpt.com`` or ``chat.openai.com`` is accepted, with no
userinfo, port, percent-encoding, whitespace or control character; anything else raises
``UrlError`` (a foreign host is never normalized into a chatgpt.com URL). A query, fragment and
trailing slash are dropped; the scheme/host and the hex project/conversation UUIDs are
case-insensitive and lowered, the path keywords (``/c/``, ``/g/``, ``/project``) are not. The
canonical conversation URL is exactly Guardian's permanent ``https://chatgpt.com/c/<id>`` form
(see ``chatgpt_monitor.CHAT_URL``); the project id travels separately.

Native read endpoint (verified live: 51 conversations read with it in one audit). The audit reads
the PLURAL ``/backend-api/conversations/<id>?num_turns=N&include_has_versions=true``: it answers
fast, honours ``num_turns`` (the window is bounded, ``page_info.has_previous_page`` says whether
older turns exist) and returns a FLAT chronological ``messages`` list with no ``mapping``. The
older SINGULAR ``/backend-api/conversation/<id>`` returns the full ``mapping`` tree, IGNORES the
turn limit and times out on large chats, so it is only a fallback shape (``NATIVE_MAPPING``) that
``audit_native`` still parses. The reader marks every payload with the format of the endpoint it
called (``NATIVE_FORMAT_KEY``); ``audit_native`` trusts the flat order only under that proof.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_URL = 2048
CANONICAL_HOST = "https://chatgpt.com"

# The two wire shapes of one conversation read (see the module docstring).
NATIVE_FLAT = "flat"
NATIVE_MAPPING = "mapping"
NATIVE_FORMATS = (NATIVE_FLAT, NATIVE_MAPPING)
NATIVE_FORMAT_KEY = "native_format"
# Turn limits: the largest ``--turns`` accepted, and the bounded steps a contextless request
# (a bare "go") is re-read with. The last step is the hard cap of that expansion.
MAX_TURNS = 50
TURN_STEPS = (4, 8, 16)
_READ = "/backend-api/conversations/{id}?num_turns={turns}&include_has_versions=true"
_NATIVE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_PROJECT_ID = re.compile(r"g-p-[0-9a-f]{32}")
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_BASE = re.compile(
    r"(?i:https)://(?i:chatgpt\.com|chat\.openai\.com)(?P<path>/[^?#]*)(?:\?[^#]*)?(?:#.*)?"
)
_PLAIN = re.compile(r"/c/(?P<id>[A-Za-z0-9_-]{1,128})/?")
_PROJECT_SEGMENT = r"/g/(?P<project>(?i:g-p-)[0-9a-fA-F]{32})(?:-[A-Za-z0-9]+)*"
_PROJECT_CHAT = re.compile(
    _PROJECT_SEGMENT + r"/c/(?P<id>[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-"
    r"[0-9a-fA-F]{12})/?"
)
_PROJECT_PAGE = re.compile(_PROJECT_SEGMENT + r"/project/?")


class UrlError(ValueError):
    """The value is not an exact ChatGPT conversation or project URL."""


@dataclass(frozen=True)
class ChatRef:
    conversation_id: str
    project_id: str | None
    url: str


@dataclass(frozen=True)
class ProjectRef:
    project_id: str
    url: str


def valid_project_id(value: object) -> bool:
    return isinstance(value, str) and _PROJECT_ID.fullmatch(value) is not None


def conversation_url(conversation_id: str) -> str:
    return f"{CANONICAL_HOST}/c/{conversation_id}"


def conversation_read_path(conversation_id: str, turns: int) -> str:
    """The same-origin path of one bounded native read (the PLURAL endpoint, ``turns`` clamped).

    Built only from a validated id and a validated int, so nothing else can reach the path.
    """
    if not isinstance(conversation_id, str) or _NATIVE_ID.fullmatch(conversation_id) is None:
        raise ValueError("invalid conversation id")
    if set(conversation_id) == {"."}:
        raise ValueError("invalid conversation id")
    if isinstance(turns, bool) or not isinstance(turns, int) or turns < 1:
        raise ValueError(f"turns must be an integer in 1..{MAX_TURNS}")
    return _READ.format(id=conversation_id, turns=min(turns, MAX_TURNS))


def next_turns(turns: int) -> int | None:
    """The next bounded step above ``turns`` for a contextless request, or ``None`` at the cap."""
    return next((step for step in TURN_STEPS if step > turns), None)


def _path_of(raw: object) -> str:
    if not isinstance(raw, str) or not raw or len(raw) > MAX_URL:
        raise UrlError("URL must be a non-empty string of at most 2048 characters")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        raise UrlError("URL must not contain whitespace or control characters")
    match = _BASE.fullmatch(raw)
    if match is None:
        raise UrlError("URL must be https on chatgpt.com or chat.openai.com with no userinfo/port")
    path = match["path"]
    if "%" in path or "//" in path or "/./" in path or "/../" in path:
        raise UrlError("URL path must be exact (no encoding, empty or dot segments)")
    return path


def parse_chat_url(raw: object) -> ChatRef:
    """The canonical reference for one conversation URL, or ``UrlError``."""
    path = _path_of(raw)
    plain = _PLAIN.fullmatch(path)
    if plain is not None:
        cid = plain["id"]
        if _UUID.fullmatch(cid.lower()):
            cid = cid.lower()
        return ChatRef(cid, None, conversation_url(cid))
    project = _PROJECT_CHAT.fullmatch(path)
    if project is None:
        raise UrlError("not an exact conversation URL")
    cid = project["id"].lower()
    return ChatRef(cid, project["project"].lower(), conversation_url(cid))


def parse_project_url(raw: object) -> ProjectRef:
    """The canonical reference for one project page URL, or ``UrlError``."""
    page = _PROJECT_PAGE.fullmatch(_path_of(raw))
    if page is None:
        raise UrlError("not an exact project URL")
    pid = page["project"].lower()
    return ProjectRef(pid, f"{CANONICAL_HOST}/g/{pid}/project")
