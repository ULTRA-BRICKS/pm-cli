"""Behavior tests for the Playwright-backed audit reader; every browser is a fake."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys

import pytest

from pmlib.chatgpt_control import audit_browser as ab
from pmlib.chatgpt_control.audit_reader import (
    ListScope,
    ReaderHttpError,
    ReaderIdentity,
    ReaderTimeout,
    ReaderUnavailable,
)

CANARY = "SENTINEL-canary-do-not-leak"
PROJECT = "g-p-" + "ab" * 16
HEX = "a" * 64


class FakePage:
    def __init__(self, url="https://chatgpt.com/c/x", result=None, closed=False):
        self.url = url
        self.closed = closed
        self.result = result
        self.calls = []

    def is_closed(self):
        return self.closed

    async def evaluate(self, js, args):
        self.calls.append((js, args))
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result(args)
        return self.result

    async def goto(self, *a, **k):
        raise AssertionError("goto is forbidden")

    async def reload(self, *a, **k):
        raise AssertionError("reload is forbidden")

    async def close(self, *a, **k):
        raise AssertionError("page.close is forbidden")


class FakeContext:
    def __init__(self, pages):
        self.pages = pages

    async def new_page(self, *a, **k):
        raise AssertionError("new_page is forbidden")


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts

    async def close(self):
        raise AssertionError("browser.close is forbidden")


class FakeDriver:
    def __init__(self, browser=None, connect_error=None):
        self.browser = browser
        self.connect_error = connect_error
        self.stopped = 0
        self.connects = []
        self.chromium = self

    async def connect_over_cdp(self, endpoint, **kwargs):
        self.connects.append((endpoint, kwargs))
        if self.connect_error:
            raise self.connect_error
        return self.browser

    async def stop(self):
        self.stopped += 1


class FakeFactory:
    def __init__(self, driver):
        self.driver = driver
        self.created = 0

    def __call__(self):
        self.created += 1
        return self

    async def start(self):
        return self.driver


def ok(body):
    return {"status": 200, "retryAfter": None, "body": json.dumps(body)}


def reader_for(page, *, pages=None, driver_kwargs=None, timeout_ms=15000):
    context = FakeContext(pages if pages is not None else [page])
    driver = FakeDriver(FakeBrowser([context]), **(driver_kwargs or {}))
    factory = FakeFactory(driver)
    reader = ab.BrowserReader(
        ab.ENDPOINTS["home"], driver_factory=factory, request_timeout_ms=timeout_ms
    )
    return reader, driver, factory


# --- endpoints -----------------------------------------------------------------------------
def test_endpoint_per_account():
    assert ab.endpoint_for("home") == "http://127.0.0.1:9226"
    assert ab.endpoint_for("glide.vc") == "http://127.0.0.1:9223"
    with pytest.raises(ValueError):
        ab.endpoint_for("other")
    assert ab.make_reader("glide.vc")._endpoint == "http://127.0.0.1:9223"
    assert ab.make_reader("home", endpoint="http://127.0.0.1:9226")._endpoint.endswith("9226")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9223",  # glide's port for home
        "http://user@127.0.0.1:9224",
        "http://127.0.0.1:9224/json",
        "http://localhost:9224",
        "http://127.0.0.1.evil.com:9224",
        "https://127.0.0.1:9224",
        "http://127.0.0.1:9224@evil.com",
        "http://127.0.0.1:09224",
        "",
    ],
)
def test_make_reader_rejects_bad_endpoint(endpoint):
    with pytest.raises(ValueError):
        ab.make_reader("home", endpoint=endpoint)


def test_make_reader_rejects_unknown_account():
    with pytest.raises(ValueError):
        ab.make_reader("nobody")


def test_constructor_rejects_foreign_endpoint():
    with pytest.raises(ValueError):
        ab.BrowserReader("http://127.0.0.1:9999")


# --- path construction ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("scope", "suffix"),
    [
        (ListScope("recent"), "is_archived=false"),
        (ListScope("recent", "tpp"), "is_archived=false&conversation_origin=tpp"),
        (ListScope("recent", "non-tpp"), "is_archived=false&exclude_conversation_origin=tpp"),
        (ListScope("archived"), "is_archived=true"),
        (ListScope("archived", "tpp"), "is_archived=true&conversation_origin=tpp"),
        (ListScope("archived", "non-tpp"), "is_archived=true&exclude_conversation_origin=tpp"),
    ],
)
def test_list_paths(scope, suffix):
    assert ab.list_path(scope, 20, 10) == (
        f"/backend-api/conversations?offset=20&limit=10&order=updated&{suffix}"
    )


def test_project_paths():
    assert ab.list_path(ListScope("project", project_id=PROJECT), 7, 10) == (
        f"/backend-api/gizmos/{PROJECT}/conversations?cursor=7"
    )
    assert ab.projects_path(3, 25) == (
        "/backend-api/gizmos/snorlax/sidebar?owned_only=true"
        "&conversations_per_gizmo=0&limit=25&cursor=3"
    )


def test_bad_project_id_refused():
    scope = ListScope("project", project_id="g-p-../../x")
    with pytest.raises(ValueError):
        ab.list_path(scope, 0, 10)


@pytest.mark.parametrize(
    "offset,limit", [(-1, 10), (0, 0), (0, 101), (1.5, 10), ("1", 10), (True, 10), (0, None)]
)
def test_bad_ints_refused(offset, limit):
    with pytest.raises(ValueError):
        ab.list_path(ListScope("recent"), offset, limit)
    with pytest.raises(ValueError):
        ab.projects_path(offset, limit)


def test_conversation_path_and_turn_clamp():
    assert (
        ab.conversation_path("abc-1_2.3:x", 4)
        == "/backend-api/conversations/abc-1_2.3:x?num_turns=4&include_has_versions=true"
    )
    assert "num_turns=50&" in ab.conversation_path("abc", 500)
    assert "num_turns=1&" in ab.conversation_path("abc", 1)


@pytest.mark.parametrize(
    "cid", ["", "..", ".", "a/b", "a?b", "a b", "a#b", "x" * 129, "\u00e9", None, 5]
)
def test_bad_conversation_ids_refused(cid):
    with pytest.raises(ValueError):
        ab.conversation_path(cid, 3)


@pytest.mark.parametrize("turns", [0, -1, 2.5, "3", True, None])
def test_bad_turns_refused(turns):
    with pytest.raises(ValueError):
        ab.conversation_path("abc", turns)


# --- page selection / connection -----------------------------------------------------------
def test_reuses_open_chatgpt_page_and_connects_like_guardian():
    other = FakePage("https://example.com/")
    closed = FakePage(closed=True)
    lookalike = FakePage("https://chatgpt.com.evil.com/c/x")
    userinfo = FakePage("https://chatgpt.com@evil.com/")
    http = FakePage("http://chatgpt.com/")
    target = FakePage(result=ok({"id": "c1"}))
    reader, driver, factory = reader_for(
        None, pages=[other, closed, lookalike, userinfo, http, target]
    )
    assert reader.read_conversation("c1", 3) == {"id": "c1", "native_format": "flat"}
    assert driver.connects == [("http://127.0.0.1:9226", {"timeout": 10000, "no_defaults": True})]
    assert len(target.calls) == 1
    assert target.calls[0][1] == {
        "mode": "data",
        "path": "/backend-api/conversations/c1?num_turns=3&include_has_versions=true",
        "timeoutMs": 15000,
        "maxBytes": 8 << 20,
    }
    assert not any(p.calls for p in (other, closed, lookalike, userinfo, http))
    reader.read_conversation("c1", 3)
    assert factory.created == 1  # connected once
    reader.close()


def test_a_conversation_read_is_marked_flat_by_the_reader_not_by_the_server():
    page = FakePage(result=ok({"id": "c1", "native_format": "mapping", "messages": []}))
    reader, _, _ = reader_for(page)
    payload = reader.read_conversation("c1", 3)
    assert payload == {"id": "c1", "native_format": "flat", "messages": []}
    reader.close()


def test_listing_uses_one_mib_cap():
    page = FakePage(result=ok({"items": []}))
    reader, _, _ = reader_for(page)
    reader.list_page(ListScope("recent"), 0, 10)
    assert page.calls[0][1]["maxBytes"] == 1 << 20
    reader.close()


def test_no_chatgpt_page_fails_without_touching_browser():
    page = FakePage("https://example.com/")
    reader, driver, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "no open chatgpt.com page"
    assert page.calls == []
    reader.close()
    assert driver.stopped == 1


def test_empty_pages_fail():
    reader, _, _ = reader_for(None, pages=[])
    with pytest.raises(ReaderUnavailable, match="no open chatgpt.com page"):
        reader.identity()
    reader.close()


@pytest.mark.parametrize("count", [0, 2])
def test_context_count_must_be_one(count):
    contexts = [FakeContext([FakePage(result=ok({}))]) for _ in range(count)]
    driver = FakeDriver(FakeBrowser(contexts))
    reader = ab.BrowserReader(ab.ENDPOINTS["home"], driver_factory=FakeFactory(driver))
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "expected exactly one browser context"
    assert driver.stopped == 1  # a refused connection is torn down, the browser is not closed
    reader.close()


def test_cdp_unreachable():
    reader, driver, _ = reader_for(
        None, driver_kwargs={"connect_error": OSError("refused " + CANARY)}
    )
    with pytest.raises(ReaderUnavailable) as info:
        reader.identity()
    assert info.value.reason == "cdp unreachable"
    assert CANARY not in repr(info.value) and info.value.__cause__ is None
    assert driver.stopped == 1
    reader.close()


def test_playwright_missing_reason(monkeypatch):
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.delenv(ab._SITE_PACKAGES_ENV, raising=False)
    reader = ab.make_reader("home")
    with pytest.raises(ReaderUnavailable) as info:
        reader.identity()
    assert info.value.reason == "playwright not installed"
    reader.close()


def test_playwright_import_is_lazy():
    code = "import sys, pmlib.chatgpt_control.audit_browser; print('playwright' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


# --- status mapping ------------------------------------------------------------------------
def test_success_returns_dict():
    page = FakePage(result=ok({"items": [{"id": "1"}], "total": 1}))
    reader, _, _ = reader_for(page)
    assert reader.list_page(ListScope("recent"), 0, 10) == {"items": [{"id": "1"}], "total": 1}
    reader.close()


@pytest.mark.parametrize(
    "status,retry", [(401, None), (403, "5"), (429, "120"), (500, None), (404, "x")]
)
def test_http_errors(status, retry):
    page = FakePage(result={"status": status, "retryAfter": retry, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderHttpError) as info:
        reader.read_conversation("c1", 3)
    assert info.value.status == status and info.value.retry_after == retry
    reader.close()


def test_retry_after_is_bounded():
    page = FakePage(result={"status": 429, "retryAfter": "9" * 500, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderHttpError) as info:
        reader.read_conversation("c1", 3)
    assert len(info.value.retry_after) <= 64
    reader.close()


def test_page_side_abort_is_timeout():
    page = FakePage(result={"status": -2, "retryAfter": None, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderTimeout):
        reader.read_conversation("c1", 3)
    reader.close()


def test_hard_deadline_is_timeout(monkeypatch):
    import asyncio

    async def hang(_args):
        await asyncio.sleep(30)

    page = FakePage(result=hang)
    reader, _, _ = reader_for(page, timeout_ms=1)
    original = asyncio.wait_for

    async def fast(awaitable, timeout):  # shrink only the grace period, keep the mechanism
        return await original(awaitable, min(timeout, 0.05))

    monkeypatch.setattr(ab.asyncio, "wait_for", fast)
    with pytest.raises(ReaderTimeout):
        reader.read_conversation("c1", 3)
    monkeypatch.setattr(ab.asyncio, "wait_for", original)
    reader.close()


def test_timeout_is_clamped():
    reader = ab.BrowserReader(ab.ENDPOINTS["home"], request_timeout_ms=10**9)
    assert reader._timeout_ms == 60000
    with pytest.raises(ValueError):
        ab.BrowserReader(ab.ENDPOINTS["home"], request_timeout_ms=0)


def test_logged_out_is_unavailable():
    page = FakePage(result={"status": 0, "retryAfter": None, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "not logged in"
    reader.close()


def test_oversize_is_unavailable():
    page = FakePage(result={"status": -1, "retryAfter": None, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "response too large"
    reader.close()


@pytest.mark.parametrize(
    "body",
    [
        '{"a": NaN}',
        '{"a": Infinity}',
        '{"a": -Infinity}',
        "[1]",
        '"s"',
        "42",
        "null",
        "garbage",
        "",
        "{",
    ],
)
def test_strict_json(body):
    page = FakePage(result={"status": 200, "retryAfter": None, "body": body})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "unusable response"
    reader.close()


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "x",
        {"status": "200", "retryAfter": None, "body": "{}"},
        {"status": True, "retryAfter": None, "body": "{}"},
        {"status": 200, "retryAfter": 5, "body": "{}"},
        {"status": 200, "retryAfter": None, "body": {"a": 1}},
        {"status": 200},
        {"status": 999, "retryAfter": None, "body": ""},
    ],
)
def test_malformed_evaluate_result(raw):
    page = FakePage(result=raw)
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "unusable response"
    reader.close()


def test_evaluate_exception_text_never_surfaces(caplog):
    page = FakePage(result=RuntimeError("Bearer " + CANARY))
    reader, _, _ = reader_for(page)
    with caplog.at_level(logging.DEBUG), pytest.raises(ReaderUnavailable) as info:
        reader.read_conversation("c1", 3)
    assert info.value.reason == "unusable response"
    assert CANARY not in str(info.value) and CANARY not in repr(info.value)
    assert info.value.__cause__ is None
    assert CANARY not in caplog.text
    reader.close()


def test_hostile_extra_keys_never_leak(caplog):
    hostile = {
        "status": 200,
        "retryAfter": None,
        "body": json.dumps({"id": "c1"}),
        "extra": CANARY,
        "session": CANARY,
        "cookies": CANARY,
    }
    page = FakePage(result=hostile)
    reader, _, _ = reader_for(page)
    with caplog.at_level(logging.DEBUG):
        value = reader.read_conversation("c1", 3)
    assert value == {"id": "c1", "native_format": "flat"} and CANARY not in json.dumps(value)
    assert CANARY not in caplog.text
    reader.close()


def test_hostile_identity_extra_keys_never_leak():
    page = FakePage(
        result={"status": 200, "retryAfter": None, "body": HEX, "email": CANARY, "extra": CANARY}
    )
    reader, _, _ = reader_for(page)
    result = reader.identity()
    assert result == ReaderIdentity(account_hash=HEX)
    assert CANARY not in repr(result)
    reader.close()


# --- list_projects -------------------------------------------------------------------------
def test_list_projects_flattens_and_filters():
    payload = {
        "items": [
            {"id": PROJECT},
            {"gizmo": {"gizmo": {"id": "g-p-" + "cd" * 16}}},
            {"id": "g-p-../x"},
            {"id": "g-abc"},
            {"gizmo": {"gizmo": {"id": 5}}},
            "junk",
            {"gizmo": "x"},
        ],
        "cursor": "5",
    }
    page = FakePage(result=ok(payload))
    reader, _, _ = reader_for(page)
    assert reader.list_projects(0, 25) == {
        "items": [{"id": PROJECT}, {"id": "g-p-" + "cd" * 16}],
        "total": None,
    }
    assert page.calls[0][1]["path"].startswith("/backend-api/gizmos/snorlax/sidebar?")
    reader.close()


def test_list_projects_without_items_is_unusable():
    page = FakePage(result=ok({"cursor": None}))
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable, match="unusable response"):
        reader.list_projects(0, 25)
    reader.close()


# --- identity ------------------------------------------------------------------------------
@pytest.mark.parametrize("body", ["A" * 64, "a" * 63, "a" * 65, "g" * 64, "", "user@example.com"])
def test_identity_rejects_non_hash(body):
    page = FakePage(result={"status": 200, "retryAfter": None, "body": body})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable, match="unusable response"):
        reader.identity()
    reader.close()


def test_identity_request_and_logged_out():
    page = FakePage(result={"status": 200, "retryAfter": None, "body": HEX})
    reader, _, _ = reader_for(page)
    assert reader.identity().account_hash == HEX
    assert page.calls[0][1]["mode"] == "identity"
    reader.close()
    page = FakePage(result={"status": 0, "retryAfter": None, "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderUnavailable, match="not logged in"):
        reader.identity()
    reader.close()


def test_identity_http_error():
    page = FakePage(result={"status": 429, "retryAfter": "30", "body": ""})
    reader, _, _ = reader_for(page)
    with pytest.raises(ReaderHttpError) as info:
        reader.identity()
    assert (info.value.status, info.value.retry_after) == (429, "30")
    reader.close()


# --- close ---------------------------------------------------------------------------------
def test_close_stops_driver_only():
    page = FakePage(result=ok({}))
    reader, driver, _ = reader_for(page)
    reader.read_conversation("c1", 3)
    reader.close()
    assert driver.stopped == 1  # FakeBrowser/page/context raise if closed
    reader.close()  # idempotent
    assert driver.stopped == 1


def test_close_without_connect_is_safe():
    reader, driver, factory = reader_for(FakePage())
    reader.close()
    assert driver.stopped == 0 and factory.created == 0


# --- import_playwright ---------------------------------------------------------------------
@pytest.fixture
def clean_modules(monkeypatch):
    before_path = list(sys.path)
    before_mods = set(sys.modules)
    monkeypatch.setitem(sys.modules, "playwright", None)  # force the fallback path
    monkeypatch.delenv(ab._SITE_PACKAGES_ENV, raising=False)
    yield
    sys.path[:] = before_path
    for name in set(sys.modules) - before_mods:
        sys.modules.pop(name, None)


def make_site(tmp_path, mode=0o755, pkg_mode=0o755, with_pkg=True):
    site = tmp_path / "site"
    site.mkdir()
    if with_pkg:
        pkg = site / "playwright"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "async_api.py").write_text("def async_playwright():\n    return 'fake-driver'\n")
        pkg.chmod(pkg_mode)
    site.chmod(mode)
    return site


def allow_fallback_import():
    # Drop the None placeholder so the appended directory can actually be imported from.
    sys.modules.pop("playwright", None)


@pytest.mark.parametrize("mode", [0o700, 0o755])
def test_import_playwright_accepts_trusted_dir(tmp_path, clean_modules, mode):
    site = make_site(tmp_path, mode=mode)
    ab_import = ab.importlib.import_module
    calls = []

    def fake_import(name):
        calls.append(name)
        if len(calls) == 1:
            raise ImportError("not in the normal path")
        allow_fallback_import()
        return ab_import(name)

    ab.importlib.import_module = fake_import
    try:
        factory = ab.import_playwright(str(site))
    finally:
        ab.importlib.import_module = ab_import
    assert factory() == "fake-driver"
    assert sys.path[-1] == str(site)  # appended, never prepended


def test_import_playwright_uses_env(tmp_path, clean_modules, monkeypatch):
    site = make_site(tmp_path)
    monkeypatch.setenv(ab._SITE_PACKAGES_ENV, str(site))
    ab_import = ab.importlib.import_module
    first = []

    def fake_import(name):
        if not first:
            first.append(1)
            raise ImportError("not in the normal path")
        allow_fallback_import()
        return ab_import(name)

    monkeypatch.setattr(ab.importlib, "import_module", fake_import)
    assert ab.import_playwright()() == "fake-driver"


@pytest.mark.parametrize("mode", [0o775, 0o757, 0o777, 0o770])
def test_import_playwright_rejects_writable_dir(tmp_path, clean_modules, mode):
    site = make_site(tmp_path, mode=mode)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))
    assert str(site) not in sys.path


def test_import_playwright_rejects_writable_package(tmp_path, clean_modules):
    site = make_site(tmp_path, pkg_mode=0o775)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))
    assert str(site) not in sys.path


def test_import_playwright_rejects_foreign_owner(tmp_path, clean_modules, monkeypatch):
    site = make_site(tmp_path)
    foreign = os.getuid() + 1
    monkeypatch.setattr(ab.os, "getuid", lambda: foreign)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))
    assert str(site) not in sys.path


def test_import_playwright_rejects_symlinked_dir(tmp_path, clean_modules):
    real = make_site(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(link))
    assert str(link) not in sys.path


def test_import_playwright_rejects_symlinked_package(tmp_path, clean_modules):
    site = make_site(tmp_path, with_pkg=False)
    real = tmp_path / "elsewhere"
    real.mkdir()
    (site / "playwright").symlink_to(real)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))


def test_import_playwright_rejects_missing_package_and_dir(tmp_path, clean_modules):
    site = make_site(tmp_path, with_pkg=False)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(tmp_path / "missing"))
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(None)


def test_import_playwright_cleans_path_on_broken_package(tmp_path, clean_modules, monkeypatch):
    site = make_site(tmp_path)
    (site / "playwright" / "async_api.py").write_text("raise RuntimeError('boom')\n")
    real_import = ab.importlib.import_module
    first = []

    def fake_import(name):
        if not first:
            first.append(1)
            raise ImportError("not in the normal path")
        allow_fallback_import()
        return real_import(name)

    monkeypatch.setattr(ab.importlib, "import_module", fake_import)
    with pytest.raises(ReaderUnavailable, match="playwright not installed"):
        ab.import_playwright(str(site))
    assert str(site) not in sys.path


# --- the page-side JS, executed under node with a stubbed fetch ---------------------------
NODE = shutil.which("node")

NODE_HARNESS = r"""
const fn = (%JS%);
const CANARY = %CANARY%;
const scenario = %SCENARIO%;
const calls = [];
globalThis.fetch = async (url, init) => {
  %PRE%
  calls.push({url, auth: (init.headers || {}).Authorization || null, hasSignal: !!init.signal});
  if (url === '/api/auth/session') {
    return new Response(JSON.stringify(scenario.session), {status: scenario.sessionStatus || 200,
      headers: scenario.sessionHeaders || {}});
  }
  if (scenario.hang) {
    return new Promise((_, reject) => init.signal.addEventListener('abort',
      () => reject(Object.assign(new Error('aborted ' + CANARY), {name: 'AbortError'}))));
  }
  return new Response(scenario.body, {status: scenario.status || 200, headers: scenario.headers || {}});
};
(async () => {
  let result, error = null;
  try { result = await fn(scenario.args); } catch (e) { error = String(e && e.message); }
  process.stdout.write(JSON.stringify({result, error, calls}));
})();
"""


def run_js(tmp_path, scenario, pre=""):
    script = (
        NODE_HARNESS.replace("%JS%", ab.FETCH_JS)
        .replace("%CANARY%", json.dumps(CANARY))
        .replace("%SCENARIO%", json.dumps(scenario))
        .replace("%PRE%", pre)
    )
    path = tmp_path / "harness.js"
    path.write_text(script)
    done = subprocess.run([NODE, str(path)], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(done.stdout)


SESSION = {"accessToken": CANARY, "user": {"id": "user-1", "email": "x@example.com"}}


def node_args(mode="data", max_bytes=1 << 20, timeout=5000):
    return {
        "mode": mode,
        "path": "/backend-api/conversations/c1?num_turns=3&include_has_versions=true",
        "timeoutMs": timeout,
        "maxBytes": max_bytes,
    }


def leaks_outside_request_header(output):
    stripped = {**output, "calls": [{**c, "auth": None} for c in output["calls"]]}
    return CANARY in json.dumps(stripped)


@pytest.mark.skipif(NODE is None, reason="node not installed")
class TestPageScript:
    def test_data_returns_only_three_keys(self, tmp_path):
        out = run_js(
            tmp_path,
            {
                "session": SESSION,
                "body": '{"id": "c1"}',
                "headers": {"Retry-After": "7"},
                "args": node_args(),
            },
        )
        assert out["error"] is None
        assert out["result"] == {"status": 200, "retryAfter": "7", "body": '{"id": "c1"}'}
        assert [c["url"] for c in out["calls"]] == [
            "/api/auth/session",
            "/backend-api/conversations/c1?num_turns=3&include_has_versions=true",
        ]
        assert out["calls"][1]["auth"] == "Bearer " + CANARY  # only the in-page request carries it
        assert out["calls"][0]["auth"] is None
        assert all(c["hasSignal"] for c in out["calls"])
        assert not leaks_outside_request_header(out)

    def test_http_error_returns_status_and_retry_after(self, tmp_path):
        out = run_js(
            tmp_path,
            {
                "session": SESSION,
                "status": 429,
                "body": "private-body",
                "headers": {"Retry-After": "30"},
                "args": node_args(),
            },
        )
        assert out["result"] == {"status": 429, "retryAfter": "30", "body": ""}

    def test_session_error_status(self, tmp_path):
        out = run_js(tmp_path, {"session": {}, "sessionStatus": 403, "args": node_args()})
        assert out["result"] == {"status": 403, "retryAfter": None, "body": ""}
        assert len(out["calls"]) == 1

    def test_logged_out(self, tmp_path):
        out = run_js(tmp_path, {"session": {}, "args": node_args()})
        assert out["result"] == {"status": 0, "retryAfter": None, "body": ""}
        assert len(out["calls"]) == 1

    def test_over_cap(self, tmp_path):
        out = run_js(
            tmp_path, {"session": SESSION, "body": "x" * 5000, "args": node_args(max_bytes=1000)}
        )
        assert out["result"] == {"status": -1, "retryAfter": None, "body": ""}

    def test_abort_is_sentinel_not_message(self, tmp_path):
        out = run_js(tmp_path, {"session": SESSION, "hang": True, "args": node_args(timeout=50)})
        assert out["result"] == {"status": -2, "retryAfter": None, "body": ""}
        assert not leaks_outside_request_header(out)

    def test_identity_returns_only_hash(self, tmp_path):
        out = run_js(tmp_path, {"session": SESSION, "args": node_args(mode="identity")})
        expected = hashlib.sha256(b"pm-chatgpt-audit-v1:user-1").hexdigest()
        assert out["result"] == {"status": 200, "retryAfter": None, "body": expected}
        assert [c["url"] for c in out["calls"]] == ["/api/auth/session"]
        assert not leaks_outside_request_header(out)
        assert "x@example.com" not in json.dumps(out["result"])

    def test_identity_without_user_id_is_logged_out(self, tmp_path):
        out = run_js(
            tmp_path,
            {"session": {"accessToken": CANARY}, "args": node_args(mode="identity")},
        )
        assert out["result"]["status"] == 0

    def test_network_failure_message_is_fixed(self, tmp_path):
        pre = "if (url !== '/api/auth/session') throw new TypeError('boom ' + CANARY);"
        out = run_js(tmp_path, {"session": SESSION, "args": node_args(), "body": "ok"}, pre=pre)
        assert out["error"] == "page fetch failed"
