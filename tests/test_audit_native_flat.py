"""The live FLAT ``messages`` wire shape and the legacy ``mapping`` shape parse to one evidence.

The plural endpoint ``/backend-api/conversations/<id>?num_turns=N`` returns a flat, chronological
``messages`` list (no ``mapping``). Its order is trusted only when the reader proved the format
(``native_format`` marker) and the list passes the order checks; the singular full-mapping endpoint
stays a fallback. Fixtures are tiny hand-invented payloads with the live key set, no real text.
"""

from __future__ import annotations

import copy
import json

import pytest

from pmlib.chatgpt_control import audit_native as native
from pmlib.chatgpt_control import audit_reader, audit_urls
from tests.audit_support import ACCOUNT, CONVERSATION, NOW
from tests.native_support import (
    as_flat,
    as_mapping,
    both,
    flat_payload,
    load,
    mapping_payload,
    message,
)

GO = "\u0433\u043e"  # the Russian word for "go"
IMAGE_PART = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"}


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


def turn(*extra):
    return [
        message("u1", "user", "Install the package and verify it.", t=0),
        *extra,
    ]


FINAL = message("a1", "assistant", "Installed and verified.", t=10, end_turn=True, channel="final")


# --- both shapes, one evidence ------------------------------------------------------------
def test_the_fixture_files_have_the_live_wire_shapes():
    flat, full = load("native_flat_turn.json"), load("native_mapping_turn.json")
    assert "mapping" not in flat and isinstance(flat["messages"], list)
    assert flat["current_node"] == flat["messages"][-1]["id"]
    assert set(flat["page_info"]) == {
        "start_cursor",
        "end_cursor",
        "has_previous_page",
        "has_next_page",
    }
    assert "messages" not in full and isinstance(full["mapping"], dict)
    assert set(flat["messages"][0]) == {
        "id",
        "author",
        "create_time",
        "update_time",
        "content",
        "status",
        "end_turn",
        "weight",
        "metadata",
        "recipient",
        "channel",
    }


def test_fixture_flat_and_mapping_payloads_parse_to_identical_evidence():
    flat = as_flat(load("native_flat_turn.json"))
    full = as_mapping(load("native_mapping_turn.json"))
    from_flat, from_full = build(flat), build(full)
    assert from_flat == from_full
    assert [(m.id, m.role, m.final, m.recap) for m in from_flat.messages] == [
        ("msg-user", "user", False, False),
        ("msg-comment", "assistant", False, False),
        ("msg-recap", "assistant", False, True),
        ("msg-final", "assistant", True, False),
    ]
    assert from_flat.request_id == "msg-user"
    assert from_flat.final_id == "msg-final"
    assert from_flat.final_text == "The checklist is drafted and all 12 checks pass."
    assert not from_flat.window_cut and not from_flat.context_truncated
    assert next(m for m in from_flat.messages if m.id == "msg-recap").tool_success


def test_the_unmarked_legacy_mapping_payload_still_parses():
    payload = mapping_payload(turn(FINAL))
    assert "native_format" not in payload
    assert build(payload) == build(as_mapping(payload))


def test_hidden_content_of_the_flat_shape_never_enters_the_evidence():
    flat = as_flat(load("native_flat_turn.json"))
    wire = json.dumps(build(flat).to_mapping())
    for hidden in ("neutral planning note", "run-checks", "12 passed", "Thought for 20s"):
        assert hidden not in wire
    assert "native_format" not in wire


# --- the format proof --------------------------------------------------------------------
def test_a_flat_payload_without_the_marker_is_rejected():
    with pytest.raises(native.NativeError, match="format"):
        build(flat_payload(turn(FINAL)))


@pytest.mark.parametrize(
    "payload",
    [
        as_mapping(flat_payload(turn(FINAL))),  # flat shape claiming to be a mapping
        as_flat(mapping_payload(turn(FINAL))),  # mapping shape claiming to be flat
        {**flat_payload(turn(FINAL)), "native_format": "hybrid"},  # unknown format
        as_flat({**mapping_payload(turn(FINAL)), "messages": turn(FINAL)}),  # both shapes
        as_flat({"conversation_id": CONVERSATION, "title": "t"}),  # neither shape
    ],
    ids=["flat-as-mapping", "mapping-as-flat", "unknown-format", "both-shapes", "neither-shape"],
)
def test_a_marker_that_contradicts_the_payload_is_rejected(payload):
    with pytest.raises(native.NativeError):
        build(payload)


def test_the_reader_marker_overrides_whatever_the_server_sent():
    spoofed = flat_payload(turn(FINAL), native_format="mapping")
    marked = audit_reader.mark_native(spoofed, audit_urls.NATIVE_FLAT)
    assert marked["native_format"] == "flat"
    assert spoofed["native_format"] == "mapping"  # the input is never mutated
    assert build(marked).final_id == "a1"


# --- order checks of the flat list --------------------------------------------------------
def test_the_latest_user_is_the_last_user_in_list_order_and_a_final_after_it_is_current():
    messages = [
        message("u1", "user", "First question about the cache.", t=0),
        message("a1", "assistant", "Answer one.", t=5, end_turn=True, channel="final"),
        message("u2", "user", "Second question about the index.", t=20),
        message("a2", "assistant", "Answer two.", t=25, end_turn=True, channel="final"),
    ]
    e = build(as_flat(flat_payload(messages)))
    assert e.request_id == "u2" and e.final_id == "a2"
    after = [m.id for m in e.messages[e.messages.index(e.last_user) + 1 :]]
    assert after == ["a2"]


def test_a_final_before_the_latest_user_does_not_answer_it():
    messages = [
        message("u1", "user", "Clean up the repo.", t=0),
        message("a1", "assistant", "The permission has expired.", t=5, end_turn=True),
        message("u2", "user", "Try again please.", t=20),
    ]
    e = build(as_flat(flat_payload(messages)))
    assert e.final_id == "a1" and not e.permission_expired


def test_timestamp_regressions_inside_one_turn_are_tolerated():
    messages = [
        message("u1", "user", "Run the checks.", t=0),
        message("a1", "assistant", "Looking.", t=30, end_turn=False, channel="commentary"),
        message("a2", "assistant", "Done: all checks pass.", t=12, end_turn=True, channel="final"),
    ]
    assert build(as_flat(flat_payload(messages))).final_id == "a2"


def _swap_ids(payload):
    payload["messages"][1]["id"] = payload["messages"][0]["id"]


def _reverse(payload):
    payload["messages"].reverse()


def _last_not_current(payload):
    payload["current_node"] = payload["messages"][0]["id"]


def _no_current(payload):
    del payload["current_node"]


def _user_goes_back(payload):
    ms = payload["messages"]
    ms[2]["create_time"] = ms[0]["create_time"] - 1


def _huge_regression(payload):
    payload["messages"][-1]["create_time"] -= 2 * native.FLAT_MAX_SKEW


def _parent_in_the_future(payload):
    ms = payload["messages"]
    ms[1]["metadata"]["parent_id"] = ms[3]["id"]


def _parent_is_self(payload):
    payload["messages"][1]["metadata"]["parent_id"] = payload["messages"][1]["id"]


@pytest.mark.parametrize(
    "mutate",
    [
        _swap_ids,
        _reverse,
        _last_not_current,
        _no_current,
        _user_goes_back,
        _huge_regression,
        _parent_in_the_future,
        _parent_is_self,
    ],
)
def test_flat_order_violations_are_rejected(mutate):
    messages = [
        message("u1", "user", "First question about the cache.", t=0),
        message("a1", "assistant", "Answer one.", t=5, end_turn=True, channel="final"),
        message("u2", "user", "Second question about the index.", t=20),
        message("a2", "assistant", "Answer two.", t=25, end_turn=True, channel="final"),
    ]
    payload = as_flat(flat_payload(messages))
    mutate(payload)
    with pytest.raises(native.NativeError):
        build(payload)


def test_a_parent_outside_the_window_is_the_normal_start_of_a_window():
    messages = turn(FINAL)
    messages[0]["metadata"]["parent_id"] = "message-before-the-window"
    assert build(as_flat(flat_payload(messages, previous_page=True))).final_id == "a1"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(messages=[]),
        lambda p: p.update(messages="nope"),
        lambda p: p.update(messages=[1, 2]),
        lambda p: p["messages"][0].pop("id"),
        lambda p: p["messages"][0].update(id="has space"),
        lambda p: p["messages"][0].update(author="user"),
        lambda p: p["messages"][0].update(content="text"),
        lambda p: p["messages"][0].update(create_time=float("nan")),
        lambda p: p["messages"][0].update(create_time="yesterday"),
        lambda p: p.update(current_node=7),
        lambda p: p.update(conversation_id="someone-elses"),
        lambda p: p.pop("conversation_id"),
        lambda p: p["page_info"].update(has_previous_page="yes"),
        lambda p: p.update(page_info=[]),
        lambda p: p["messages"][0]["metadata"].update(parent_id=5),
    ],
    ids=[
        "empty",
        "not-a-list",
        "not-dicts",
        "no-id",
        "bad-id",
        "bad-author",
        "bad-content",
        "nan-time",
        "string-time",
        "bad-current",
        "wrong-conversation",
        "no-conversation",
        "bad-page-flag",
        "bad-page-info",
        "bad-parent",
    ],
)
def test_malformed_flat_payloads_are_a_typed_error(mutate):
    payload = as_flat(flat_payload(turn(FINAL)))
    mutate(payload)
    with pytest.raises(native.NativeError):
        build(payload)


def test_an_oversized_flat_list_is_a_typed_error():
    messages = [
        message(f"m{i}", "user" if i % 2 == 0 else "assistant", "x", t=i)
        for i in range(native.MAX_NODES + 1)
    ]
    with pytest.raises(native.NativeError, match="too large"):
        build(as_flat(flat_payload(messages)))


def test_a_marked_mapping_cycle_is_still_rejected():
    payload = as_mapping(mapping_payload(turn(FINAL)))
    payload["mapping"]["u1"]["parent"] = "a1"
    with pytest.raises(native.NativeError):
        build(payload)


def test_a_marked_mapping_over_the_node_cap_is_still_rejected():
    messages = [message(f"m{i}", "user", "x", t=i) for i in range(native.MAX_NODES + 1)]
    with pytest.raises(native.NativeError, match="too large"):
        build(as_mapping(mapping_payload(messages)))


# --- the window ---------------------------------------------------------------------------
def test_a_previous_page_marks_the_window_cut_but_a_request_inside_keeps_the_context():
    e = build(as_flat(flat_payload(turn(FINAL), previous_page=True)))
    assert e.window_cut and not e.context_truncated and e.request_id == "u1"


def test_a_bare_go_in_a_cut_window_has_no_goal_and_the_context_is_truncated():
    messages = [
        message("u1", "user", GO, t=0),
        message("a1", "assistant", "Working on it.", t=5, end_turn=True, channel="final"),
    ]
    e = build(as_flat(flat_payload(messages, previous_page=True)))
    assert e.request_id is None and e.window_cut and e.context_truncated


def test_a_bare_go_with_the_prior_goal_inside_the_window_resolves_it():
    messages = [
        message("u1", "user", "Install and verify the package.", t=0),
        message("a1", "assistant", "Installed; verifying next.", t=5, end_turn=False),
        message("u2", "user", GO, t=20),
        message("a2", "assistant", "Verified.", t=25, end_turn=True, channel="final"),
    ]
    e = build(as_flat(flat_payload(messages, previous_page=True)))
    assert e.request_id == "u1" and not e.context_truncated


def test_a_missing_page_info_is_treated_as_a_cut_window():
    payload = as_flat(flat_payload(turn(FINAL)))
    del payload["page_info"]
    assert build(payload).window_cut


# --- completion structure -----------------------------------------------------------------
def test_a_recap_is_not_a_final_answer_even_when_it_ended_the_turn_successfully():
    messages = turn(
        message("th", "assistant", "plan", ctype="thoughts", t=3, end_turn=False),
        message("rc", "assistant", "Thought for 9s", ctype="reasoning_recap", t=9, end_turn=True),
    )
    flat, full = both(messages)
    for payload in (flat, full):
        e = build(payload)
        last = e.messages[-1]
        assert last.recap and last.end_turn is True and last.status == "finished_successfully"
        assert not last.final and e.final_id is None and e.final_text == ""
        assert not e.end_turn_without_reply
    assert build(flat) == build(full)


def test_a_recap_after_a_commentary_leaves_the_commentary_a_non_final():
    messages = turn(
        message("c1", "assistant", "Checking.", t=3, end_turn=False, channel="commentary"),
        message("rc", "assistant", "Thought for 9s", ctype="reasoning_recap", t=9, end_turn=True),
    )
    e = build(as_flat(flat_payload(messages)))
    assert e.final_id is None and [m.final for m in e.messages] == [False, False, False]


def test_an_image_only_final_keeps_its_artifact_counts_in_both_shapes():
    messages = turn(
        message(
            "a1",
            "assistant",
            ctype="multimodal_text",
            parts=[IMAGE_PART, ""],
            t=10,
            end_turn=True,
            channel="final",
        )
    )
    flat, full = both(messages)
    for payload in (flat, full):
        e = build(payload)
        final = e.messages[-1]
        assert final.final and final.image_artifacts == 1 and final.artifacts == 1
        assert e.final_id == "a1" and e.final_text == ""
    assert build(flat) == build(full)


def test_a_successful_tool_image_counts_on_the_next_assistant_message():
    messages = turn(
        message(
            "tl",
            "tool",
            ctype="multimodal_text",
            parts=[IMAGE_PART, dict(IMAGE_PART)],
            t=4,
        ),
        message("a1", "assistant", "", t=10, end_turn=True, channel="final"),
    )
    e = build(as_flat(flat_payload(messages)))
    assert e.messages[-1].image_artifacts == 2 and e.messages[-1].final


def test_tool_messages_stay_out_of_the_refs_but_mark_the_next_assistant_message():
    messages = turn(
        message("tl", "tool", "TOOL-OUTPUT", ctype="execution_output", t=4),
        message("a1", "assistant", "Done.", t=10, end_turn=True, channel="final"),
    )
    e = build(as_flat(flat_payload(messages)))
    assert [m.id for m in e.messages] == ["u1", "a1"]
    assert e.messages[-1].tool_success
    assert "TOOL-OUTPUT" not in json.dumps(e.to_mapping())


def test_a_flat_conversation_that_ends_on_a_tool_step_is_not_a_finished_reply():
    messages = turn(
        message("th", "assistant", "plan", ctype="thoughts", t=3, end_turn=False),
        message("tl", "tool", "partial output", ctype="execution_output", t=4),
    )
    e = build(as_flat(flat_payload(messages)))
    assert e.final_id is None and not e.end_turn_without_reply
    assert [m.id for m in e.messages] == ["u1"]


def test_a_flat_assistant_turn_that_ended_with_no_reply_is_flagged():
    messages = turn(
        message("tl", "tool", "ok", ctype="execution_output", t=4),
        message("a1", "assistant", "", t=10, end_turn=True),
    )
    flat, full = both(messages)
    assert build(flat).end_turn_without_reply and build(flat) == build(full)


def test_the_flat_payload_is_not_mutated():
    payload = as_flat(flat_payload(turn(FINAL)))
    before = copy.deepcopy(payload)
    build(payload)
    assert payload == before
