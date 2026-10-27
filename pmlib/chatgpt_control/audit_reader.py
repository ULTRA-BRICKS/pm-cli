"""The read seam of the audit: a small typed ``PageReader`` protocol and its error vocabulary.

Accessed via: ``audit_guard`` (wraps any reader), ``audit_collector`` (drives one) and
``audit_browser`` (the Playwright-backed implementation). Tests drive the collector through a
fake reader that implements the same methods.

A reader only READS through the owned, already-authenticated browser: it never navigates, opens a
tab, observes, or writes. ``identity`` proves which account the browser is logged into (an opaque
hash, never an email or token), ``list_page`` returns one page of conversation metadata for a
listing scope (``recent``/``archived``, each optionally narrowed to the ``tpp`` origin or its
complement, or one ``project``), ``list_projects`` pages the project ids, and ``read_conversation``
returns one native conversation payload for at most ``turns`` turns. Failures
are typed: ``ReaderUnavailable`` (no browser / not logged in / unusable response),
``ReaderHttpError`` (a non-2xx status, with the raw ``Retry-After`` header) and ``ReaderTimeout``.

A reader knows which endpoint it called, so it (and only it) stamps the payload with the wire
format that endpoint returns (``mark_native``): ``flat`` for the plural ``messages`` endpoint, or
``mapping`` for the legacy full-mapping one. A server-supplied marker is always overwritten.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .audit_urls import NATIVE_FORMAT_KEY, NATIVE_FORMATS

SCOPES = ("recent", "project", "archived")
ORIGINS = ("any", "tpp", "non-tpp")


class ReaderError(Exception):
    """Base of every reader failure; ``str()`` is safe to show (it never carries secrets)."""


class ReaderUnavailable(ReaderError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ReaderHttpError(ReaderError):
    def __init__(self, status: int, retry_after: str | None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.retry_after = retry_after


class ReaderTimeout(ReaderError):
    """One request exceeded its per-request timeout."""


def mark_native(payload: dict, native_format: str) -> dict:
    """A copy of ``payload`` stamped with the wire format of the endpoint that produced it."""
    if native_format not in NATIVE_FORMATS:
        raise ValueError(f"unknown native format {native_format!r}")
    return {**payload, NATIVE_FORMAT_KEY: native_format}


@dataclass(frozen=True)
class ReaderIdentity:
    """Opaque proof of the logged-in account: a hash of the account id, never the id itself."""

    account_hash: str


@dataclass(frozen=True)
class ListScope:
    """One conversation listing: ``recent`` or ``archived`` (optionally one origin variant), or the
    chats of one ``project``. ``tpp`` asks only for ``conversation_origin=tpp`` conversations,
    ``non-tpp`` for ``exclude_conversation_origin=tpp``, ``any`` for the unfiltered listing."""

    kind: str  # recent | project | archived
    origin: str = "any"  # any | tpp | non-tpp
    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in SCOPES:
            raise ValueError(f"unknown listing scope {self.kind!r}")
        if self.origin not in ORIGINS:
            raise ValueError(f"unknown listing origin {self.origin!r}")
        if (self.kind == "project") != (self.project_id is not None):
            raise ValueError("a project scope needs a project_id and only a project scope has one")
        if self.kind == "project" and self.origin != "any":
            raise ValueError("a project listing has no origin variants")

    @property
    def label(self) -> str:
        """Stable coverage key: ``recent``, ``recent:tpp``, ``archived:non-tpp``, ``project:<id>``."""
        if self.kind == "project":
            return f"project:{self.project_id}"
        return self.kind if self.origin == "any" else f"{self.kind}:{self.origin}"


class PageReader(Protocol):
    def identity(self) -> ReaderIdentity: ...

    def list_page(self, scope: ListScope, offset: int, limit: int) -> dict: ...

    def list_projects(self, offset: int, limit: int) -> dict: ...

    def read_conversation(self, conversation_id: str, turns: int) -> dict: ...

    def close(self) -> None: ...
