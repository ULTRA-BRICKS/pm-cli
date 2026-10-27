"""Chat/project URL canonicalization for the audit: exact forms in, one canonical form out."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control import audit_urls

PROJECT = "g-p-0123456789abcdef0123456789abcdef"
UUID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    "raw,conversation,project",
    [
        ("https://chatgpt.com/c/abc_DEF-1", "abc_DEF-1", None),
        ("https://chatgpt.com/c/abc_DEF-1/", "abc_DEF-1", None),
        ("https://chatgpt.com/c/abc_DEF-1?model=x#frag", "abc_DEF-1", None),
        ("HTTPS://ChatGPT.com/c/abc", "abc", None),
        ("https://chat.openai.com/c/abc", "abc", None),
        (f"https://chatgpt.com/g/{PROJECT}/c/{UUID}", UUID, PROJECT),
        (f"https://chatgpt.com/g/{PROJECT}-my-project/c/{UUID}/", UUID, PROJECT),
        (f"https://chatgpt.com/g/{PROJECT}-my-long-project-name/c/{UUID}?x=1", UUID, PROJECT),
        (f"https://chat.openai.com/g/{PROJECT}/c/{UUID.upper()}", UUID, PROJECT),
        (f"https://chatgpt.com/g/{PROJECT.upper()}/c/{UUID}", UUID, PROJECT),
    ],
)
def test_conversation_urls_canonicalize(raw, conversation, project):
    ref = audit_urls.parse_chat_url(raw)
    assert ref.conversation_id == conversation
    assert ref.project_id == project
    assert ref.url == f"https://chatgpt.com/c/{conversation}"


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/c/abc",
        "https://chatgpt.com.evil.example/c/abc",
        "https://evilchatgpt.com/c/abc",
        "https://user@chatgpt.com/c/abc",
        "https://user:pw@chatgpt.com/c/abc",
        "https://chatgpt.com:8443/c/abc",
        "http://chatgpt.com/c/abc",
        "ftp://chatgpt.com/c/abc",
        "//chatgpt.com/c/abc",
        "/c/abc",
        "chatgpt.com/c/abc",
        "https://chatgpt.com/C/abc",
        "https://chatgpt.com/c/",
        "https://chatgpt.com/c/a%2Fb",
        "https://chatgpt.com/c/a b",
        "https://chatgpt.com//c/abc",
        "https://chatgpt.com/c/abc/extra",
        "https://chatgpt.com/c/../c/abc",
        f"https://chatgpt.com/g/{PROJECT}/c/not-a-uuid",
        "https://chatgpt.com/g/g-p-short/c/" + UUID,
        f"https://chatgpt.com/g/{PROJECT}/project",
        "https://chatgpt.com/",
        "",
        None,
        123,
        "https://chatgpt.com/c/" + "a" * 200,
        "https://chatgpt.com/c/abc\n",
        "https://chatgpt.com/c/abc\x00",
    ],
)
def test_bad_conversation_urls_are_rejected(raw):
    with pytest.raises(audit_urls.UrlError):
        audit_urls.parse_chat_url(raw)


@pytest.mark.parametrize(
    "raw",
    [
        f"https://chatgpt.com/g/{PROJECT}/project",
        f"https://chatgpt.com/g/{PROJECT}-slug/project",
        f"https://chatgpt.com/g/{PROJECT}/project/",
        f"https://chat.openai.com/g/{PROJECT}/project?x=1#y",
        f"HTTPS://CHATGPT.COM/g/{PROJECT.upper()}/project",
    ],
)
def test_project_urls_canonicalize(raw):
    ref = audit_urls.parse_project_url(raw)
    assert ref.project_id == PROJECT
    assert ref.url == f"https://chatgpt.com/g/{PROJECT}/project"


@pytest.mark.parametrize(
    "raw",
    [
        "https://evil.example/g/" + PROJECT + "/project",
        "https://chatgpt.com/g/g-p-zz/project",
        f"https://chatgpt.com/g/{PROJECT}/c/{UUID}",
        f"https://chatgpt.com/g/{PROJECT}/projects",
        "https://chatgpt.com/project",
    ],
)
def test_bad_project_urls_are_rejected(raw):
    with pytest.raises(audit_urls.UrlError):
        audit_urls.parse_project_url(raw)


def test_canonical_form_matches_the_existing_chat_contract():
    from pmlib.chatgpt_monitor import CHAT_URL

    assert CHAT_URL.fullmatch(audit_urls.parse_chat_url("https://chatgpt.com/c/abc").url)


def test_project_id_is_recognized_in_a_native_payload_value():
    assert audit_urls.valid_project_id(PROJECT)
    assert not audit_urls.valid_project_id("g-p-xyz")
    assert not audit_urls.valid_project_id(None)


# --- the native read endpoint -------------------------------------------------------------
def test_the_read_path_is_the_plural_endpoint_with_a_turn_limit():
    assert (
        audit_urls.conversation_read_path("abc-1_2.3:x", 4)
        == "/backend-api/conversations/abc-1_2.3:x?num_turns=4&include_has_versions=true"
    )
    assert "/backend-api/conversation/" not in audit_urls.conversation_read_path("abc", 4)


def test_the_read_path_clamps_the_turn_limit():
    assert "num_turns=50&" in audit_urls.conversation_read_path("abc", 500)
    assert "num_turns=1&" in audit_urls.conversation_read_path("abc", 1)


@pytest.mark.parametrize(
    "cid", ["", "..", ".", "a/b", "a?b", "a b", "a#b", "x" * 129, "\u00e9", None, 5]
)
def test_the_read_path_refuses_bad_conversation_ids(cid):
    with pytest.raises(ValueError):
        audit_urls.conversation_read_path(cid, 3)


@pytest.mark.parametrize("turns", [0, -1, 2.5, "3", True, None])
def test_the_read_path_refuses_bad_turn_counts(turns):
    with pytest.raises(ValueError):
        audit_urls.conversation_read_path("abc", turns)


def test_context_expansion_steps_are_bounded_and_strictly_increasing():
    assert audit_urls.TURN_STEPS == (4, 8, 16)
    assert audit_urls.next_turns(4) == 8
    assert audit_urls.next_turns(8) == 16
    assert audit_urls.next_turns(1) == 4
    assert audit_urls.next_turns(16) is None
    assert audit_urls.next_turns(30) is None


def test_the_native_formats_are_a_closed_vocabulary():
    assert audit_urls.NATIVE_FORMATS == ("flat", "mapping")
    assert audit_urls.NATIVE_FORMAT_KEY == "native_format"
