"""Playwright-backed ``PageReader``: read-only ChatGPT fetches through Guardian's owned browser.

Accessed via: ``audit_collector`` / the ``pm chatgpt audit`` CLI through ``make_reader``.

How it works: attach to the EXISTING authenticated Guardian Chrome over CDP
(``connect_over_cdp``, exactly Guardian's call shape), pick an ALREADY-OPEN ``https://chatgpt.com``
page and run ONE same-origin native ``fetch`` inside it per operation (``page.evaluate``).

Invariants:
- Never navigates, opens/reloads/closes a page, observes, or writes. ``close()`` only stops the
  Playwright driver (as Guardian does); Chrome and its pages are left untouched.
- The bearer/session token never leaves the page evaluation: the JS returns only
  ``{status, retryAfter, body}`` (or a SHA-256 hex for ``identity``); the wrapper projects exactly
  those keys with their types and every failure maps to a fixed safe reason. Nothing is logged.
- Request paths are built only from validated ints and ids (see ``_ENDPOINTS``).
- Every operation runs on a private event loop under a hard ``asyncio.wait_for`` deadline.
- Stdlib-only at import time; Playwright is imported when the first operation connects.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import re
import stat
import sys
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .audit_reader import (
    ListScope,
    ReaderHttpError,
    ReaderIdentity,
    ReaderTimeout,
    ReaderUnavailable,
    mark_native,
)
from .audit_urls import NATIVE_FLAT, conversation_read_path, valid_project_id

# One place for every endpoint shape. OBSERVED against the live service: the conversation read
# (the PLURAL ``/backend-api/conversations/<id>``, built by ``audit_urls.conversation_read_path``;
# the singular full-mapping endpoint is NOT used: it ignores ``num_turns`` and times out on large
# chats), the recent/archived/tpp listings. BEST-EFFORT (not verified live): the project ones; a
# non-2xx there surfaces as ReaderHttpError, which the collector counts as a scope error.
_ENDPOINTS = {
    "list": "/backend-api/conversations?offset={offset}&limit={limit}&order=updated&is_archived={archived}",  # observed
    "list_tpp": "&conversation_origin=tpp",  # observed
    "list_non_tpp": "&exclude_conversation_origin=tpp",  # observed
    "project_chats": "/backend-api/gizmos/{project}/conversations?cursor={offset}",  # best-effort
    "project_ids": "/backend-api/gizmos/snorlax/sidebar?owned_only=true&conversations_per_gizmo=0&limit={limit}&cursor={offset}",  # best-effort
}

ENDPOINTS = {"home": "http://127.0.0.1:9224", "glide.vc": "http://127.0.0.1:9223"}
_ENDPOINT_RE = re.compile(r"http://127\.0\.0\.1:([0-9]{1,5})")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PAGE_ORIGIN = "chatgpt.com"
_SITE_PACKAGES_ENV = "PM_AUDIT_PLAYWRIGHT_SITE_PACKAGES"

DEFAULT_TIMEOUT_MS = 15_000
MAX_TIMEOUT_MS = 60_000
LISTING_CAP = 1 << 20
CONVERSATION_CAP = 8 << 20
CONNECT_TIMEOUT_MS = 10_000
CONNECT_DEADLINE = 15.0
_MAX_OFFSET = 1_000_000
_MAX_LIMIT = 100
_MAX_RETRY_AFTER = 64

_STATUS_NO_TOKEN = 0
_STATUS_TOO_LARGE = -1
_STATUS_ABORTED = -2

# Runs inside the page. `args` = {mode: "data"|"identity", path, timeoutMs, maxBytes}. The token
# lives only in local variables; the result has only the three expected keys (for "identity",
# `body` is the SHA-256 hex of the namespaced account id).
FETCH_JS = r"""
async (args) => {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), args.timeoutMs);
  const out = (status, retryAfter, body) => ({status: status, retryAfter: retryAfter, body: body});
  const readCapped = async (res, max) => {
    if (!res.body) {
      const text = await res.text();
      return text.length > max ? null : text;
    }
    const reader = res.body.getReader();
    const chunks = [];
    let total = 0;
    for (;;) {
      const part = await reader.read();
      if (part.done) break;
      total += part.value.byteLength;
      if (total > max) {
        try { await reader.cancel(); } catch (e) { /* already failing closed */ }
        return null;
      }
      chunks.push(part.value);
    }
    const decoder = new TextDecoder('utf-8');
    let text = '';
    for (const chunk of chunks) text += decoder.decode(chunk, {stream: true});
    return text + decoder.decode();
  };
  try {
    const sres = await fetch('/api/auth/session', {credentials: 'same-origin', signal: ctl.signal});
    if (!sres.ok) return out(sres.status, sres.headers.get('retry-after'), '');
    const session = await sres.json();
    const token = session && typeof session.accessToken === 'string' ? session.accessToken : '';
    if (!token) return out(0, null, '');
    if (args.mode === 'identity') {
      const userId = session.user && typeof session.user.id === 'string' ? session.user.id : '';
      if (!userId) return out(0, null, '');
      const digest = await crypto.subtle.digest(
        'SHA-256', new TextEncoder().encode('pm-chatgpt-audit-v1:' + userId));
      const hex = Array.from(new Uint8Array(digest))
        .map((b) => b.toString(16).padStart(2, '0')).join('');
      return out(200, null, hex);
    }
    const res = await fetch(args.path, {
      headers: {Authorization: 'Bearer ' + token},
      credentials: 'same-origin',
      signal: ctl.signal,
    });
    if (!res.ok) return out(res.status, res.headers.get('retry-after'), '');
    const body = await readCapped(res, args.maxBytes);
    if (body === null) return out(-1, null, '');
    return out(res.status, res.headers.get('retry-after'), body);
  } catch (e) {
    if (e && e.name === 'AbortError') return out(-2, null, '');
    throw new Error('page fetch failed');
  } finally {
    clearTimeout(timer);
  }
}
"""


def endpoint_for(account: str) -> str:
    try:
        return ENDPOINTS[account]
    except KeyError:
        raise ValueError(f"unknown account {account!r}") from None


def _validated_endpoint(account: str, endpoint: str | None) -> str:
    expected = endpoint_for(account)
    if endpoint is None:
        return expected
    match = _ENDPOINT_RE.fullmatch(endpoint) if isinstance(endpoint, str) else None
    if match is None or endpoint != expected:
        raise ValueError(f"endpoint for {account!r} must be exactly {expected}")
    return endpoint


def _int_in(value: object, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in {low}..{high}")
    return value


def conversation_path(conversation_id: str, turns: int) -> str:
    return conversation_read_path(conversation_id, turns)


def list_path(scope: ListScope, offset: int, limit: int) -> str:
    offset = _int_in(offset, 0, _MAX_OFFSET, "offset")
    limit = _int_in(limit, 1, _MAX_LIMIT, "limit")
    if scope.kind == "project":
        if not valid_project_id(scope.project_id):
            raise ValueError("invalid project id")
        return _ENDPOINTS["project_chats"].format(project=scope.project_id, offset=offset)
    path = _ENDPOINTS["list"].format(
        offset=offset, limit=limit, archived="true" if scope.kind == "archived" else "false"
    )
    if scope.origin == "tpp":
        path += _ENDPOINTS["list_tpp"]
    elif scope.origin == "non-tpp":
        path += _ENDPOINTS["list_non_tpp"]
    return path


def projects_path(offset: int, limit: int) -> str:
    return _ENDPOINTS["project_ids"].format(
        limit=_int_in(limit, 1, _MAX_LIMIT, "limit"),
        offset=_int_in(offset, 0, _MAX_OFFSET, "offset"),
    )


def _owned_private(path: str, *, directory: bool) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    kind_ok = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return kind_ok and info.st_uid == os.getuid() and info.st_mode & 0o022 == 0


def import_playwright(site_packages: str | None = None) -> Callable[[], Any]:
    """Return ``async_playwright``; fall back to an explicitly trusted site-packages directory."""
    try:
        return importlib.import_module("playwright.async_api").async_playwright
    except ImportError:
        pass
    directory = site_packages or os.environ.get(_SITE_PACKAGES_ENV)
    if not directory or "\x00" in directory:
        raise ReaderUnavailable("playwright not installed")
    package = os.path.join(directory, "playwright")
    if not (_owned_private(directory, directory=True) and _owned_private(package, directory=True)):
        raise ReaderUnavailable("playwright not installed")
    added = directory not in sys.path
    if added:
        sys.path.append(directory)  # appended: a trusted dir must never shadow pm's own imports
    try:
        return importlib.import_module("playwright.async_api").async_playwright
    except Exception:  # noqa: BLE001 - fixed safe reason; raw text may carry page content
        if added and directory in sys.path:
            sys.path.remove(directory)
        raise ReaderUnavailable("playwright not installed") from None


def _is_chatgpt_page(page: Any) -> bool:
    try:
        if page.is_closed():
            return False
        parts = urlsplit(page.url)
        return parts.scheme == "https" and parts.netloc == _PAGE_ORIGIN
    except Exception:  # noqa: BLE001 - fixed safe reason; raw text may carry page content
        return False


def _strict_object(text: str) -> dict:
    def reject(constant: str) -> Any:
        raise ValueError(constant)

    try:
        value = json.loads(text, parse_constant=reject)
    except (ValueError, RecursionError):
        raise ReaderUnavailable("unusable response") from None
    if not isinstance(value, dict):
        raise ReaderUnavailable("unusable response")
    return value


class BrowserReader:
    def __init__(
        self,
        endpoint: str,
        *,
        driver_factory: Callable[[], Any] | None = None,
        playwright_site_packages: str | None = None,
        request_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        if not isinstance(endpoint, str) or _ENDPOINT_RE.fullmatch(endpoint) is None:
            raise ValueError("endpoint must be http://127.0.0.1:<port>")
        if endpoint not in ENDPOINTS.values():
            raise ValueError("endpoint is not a Guardian endpoint")
        self._endpoint = endpoint
        self._site_packages = playwright_site_packages
        self._driver_factory = driver_factory
        self._timeout_ms = min(_int_in(request_timeout_ms, 1, 10**9, "timeout"), MAX_TIMEOUT_MS)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._driver: Any = None
        self._browser: Any = None

    # -- PageReader -------------------------------------------------------------------------
    def identity(self) -> ReaderIdentity:
        status, retry_after, body = self._run("identity", "", LISTING_CAP)
        self._check_status(status, retry_after)
        if _HEX64.fullmatch(body) is None:
            raise ReaderUnavailable("unusable response")
        return ReaderIdentity(account_hash=body)

    def list_page(self, scope: ListScope, offset: int, limit: int) -> dict:
        return self._get_json(list_path(scope, offset, limit), LISTING_CAP)

    def list_projects(self, offset: int, limit: int) -> dict:
        payload = self._get_json(projects_path(offset, limit), LISTING_CAP)
        items = payload.get("items")
        if not isinstance(items, list):
            raise ReaderUnavailable("unusable response")
        ids: list[dict[str, str]] = []
        for item in items:
            found = _project_id_of(item)
            if found is not None:
                ids.append({"id": found})
        return {"items": ids, "total": None}

    def read_conversation(self, conversation_id: str, turns: int) -> dict:
        payload = self._get_json(conversation_path(conversation_id, turns), CONVERSATION_CAP)
        return mark_native(payload, NATIVE_FLAT)  # the plural endpoint returns the flat shape

    def close(self) -> None:
        driver, self._driver, self._browser = self._driver, None, None
        loop, self._loop = self._loop, None
        try:
            if driver is not None and loop is not None:
                with contextlib.suppress(Exception):  # driver is gone either way; Chrome untouched
                    loop.run_until_complete(asyncio.wait_for(driver.stop(), 10))
        finally:
            if loop is not None:
                loop.close()

    # -- internals --------------------------------------------------------------------------
    def _get_json(self, path: str, cap: int) -> dict:
        status, retry_after, body = self._run("data", path, cap)
        self._check_status(status, retry_after)
        return _strict_object(body)

    @staticmethod
    def _check_status(status: int, retry_after: str | None) -> None:
        if status == _STATUS_NO_TOKEN:
            raise ReaderUnavailable("not logged in")
        if status == _STATUS_TOO_LARGE:
            raise ReaderUnavailable("response too large")
        if status == _STATUS_ABORTED:
            raise ReaderTimeout("request timed out")
        if not 200 <= status <= 299:
            if not 100 <= status <= 599:
                raise ReaderUnavailable("unusable response")
            raise ReaderHttpError(status, retry_after)

    def _run(self, mode: str, path: str, cap: int) -> tuple[int, str | None, str]:
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        return self._loop.run_until_complete(self._operate(mode, path, cap))

    async def _operate(self, mode: str, path: str, cap: int) -> tuple[int, str | None, str]:
        try:
            await asyncio.wait_for(self._ensure_connected(), CONNECT_DEADLINE)
        except TimeoutError:
            raise ReaderUnavailable("cdp unreachable") from None
        page = self._pick_page()
        args = {"mode": mode, "path": path, "timeoutMs": self._timeout_ms, "maxBytes": cap}
        try:
            raw = await asyncio.wait_for(page.evaluate(FETCH_JS, args), self._timeout_ms / 1000 + 5)
        except TimeoutError:
            raise ReaderTimeout("request timed out") from None
        except Exception:  # noqa: BLE001 - fixed safe reason; raw text may carry page content
            raise ReaderUnavailable("unusable response") from None
        return _project_result(raw)

    async def _ensure_connected(self) -> None:
        if self._browser is not None:
            return
        factory = self._driver_factory
        if factory is None:
            factory = import_playwright(self._site_packages)
        try:
            driver = await factory().start()
        except ReaderUnavailable:
            raise
        except Exception:  # noqa: BLE001 - fixed safe reason; raw text may carry page content
            raise ReaderUnavailable("playwright not installed") from None
        try:
            browser = await driver.chromium.connect_over_cdp(
                self._endpoint, timeout=CONNECT_TIMEOUT_MS, no_defaults=True
            )
        except Exception:  # noqa: BLE001 - fixed safe reason; raw text may carry page content
            await _stop_quietly(driver)
            raise ReaderUnavailable("cdp unreachable") from None
        if len(browser.contexts) != 1:
            await _stop_quietly(driver)
            raise ReaderUnavailable("expected exactly one browser context")
        self._driver, self._browser = driver, browser

    def _pick_page(self) -> Any:
        contexts = self._browser.contexts
        if len(contexts) != 1:
            raise ReaderUnavailable("expected exactly one browser context")
        for page in contexts[0].pages:
            if _is_chatgpt_page(page):
                return page
        raise ReaderUnavailable("no open chatgpt.com page")


async def _stop_quietly(driver: Any) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(driver.stop(), 10)


def _project_id_of(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    candidate = item.get("id")
    if candidate is None:
        gizmo = item.get("gizmo")
        inner = gizmo.get("gizmo") if isinstance(gizmo, dict) else None
        candidate = inner.get("id") if isinstance(inner, dict) else None
    return candidate if valid_project_id(candidate) else None


def _project_result(raw: object) -> tuple[int, str | None, str]:
    """Keep only the three expected keys with their types; anything else is dropped/rejected."""
    if not isinstance(raw, dict):
        raise ReaderUnavailable("unusable response")
    status, retry_after, body = raw.get("status"), raw.get("retryAfter"), raw.get("body")
    if isinstance(status, bool) or not isinstance(status, int):
        raise ReaderUnavailable("unusable response")
    if retry_after is not None and not isinstance(retry_after, str):
        raise ReaderUnavailable("unusable response")
    if not isinstance(body, str):
        raise ReaderUnavailable("unusable response")
    return status, None if retry_after is None else retry_after[:_MAX_RETRY_AFTER], body


def make_reader(
    account: str, *, endpoint: str | None = None, playwright_site_packages: str | None = None
) -> BrowserReader:
    return BrowserReader(
        _validated_endpoint(account, endpoint), playwright_site_packages=playwright_site_packages
    )
