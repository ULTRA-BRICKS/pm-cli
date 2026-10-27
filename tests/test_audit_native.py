"""Native conversation/list payloads to evidence: visible roles only, no hidden reasoning."""

from __future__ import annotations

import json

import pytest

from pmlib.chatgpt_control import audit_evidence as ev
from pmlib.chatgpt_control import audit_native as native
from tests.audit_support import ACCOUNT, CONVERSATION, NOW

GO = "\u0433\u043e"  # the Russian word for "go"


def node(nid, parent, role, text=None, *, ctype="text", t=0.0, **kw):
    status = kw.pop("status", "finished_successfully")
    meta = kw.pop("meta", None) or {}
    content = {"content_type": ctype, "parts": [] if text is None else [text]}
    if ctype == "thoughts":
        content = {"content_type": "thoughts", "thoughts": [{"content": text}]}
    message = {
        "id": nid,
        "author": {"role": role},
        "create_time": NOW - 1000 + t,
        "content": content,
        "status": status,
        "end_turn": kw.pop("end_turn", None),
        "recipient": kw.pop("recipient", "all"),
        "channel": kw.pop("channel", None),
        "metadata": meta,
    }
    assert not kw, kw
    return {"id": nid, "message": message, "parent": parent, "children": []}


def convo(nodes, current=None, *, with_root=True, **top):
    mapping = {n["id"]: n for n in nodes}
    if with_root:
        mapping["root"] = {"id": "root", "message": None, "parent": None, "children": []}
    return {
        "conversation_id": CONVERSATION,
        "title": "t",
        "update_time": NOW - 300,
        "current_node": current or nodes[-1]["id"],
        "mapping": mapping,
        **top,
    }


def listed(**kw):
    base = {
        "conversation_id": CONVERSATION,
        "updated_at": NOW - 300,
        "archived": False,
        "project_id": None,
    }
    base.update(kw)
    return native.ListedConversation(**base)


def build(payload, **kw):
    args = {"listed": listed(), "account": ACCOUNT, "observed_account": ACCOUNT, "now": NOW}
    args.update(kw)
    return native.build_evidence(payload, **args)


def simple():
    return convo(
        [
            node("u1", "root", "user", "Summarize the three options.", t=0),
            node("a1", "u1", "assistant", "Option B wins; here is why.", t=10, end_turn=True),
        ]
    )


def test_simple_conversation_maps_to_baseline_evidence():
    e = build(simple())
    assert [(m.id, m.role, m.final) for m in e.messages] == [
        ("u1", "user", False),
        ("a1", "assistant", True),
    ]
    assert e.request_id == "u1" and e.request_text == "Summarize the three options."
    assert e.final_id == "a1" and e.final_text == "Option B wins; here is why."
    assert (e.read_at, e.verified_at) == (NOW, NOW)
    assert e.updated_at == NOW - 300 and not e.archived and e.project_id is None
    assert not e.window_cut and not e.context_truncated
    assert e.url == f"https://chatgpt.com/c/{CONVERSATION}"
    assert (e.account, e.observed_account) == (ACCOUNT, ACCOUNT)


def test_hidden_reasoning_system_and_tool_text_never_enter_the_evidence():
    nodes = [
        node("s", "root", "system", "SYSTEM-SECRET"),
        node("u1", "s", "user", "Fix the bug.", t=1),
        node("th", "u1", "assistant", "CHAIN-OF-THOUGHT-SECRET", ctype="thoughts", t=2),
        node("rc", "th", "assistant", "Thought for 9s RECAP-SECRET", ctype="reasoning_recap", t=3),
        node("tl", "rc", "tool", "TOOL-OUTPUT-SECRET", ctype="execution_output", t=4),
        node("a1", "tl", "assistant", "Fixed and verified.", t=5, end_turn=True),
    ]
    e = build(convo(nodes))
    wire = json.dumps(e.to_mapping())
    for secret in (
        "SYSTEM-SECRET",
        "CHAIN-OF-THOUGHT-SECRET",
        "RECAP-SECRET",
        "TOOL-OUTPUT-SECRET",
    ):
        assert secret not in wire
    assert "th" not in [m.id for m in e.messages]
    assert "s" not in [m.id for m in e.messages] and "tl" not in [m.id for m in e.messages]
    recap = next(m for m in e.messages if m.id == "rc")
    assert recap.recap and not recap.final
    assert next(m for m in e.messages if m.id == "a1").tool_success


def test_failed_tool_does_not_mark_tool_success():
    nodes = [
        node("u1", "root", "user", "Run it.", t=1),
        node("tl", "u1", "tool", "boom", ctype="execution_output", status="error", t=2),
        node("a1", "tl", "assistant", "It failed.", t=3, end_turn=True),
    ]
    assert not next(m for m in build(convo(nodes)).messages if m.id == "a1").tool_success


def test_only_the_current_branch_is_read():
    nodes = [
        node("u1", "root", "user", "Question.", t=1),
        node("old", "u1", "assistant", "Abandoned branch answer.", t=2, end_turn=True),
        node("a1", "u1", "assistant", "Current branch answer.", t=3, end_turn=True),
    ]
    e = build(convo(nodes, current="a1"))
    assert [m.id for m in e.messages] == ["u1", "a1"] and "Abandoned" not in e.final_text


def test_go_after_a_substantive_request_resolves_the_prior_request():
    nodes = [
        node("u1", "root", "user", "Install and verify the package.", t=1),
        node("a1", "u1", "assistant", "Installed; verifying next.", t=2, end_turn=False),
        node("u2", "a1", "user", GO, t=3),
        node("a2", "u2", "assistant", "Verified: 140 tests pass.", t=4, end_turn=True),
    ]
    e = build(convo(nodes))
    assert e.request_id == "u1" and e.last_user.id == "u2" and e.final_id == "a2"
    assert e.request_text == "Install and verify the package."


def test_only_go_with_no_original_goal_has_no_substantive_request():
    e = build(
        convo([node("u1", "root", "user", GO, t=1), node("a1", "u1", "assistant", "OK", t=2)])
    )
    assert e.request_id is None and e.request_text == ""


def test_window_cut_with_a_request_inside_is_not_truncated():
    nodes = [
        node("u1", "gone", "user", "Do the thing.", t=1),
        node("a1", "u1", "assistant", "Done.", t=2, end_turn=True),
    ]
    e = build(convo(nodes, with_root=False))
    assert e.window_cut and not e.context_truncated and e.request_id == "u1"


def test_window_cut_with_no_substantive_request_is_truncated_context():
    nodes = [
        node("u1", "gone", "user", GO, t=1),
        node("a1", "u1", "assistant", "Done.", t=2, end_turn=True),
    ]
    e = build(convo(nodes, with_root=False))
    assert e.window_cut and e.context_truncated and e.request_id is None


def test_in_progress_status_is_carried():
    nodes = [
        node("u1", "root", "user", "Go build it.", t=1),
        node("a1", "u1", "assistant", "Working", t=2, status="in_progress"),
    ]
    e = build(convo(nodes))
    assert any(m.in_progress for m in e.messages)


def test_channel_decides_final_versus_commentary():
    nodes = [
        node("u1", "root", "user", "Analyse it.", t=1),
        node(
            "c1",
            "u1",
            "assistant",
            "Looking at the data...",
            t=2,
            channel="commentary",
            end_turn=False,
        ),
        node("a1", "c1", "assistant", "Analysis result.", t=3, channel="final", end_turn=True),
        node("c2", "a1", "assistant", "Extra remark.", t=4, channel="commentary", end_turn=False),
    ]
    e = build(convo(nodes))
    assert e.final_id == "a1" and e.final_text == "Analysis result."
    assert [m.final for m in e.messages] == [False, False, True, False]


def test_assistant_message_addressed_to_a_tool_is_not_a_final_answer():
    nodes = [
        node("u1", "root", "user", "Search it.", t=1),
        node("a1", "u1", "assistant", "search(query)", t=2, recipient="browser", end_turn=False),
    ]
    e = build(convo(nodes))
    assert e.final_id is None and e.final_text == ""


def test_text_is_bounded_and_truncation_is_flagged():
    nodes = [
        node("u1", "root", "user", "r" * (ev.MAX_TEXT + 10), t=1),
        node("a1", "u1", "assistant", "f" * (ev.MAX_TEXT + 10), t=2, end_turn=True),
    ]
    e = build(convo(nodes))
    assert len(e.request_text) == ev.MAX_TEXT and e.request_truncated
    assert len(e.final_text) == ev.MAX_TEXT and e.final_truncated


def test_control_characters_are_stripped():
    nodes = [
        node("u1", "root", "user", "Go\x00 do\x07 it.", t=1),
        node("a1", "u1", "assistant", "Done\x1b.", t=2, end_turn=True),
    ]
    e = build(convo(nodes))
    assert e.request_text == "Go do it." and e.final_text == "Done."


def test_expired_permission_final_after_the_latest_user_sets_the_signal():
    nodes = [
        node("u1", "root", "user", "Clean up the repo.", t=1),
        node("a1", "u1", "assistant", "I stopped. The permission has expired.", t=2, end_turn=True),
    ]
    assert build(convo(nodes)).permission_expired


def test_signals_from_a_final_before_the_latest_user_are_not_current():
    nodes = [
        node("u1", "root", "user", "Clean up the repo.", t=1),
        node("a1", "u1", "assistant", "The permission has expired.", t=2, end_turn=True),
        node("u2", "a1", "user", "Permission granted again, retry.", t=3),
    ]
    assert not build(convo(nodes)).permission_expired


def media_node(nid, parent, role, parts, *, t=0.0, **kw):
    """A multimodal message whose parts may be text strings or non-text attachment dicts."""
    n = node(nid, parent, role, None, ctype="multimodal_text", t=t, **kw)
    n["message"]["content"]["parts"] = parts
    return n


IMAGE_PART = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://SECRET-ASSET"}


def test_login_requirement_in_the_final_sets_its_own_signal():
    nodes = [
        node("u1", "root", "user", "Check the dashboard numbers.", t=1),
        node(
            "a1",
            "u1",
            "assistant",
            "You need to log in again before I can read it.",
            t=2,
            end_turn=True,
        ),
    ]
    e = build(convo(nodes))
    assert e.login_required and not e.permission_expired


def test_unfinished_cues_use_the_request_and_final_pair():
    nodes = [
        node("u1", "root", "user", "Implement the importer, install it and verify it live.", t=1),
        node(
            "a1",
            "u1",
            "assistant",
            "Here is the plan:\n1. Add it.\n2. Install it.\n3. Verify.",
            t=2,
            end_turn=True,
        ),
    ]
    e = build(convo(nodes))
    assert e.unfinished_cue == "plan-only" and e.continuation_announced
    advice = [
        node("u1", "root", "user", "Draft an implementation plan for the importer.", t=1),
        node(
            "a1",
            "u1",
            "assistant",
            "Here is the plan:\n1. Add it.\n2. Install it.",
            t=2,
            end_turn=True,
        ),
    ]
    assert build(convo(advice)).unfinished_cue == ""
    deploy = [
        node("u1", "root", "user", "Deploy the fix to production and verify it live.", t=1),
        node("a1", "u1", "assistant", "All 140 tests pass on the branch.", t=2, end_turn=True),
    ]
    assert build(convo(deploy)).doubt_cue == "deployment-unevidenced"


def test_unfinished_cues_from_a_final_before_the_latest_user_are_not_current():
    nodes = [
        node("u1", "root", "user", "Install it.", t=1),
        node("a1", "u1", "assistant", "I'll continue in the next message.", t=2, end_turn=True),
        node("u2", "a1", "user", "What is 12*8?", t=3),
    ]
    e = build(convo(nodes))
    assert e.unfinished_cue == "" and not e.continuation_announced


def test_stop_intent_is_read_from_the_latest_substantive_request():
    nodes = [
        node("u1", "root", "user", "Write a handoff note and wrap up.", t=1),
        node(
            "a1",
            "u1",
            "assistant",
            "Handoff: the importer is half done; resume at the parser.",
            t=2,
            end_turn=True,
        ),
    ]
    assert build(convo(nodes)).stop_intent
    assert not build(simple()).stop_intent


def test_an_image_only_assistant_message_is_a_final_with_counted_artifacts():
    nodes = [
        node("u1", "root", "user", "Make a meme about Mondays.", t=1),
        media_node("a1", "u1", "assistant", [IMAGE_PART, ""], t=2, end_turn=True),
    ]
    e = build(convo(nodes))
    a1 = next(m for m in e.messages if m.id == "a1")
    assert a1.final and a1.image_artifacts == 1 and a1.file_artifacts == 0
    assert e.final_id == "a1" and e.final_text == ""
    assert "SECRET-ASSET" not in json.dumps(e.to_mapping())


def test_artifacts_from_a_successful_tool_step_count_on_the_next_assistant_message():
    nodes = [
        node("u1", "root", "user", "Generate a logo.", t=1),
        media_node("tl", "u1", "tool", [IMAGE_PART, dict(IMAGE_PART)], t=2),
        node("a1", "tl", "assistant", "", t=3, end_turn=True),
    ]
    e = build(convo(nodes))
    a1 = next(m for m in e.messages if m.id == "a1")
    assert a1.final and a1.image_artifacts == 2


def test_a_failed_tool_step_leaves_no_artifacts():
    nodes = [
        node("u1", "root", "user", "Generate a logo.", t=1),
        media_node("tl", "u1", "tool", [IMAGE_PART], status="error", t=2),
        node("a1", "tl", "assistant", "", t=3, end_turn=True),
    ]
    a1 = next(m for m in build(convo(nodes)).messages if m.id == "a1")
    assert a1.artifacts == 0 and not a1.final


def test_attachments_are_counted_by_kind_never_named():
    meta = {
        "attachments": [
            {"id": "file-1", "name": "secret-plan.png", "mime_type": "image/png"},
            {"id": "file-2", "name": "secret-notes.pdf", "mime_type": "application/pdf"},
        ]
    }
    nodes = [
        node("u1", "root", "user", "Review these files.", t=1, meta=meta),
        node("a1", "u1", "assistant", "Reviewed both.", t=2, end_turn=True),
    ]
    e = build(convo(nodes))
    u1 = next(m for m in e.messages if m.id == "u1")
    assert (u1.image_artifacts, u1.file_artifacts) == (1, 1)
    assert "secret-" not in json.dumps(e.to_mapping())


def test_an_assistant_turn_that_ends_with_no_reply_is_flagged():
    nodes = [
        node("u1", "root", "user", "Run the report.", t=1),
        node("tl", "u1", "tool", "ok", ctype="execution_output", t=2),
        node("a1", "tl", "assistant", "", t=3, end_turn=True),
    ]
    e = build(convo(nodes))
    assert e.end_turn_without_reply and e.final_id is None
    assert not build(simple()).end_turn_without_reply
    done = nodes[:2] + [node("a1", "tl", "assistant", "Report is ready.", t=3, end_turn=True)]
    assert not build(convo(done)).end_turn_without_reply


def test_credential_looking_text_is_redacted_in_the_evidence():
    secret = "sk-" + "Q1w2E3r4" * 5
    nodes = [
        node("u1", "root", "user", f"Use my key {secret} to call the API.", t=1),
        node(
            "a1", "u1", "assistant", f"Called it with {secret}; result below.", t=2, end_turn=True
        ),
    ]
    e = build(convo(nodes))
    assert secret not in e.request_text and secret not in e.final_text
    assert "[REDACTED]" in e.request_text and "[REDACTED]" in e.final_text
    assert secret not in json.dumps(e.to_mapping())


def test_running_flag_from_guardian_becomes_the_progress_indicator():
    assert build(simple(), running=True).progress_indicator
    assert not build(simple()).progress_indicator


def test_list_metadata_overrides_identity_fields():
    gizmo = "g-p-0123456789abcdef0123456789abcdef"
    e = build(simple(), listed=listed(archived=True, project_id=gizmo, updated_at=NOW - 50))
    assert e.archived and e.project_id == gizmo and e.updated_at == NOW - 50


def test_conversation_id_mismatch_is_rejected():
    payload = simple()
    payload["conversation_id"] = "someone-elses"
    with pytest.raises(native.NativeError):
        build(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("mapping"),
        lambda p: p.pop("current_node"),
        lambda p: p.update(current_node="nope"),
        lambda p: p.update(mapping=[]),
        lambda p: p.update(mapping={}),
        lambda p: p["mapping"]["u1"].update(parent="a1"),  # cycle u1 -> a1 -> u1
        lambda p: p["mapping"]["a1"]["message"].update(author="user"),
        lambda p: p["mapping"]["a1"]["message"].update(create_time="soon"),
        lambda p: p["mapping"]["a1"]["message"].update(content=[]),
    ],
)
def test_malformed_conversation_payloads_are_a_typed_error(mutate):
    payload = simple()
    mutate(payload)
    with pytest.raises(native.NativeError):
        build(payload)


def test_non_dict_payload_is_a_typed_error():
    for bad in (None, [], "x", 3):
        with pytest.raises(native.NativeError):
            build(bad)


def test_oversized_mapping_is_a_typed_error():
    nodes = [
        node(f"m{i}", "root" if i == 0 else f"m{i - 1}", "user", "x", t=i)
        for i in range(native.MAX_NODES + 1)
    ]
    with pytest.raises(native.NativeError):
        build(convo(nodes))


def test_long_histories_keep_only_the_latest_messages_and_mark_the_cut():
    count = ev.MAX_MESSAGES + 50
    nodes = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        nodes.append(
            node(
                f"m{i}",
                "root" if i == 0 else f"m{i - 1}",
                role,
                f"text {i}",
                t=i,
                end_turn=True if role == "assistant" else None,
            )
        )
    e = build(convo(nodes))
    assert len(e.messages) <= ev.MAX_MESSAGES and e.window_cut


# ---- list pages -------------------------------------------------------------------------


def page(items, **extra):
    return {
        "items": items,
        "total": extra.pop("total", len(items)),
        "limit": 28,
        "offset": 0,
        **extra,
    }


def item(cid="c1", update="2026-10-27T10:06:38.123456Z", **kw):
    return {"id": cid, "title": "T", "update_time": update, **kw}


def test_list_page_parses_iso_and_epoch_times():
    p = native.parse_list_page(page([item("a"), item("b", update=NOW - 5)]), archived=False)
    assert [i.conversation_id for i in p.items] == ["a", "b"]
    assert p.items[0].updated_at == pytest.approx(1793095598.123456)
    assert p.items[1].updated_at == NOW - 5
    assert p.total == 2 and p.malformed == 0 and p.raw_count == 2


def test_list_page_marks_archived_and_project_ids():
    gizmo = "g-p-0123456789abcdef0123456789abcdef-slug"
    p = native.parse_list_page(
        page([item("a", is_archived=True, gizmo_id=gizmo), item("b", gizmo_id="g-weird")]),
        archived=False,
    )
    assert p.items[0].archived and p.items[0].project_id == "g-p-0123456789abcdef0123456789abcdef"
    assert not p.items[1].archived and p.items[1].project_id is None
    forced = native.parse_list_page(page([item("a")]), archived=True)
    assert forced.items[0].archived


@pytest.mark.parametrize(
    "bad",
    [
        {"id": ""},
        {"id": 5},
        {"id": "bad id"},
        {"id": "ok", "update_time": "not a time"},
        {"id": "ok", "update_time": float("nan")},
        {"id": "ok", "update_time": None},
        {"id": "ok", "update_time": True},
    ],
)
def test_malformed_list_items_are_counted_and_skipped(bad):
    base = {"title": "T", "update_time": NOW}
    p = native.parse_list_page(page([{**base, **bad}, item("good")]), archived=False)
    assert [i.conversation_id for i in p.items] == ["good"]
    assert p.malformed == 1 and p.raw_count == 2


@pytest.mark.parametrize("payload", [None, [], {"items": "x"}, {"nope": []}, {"items": [1, 2]}])
def test_malformed_list_pages_are_a_typed_error_or_all_malformed(payload):
    try:
        p = native.parse_list_page(payload, archived=False)
    except native.NativeError:
        return
    assert p.items == ()


def test_total_is_optional_and_validated():
    assert native.parse_list_page({"items": []}, archived=False).total is None
    assert native.parse_list_page({"items": [], "total": "many"}, archived=False).total is None
    assert native.parse_list_page({"items": [], "total": -3}, archived=False).total is None
