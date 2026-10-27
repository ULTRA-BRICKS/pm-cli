"""Typed audit evidence: strict construction, bounded text, no hidden/credential carriers."""

from __future__ import annotations

import json
import math

import pytest

from pmlib.chatgpt_control import audit_evidence as ev
from tests.audit_support import CONVERSATION, NOW, URL, evidence, msg, replace


def test_baseline_evidence_exposes_its_last_messages():
    e = evidence()
    assert e.last_user.id == "u1" and e.last_final.id == "a1"
    assert e.schema_version == ev.SCHEMA_VERSION


def test_evidence_is_frozen():
    e = evidence()
    with pytest.raises(AttributeError):
        e.account = "glide.vc"  # type: ignore[misc]


def test_round_trips_through_a_mapping_and_json():
    e = evidence()
    wire = json.loads(json.dumps(e.to_mapping(), allow_nan=False))
    assert ev.ConversationEvidence.from_mapping(wire) == e


def test_digest_ignores_read_times_but_not_content():
    base = evidence()
    assert base.digest() == replace(base, read_at=NOW, verified_at=NOW).digest()
    assert base.digest() != replace(base, final_text="a different final answer").digest()
    assert base.digest() != replace(base, progress_indicator=True).digest()


@pytest.mark.parametrize(
    "overrides",
    [
        {"account": ""},
        {"account": "Home Account"},
        {"observed_account": 3},
        {"conversation_id": "bad id"},
        {"conversation_id": ""},
        {"url": "https://evil.example/c/x"},
        {"url": "https://chatgpt.com/c/other-id"},
        {"project_id": "g-p-nothex"},
        {"updated_at": float("nan")},
        {"updated_at": float("inf")},
        {"updated_at": True},
        {"archived": 1},
        {"read_at": float("nan")},
        {"read_at": None},
        {"verified_at": "now"},
        {"messages": []},
        {"messages": [msg("u1", "user", NOW)]},
        {"messages": (msg("u1", "user", NOW), msg("u1", "assistant", NOW + 1))},
        {"messages": (msg("u1", "user", NOW),) * 2},
        {"request_text": "x" * (ev.MAX_TEXT + 1)},
        {"final_text": "x" * (ev.MAX_TEXT + 1)},
        {"request_text": "bell\x07char"},
        {"final_text": "esc\x1b[31mred"},
        {"request_text": None},
        {"request_truncated": "no"},
        {"final_id": "missing-id"},
        {"request_id": "a1"},
        {"final_id": "u1"},
        {"pending_approval": None},
        {"progress_indicator": "yes"},
        {"window_cut": 0},
        {"continuation_announced": 1},
        {"login_required": 1},
        {"stop_intent": "yes"},
        {"end_turn_without_reply": None},
        {"unfinished_cue": "whatever"},
        {"unfinished_cue": None},
        {"doubt_cue": "maybe"},
    ],
)
def test_malformed_evidence_is_a_typed_error_never_partial(overrides):
    with pytest.raises(ev.EvidenceError):
        evidence(**overrides)


@pytest.mark.parametrize(
    "bad",
    [
        {"id": ""},
        {"id": "x" * 200},
        {"role": "system"},
        {"role": "tool"},
        {"created_at": math.nan},
        {"created_at": "yesterday"},
        {"status": ""},
        {"status": "In Progress"},
        {"end_turn": "true"},
        {"final": 1},
        {"tool_success": None},
        {"turn_uuid": 5},
        {"turn_uuid": "x" * 200},
        {"image_artifacts": -1},
        {"image_artifacts": True},
        {"file_artifacts": 1.5},
        {"file_artifacts": 10**6},
    ],
)
def test_malformed_message_refs_are_rejected(bad):
    fields = {
        "id": "m",
        "role": "user",
        "created_at": NOW,
        "turn_uuid": "t",
        "status": "finished_successfully",
        "end_turn": None,
        "final": False,
        "tool_success": False,
        "recap": False,
    }
    with pytest.raises(ev.EvidenceError):
        ev.MessageRef(**{**fields, **bad})


def test_text_without_its_message_is_rejected():
    with pytest.raises(ev.EvidenceError):
        evidence(messages=(msg("a1", "assistant", NOW - 650),))  # request_id u1 missing
    with pytest.raises(ev.EvidenceError):
        evidence(final_id=None)  # final text without a final message id


def test_empty_request_and_final_are_allowed_when_their_ids_are_absent():
    e = evidence(final_id=None, final_text="", messages=(msg("u1", "user", NOW - 700),))
    assert e.last_final is None


def test_too_many_messages_are_rejected():
    many = tuple(msg(f"m{i}", "user", NOW) for i in range(ev.MAX_MESSAGES + 1))
    with pytest.raises(ev.EvidenceError):
        evidence(messages=many, request_id="m0", final_id=None, final_text="")


@pytest.mark.parametrize(
    "key",
    [
        "cookie",
        "cookies",
        "authorization",
        "bearer",
        "access_token",
        "accessToken",
        "token",
        "password",
        "reasoning",
        "thoughts",
        "chain_of_thought",
        "reasoning_content",
        "tool_credentials",
    ],
)
def test_credential_and_hidden_reasoning_keys_are_refused(key):
    wire = evidence().to_mapping()
    wire[key] = "secret-value"
    with pytest.raises(ev.EvidenceError, match="forbidden"):
        ev.ConversationEvidence.from_mapping(wire)
    wire = evidence().to_mapping()
    wire["messages"][0][key] = "secret-value"
    with pytest.raises(ev.EvidenceError, match="forbidden"):
        ev.ConversationEvidence.from_mapping(wire)


def test_unknown_keys_and_missing_keys_are_refused():
    wire = evidence().to_mapping()
    wire["surprise"] = 1
    with pytest.raises(ev.EvidenceError, match="unknown"):
        ev.ConversationEvidence.from_mapping(wire)
    wire = evidence().to_mapping()
    del wire["read_at"]
    with pytest.raises(ev.EvidenceError, match="missing"):
        ev.ConversationEvidence.from_mapping(wire)
    with pytest.raises(ev.EvidenceError):
        ev.ConversationEvidence.from_mapping("not a mapping")  # type: ignore[arg-type]


def test_schema_version_mismatch_is_refused():
    wire = evidence().to_mapping()
    wire["schema_version"] = 99
    with pytest.raises(ev.EvidenceError, match="schema"):
        ev.ConversationEvidence.from_mapping(wire)


def test_serialized_evidence_carries_no_other_role_text():
    wire = json.dumps(evidence().to_mapping())
    assert "role" in wire
    for message in json.loads(wire)["messages"]:
        assert set(message) == {
            "id",
            "role",
            "created_at",
            "turn_uuid",
            "status",
            "end_turn",
            "final",
            "tool_success",
            "recap",
            "image_artifacts",
            "file_artifacts",
        }
    assert CONVERSATION in wire and URL in wire


def test_clean_text_keeps_newlines_strips_controls_and_reports_truncation():
    text, cut = ev.bounded_text("line one\nline\ttwo\x00\x07 end", 100)
    assert text == "line one\nline\ttwo end" and cut is False
    text, cut = ev.bounded_text("a" * 50, 10)
    assert text == "a" * 10 and cut is True
    assert ev.bounded_text("", 10) == ("", False)


def test_trivial_acknowledgements_are_recognized_in_english_and_russian():
    assert ev.is_trivial_ack("go")
    assert ev.is_trivial_ack("  Go! ")
    assert ev.is_trivial_ack("continue")
    assert ev.is_trivial_ack("ok")
    assert ev.is_trivial_ack("\u0433\u043e")  # Russian "go"
    assert ev.is_trivial_ack("\u0413\u043e\u0442\u043e\u0432\u043e")  # Russian "done"
    assert not ev.is_trivial_ack("go ahead and summarize the three options")
    assert not ev.is_trivial_ack("Reply with ONLY the control word.")
    assert not ev.is_trivial_ack("")


def test_fingerprint_binds_the_latest_request_and_final_but_not_the_clock():
    e = evidence()
    later = replace(e, read_at=e.read_at + 60, verified_at=e.verified_at + 60)
    assert later.fingerprint() == e.fingerprint()
    assert replace(e, final_text="A different final answer.").fingerprint() != e.fingerprint()
    assert replace(e, request_text="Another request entirely.").fingerprint() != e.fingerprint()
    other = replace(e, account="glide.vc", observed_account="glide.vc")
    assert other.fingerprint() != e.fingerprint()
    msgs = (*e.messages, msg("u2", "user", NOW - 10))
    assert replace(e, messages=msgs).fingerprint() != e.fingerprint()


def test_fingerprint_carries_no_chat_text():
    e = evidence(final_text="SECRET-FINAL-TEXT", request_text="SECRET-REQUEST-TEXT")
    assert "SECRET" not in e.fingerprint()
    assert len(e.fingerprint()) == 64


def test_new_evidence_fields_default_to_the_quiet_state_and_round_trip():
    e = evidence()
    assert (e.login_required, e.stop_intent, e.end_turn_without_reply) == (False, False, False)
    assert (e.unfinished_cue, e.doubt_cue) == ("", "")
    assert all(m.image_artifacts == 0 and m.file_artifacts == 0 for m in e.messages)
    flagged = evidence(
        login_required=True,
        stop_intent=True,
        end_turn_without_reply=True,
        unfinished_cue="plan-only",
        doubt_cue="plan-like",
        continuation_announced=True,
        messages=(
            msg("u1", "user", NOW - 700),
            msg("a1", "assistant", NOW - 650, image_artifacts=2),
        ),
    )
    wire = json.loads(json.dumps(flagged.to_mapping(), allow_nan=False))
    assert ev.ConversationEvidence.from_mapping(wire) == flagged
    assert flagged.last_final.artifacts == 2


def test_artifact_counts_change_the_fingerprint_and_travel_as_counts_only():
    plain = evidence()
    msgs = (msg("u1", "user", NOW - 700), msg("a1", "assistant", NOW - 650, image_artifacts=1))
    with_image = replace(plain, messages=msgs)
    assert with_image.fingerprint() != plain.fingerprint()
    carried = with_image.to_mapping()["messages"][1]
    assert (carried["image_artifacts"], carried["file_artifacts"]) == (1, 0)
