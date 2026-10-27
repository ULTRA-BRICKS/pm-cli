"""Native payload -> evidence -> classifier (real provider wire) -> verdict, end to end.

Each scenario is a tiny hand-invented conversation in BOTH native wire shapes (the live flat
``messages`` list and the legacy ``mapping``), read through ``audit_native.build_evidence`` and
judged by ``audit_decision.decide`` with a ``LayaClassifier`` talking to a fake provider that emits
the exact real 0.23.1 wire (top-level ``meta``, explicit route, ``capabilities`` first). Nothing
here is real chat text.
"""

from __future__ import annotations

import json

import pytest

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_decision as dec
from pmlib.chatgpt_control import audit_native as native
from tests.audit_support import ACCOUNT, CONVERSATION, NOW
from tests.laya_support import GOOD_ANSWERS, FakeLaya, encode, over_length_reply, predict_reply
from tests.native_support import as_flat, as_mapping, flat_payload, mapping_payload, message

# Russian sentences as escapes so the file stays ASCII.
RU_REQUEST = (
    "\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438 \u043f\u0430\u043a\u0435\u0442 "
    "\u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u044c \u0435\u0433\u043e."
)
RU_FINAL = (
    "\u041f\u0430\u043a\u0435\u0442 \u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d "
    "\u0438 \u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d: \u0432\u0441\u0435 "
    "\u0442\u0435\u0441\u0442\u044b \u043f\u0440\u043e\u0448\u043b\u0438."
)
RU_GO = "\u0433\u043e"
IMAGE_PART = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"}
SHAPES = ("flat", "mapping")


@pytest.fixture()
def provider(tmp_path):
    fake = FakeLaya(tmp_path / "state" / "task-cli" / "classify", "laya-0.23.1.sock")
    yield fake
    fake.close()


def listed(**kw) -> native.ListedConversation:
    base = {
        "conversation_id": CONVERSATION,
        "updated_at": NOW - 300,
        "archived": False,
        "project_id": None,
    }
    base.update(kw)
    return native.ListedConversation(**base)


def payload_of(shape: str, messages, **top) -> dict:
    if shape == "flat":
        return as_flat(flat_payload(messages, **top))
    top.pop("previous_page", None)
    return as_mapping(mapping_payload(messages, **top))


def evidence_of(shape, messages, *, archived=False, running=False, **top):
    return native.build_evidence(
        payload_of(shape, messages, **top),
        listed=listed(archived=archived),
        account=ACCOUNT,
        observed_account=ACCOUNT,
        now=NOW,
        running=running,
    )


def judge(provider, shape, messages, **kw):
    """The verdict and the evidence for one scenario, classifying through the fake provider."""
    top = {k: kw.pop(k) for k in ("previous_page",) if k in kw}
    e = evidence_of(shape, messages, **top, **kw)
    classifier = clf.LayaClassifier(socket_path=provider.path)
    return e, dec.decide(e, lambda: classifier.classify(e), now=NOW)


def final(text, **kw):
    kw.setdefault("t", 100)
    return message("a1", "assistant", text, end_turn=True, channel="final", **kw)


def sent(provider, index=-1) -> dict:
    return json.loads(provider.requests[index])


@pytest.mark.parametrize("shape", SHAPES)
def test_a_cyrillic_conversation_is_routed_to_the_multilingual_model(provider, shape):
    messages = [message("u1", "user", RU_REQUEST, t=0), final(RU_FINAL)]
    _, v = judge(provider, shape, messages)
    request = sent(provider)
    assert request["model"] == "multilingual" and request["max_len"] == 2048
    assert (v.task_state, v.recovery_state) == ("done", "not_needed")
    assert v.model == "convaiinnovations/laya-multilingual@2048"
    assert v.classifier_meta == {
        "checkpoint": "convaiinnovations/laya-multilingual",
        "max_len": 2048,
        "truncated": False,
    }
    assert provider.capability_requests == 1  # capabilities are read before predict


@pytest.mark.parametrize("shape", SHAPES)
def test_an_english_conversation_is_routed_to_the_english_model(provider, shape):
    messages = [
        message("u1", "user", "Summarize the three options and recommend one.", t=0),
        final("Option B is the best trade-off: it is cheapest and ships first."),
    ]
    _, v = judge(provider, shape, messages)
    assert sent(provider)["model"] == "english" and sent(provider)["max_len"] == 512
    assert v.task_state == "done" and v.model == "convaiinnovations/laya@512"


@pytest.mark.parametrize("shape", SHAPES)
def test_a_truncated_provider_reply_is_unknown_never_done(provider, shape):
    provider.reply = encode(over_length_reply())  # the real over-length reply, meta.truncated true
    messages = [message("u1", "user", RU_REQUEST, t=0), final(RU_FINAL)]
    _, v = judge(provider, shape, messages)
    assert v.task_state == "unknown" and "classifier-invalid" in v.reasons
    assert v.classifier_status == "invalid" and v.classifier_meta == {}
    assert v.recovery_state == "hold"


@pytest.mark.parametrize("shape", SHAPES)
def test_meta_only_inside_result_is_not_accepted_end_to_end(provider, shape):
    reply = predict_reply(route="english", max_len=512)
    reply["result"]["meta"] = reply.pop("meta")
    provider.reply = encode(reply)
    messages = [
        message("u1", "user", "Summarize the three options and recommend one.", t=0),
        final("Option B is the best trade-off: it is cheapest and ships first."),
    ]
    _, v = judge(provider, shape, messages)
    assert v.task_state == "unknown" and v.classifier_status == "invalid"


@pytest.mark.parametrize("shape", SHAPES)
def test_an_old_abandoned_in_progress_message_does_not_veto_a_new_completed_final(provider, shape):
    messages = [
        message("u0", "user", "Draft the migration notes.", t=-500),
        message("a0", "assistant", "Working on it", t=-490, status="in_progress"),
        message("u1", "user", "Summarize the three options and recommend one.", t=0),
        final("Option B is the best trade-off: it is cheapest and ships first."),
    ]
    e, v = judge(provider, shape, messages)
    assert e.last_user.id == "u1" and e.last_final.id == "a1"
    assert (v.response_state, v.task_state) == ("finished", "done")
    assert "in-progress-message" not in v.reasons


@pytest.mark.parametrize("shape", SHAPES)
def test_a_current_generation_still_vetoes_even_with_an_older_completed_final(provider, shape):
    messages = [
        message("u0", "user", "Draft the migration notes.", t=-500),
        message("a0", "assistant", "Done, notes attached.", t=-490, end_turn=True, channel="final"),
        message("u1", "user", "Now summarize the three options.", t=0),
        message("a1", "assistant", "Working on it", t=10, status="in_progress"),
    ]
    _, v = judge(provider, shape, messages)
    assert v.task_state == "working" and v.recovery_state == "none"
    assert provider.requests == []  # a running turn never costs a model call


@pytest.mark.parametrize("shape", SHAPES)
def test_a_bare_go_with_the_goal_outside_the_window_is_unknown_not_done(provider, shape):
    messages = [
        message("u1", "user", RU_GO, t=0),
        final(RU_FINAL, t=60),
    ]
    e, v = judge(provider, shape, messages, previous_page=True)
    assert e.request_id is None
    assert v.task_state == "unknown" and provider.requests == []
    # A cut flat window is `truncated-context`; a complete mapping simply has no goal at all.
    assert {"truncated-context", "no-substantive-request"} & set(v.reasons)
    assert e.context_truncated == (shape == "flat")


@pytest.mark.parametrize("shape", SHAPES)
def test_a_bare_go_with_the_goal_in_the_expanded_window_judges_the_goal(provider, shape):
    messages = [
        message("u0", "user", RU_REQUEST, t=-200),
        message("a0", "assistant", "I will start with the installation.", t=-190),
        message("u1", "user", RU_GO, t=0),
        final(RU_FINAL, t=60),
    ]
    e, v = judge(provider, shape, messages, previous_page=True)
    assert e.request_id == "u0" and not e.context_truncated
    assert v.task_state == "done" and sent(provider)["model"] == "multilingual"


@pytest.mark.parametrize("shape", SHAPES)
def test_an_image_only_final_keeps_its_artifact_and_is_never_incomplete(provider, shape):
    image_final = message(
        "a1",
        "assistant",
        None,
        ctype="multimodal_text",
        parts=[IMAGE_PART],
        t=100,
        end_turn=True,
        channel="final",
    )
    messages = [message("u1", "user", "Draw a cat meme and attach the image.", t=0), image_final]
    e, v = judge(provider, shape, messages)
    assert e.final_text == "" and e.last_final.image_artifacts >= 1
    assert v.task_state == "unknown" and "artifact-only-final" in v.reasons
    assert v.task_state != "incomplete" and provider.requests == []


@pytest.mark.parametrize("shape", SHAPES)
def test_an_archived_handoff_is_done_and_never_resumed(provider, shape):
    handoff = (
        "Handoff: the work is paused here. Done so far: the parser and its tests. Open items: "
        "the exporter and the docs. Resume from the exporter branch when you are back."
    )
    messages = [
        message("u1", "user", "Please write the handoff and wrap up for today.", t=0),
        final(handoff),
    ]
    _, v = judge(provider, shape, messages, archived=True)
    assert v.task_state == "done" and v.recovery_state == "not_needed"
    assert "stop-intent-honored" in v.reasons


@pytest.mark.parametrize("shape", SHAPES)
def test_an_archived_unfinished_chat_is_reported_but_never_continuable(provider, shape):
    messages = [
        message("u1", "user", "Install the package and verify it.", t=0),
        final("I will install the package and verify it in the next step."),
    ]
    _, v = judge(provider, shape, messages, archived=True)
    assert v.task_state == "incomplete"
    assert v.recovery_state == "hold" and "archived-no-resume" in v.reasons


@pytest.mark.parametrize("shape", SHAPES)
def test_a_current_pending_permission_is_blocked_without_asking_the_model(provider, shape):
    messages = [
        message("u1", "user", "Delete the old tasks listed in the report.", t=0),
        final("The deletion is prepared. I am waiting for your approval before removing anything."),
    ]
    _, v = judge(provider, shape, messages)
    assert (v.task_state, v.recovery_state) == ("blocked", "awaiting_owner")
    assert "pending-approval" in v.reasons and provider.requests == []


@pytest.mark.parametrize("shape", SHAPES)
def test_an_expired_permission_is_blocked(provider, shape):
    messages = [
        message("u1", "user", "Delete the old tasks listed in the report.", t=0),
        final("The permission expired, so the deletion did not run."),
    ]
    _, v = judge(provider, shape, messages)
    assert v.task_state == "blocked" and "permission-expired" in v.reasons


@pytest.mark.parametrize("shape", SHAPES)
def test_a_high_blocked_score_alone_never_creates_blocked(provider, shape):
    answers = json.loads(json.dumps(GOOD_ANSWERS))
    answers["blocked_on_human"]["noul"] = 0.99
    provider.reply = encode(predict_reply(answers, route="english", max_len=512))
    messages = [
        message("u1", "user", "What is 12*8?", t=0),
        final("12 * 8 = 96. If you want, I can also show the steps."),
    ]
    _, v = judge(provider, shape, messages)
    assert v.task_state == "unknown" and "laya-blocked-advisory" in v.reasons
    assert v.task_state != "blocked"


@pytest.mark.parametrize("shape", SHAPES)
def test_a_reasoning_recap_without_a_final_is_never_done(provider, shape):
    recap = message(
        "a1",
        "assistant",
        "Thought for 2m",
        ctype="reasoning_recap",
        t=100,
        end_turn=True,
        meta={"reasoning_status": "is_reasoning"},
    )
    messages = [message("u1", "user", "Install the package and verify it.", t=0), recap]
    e, v = judge(provider, shape, messages)
    assert e.last_final is None or e.last_final.recap is False
    assert v.task_state in ("incomplete", "unknown") and v.task_state != "done"
    assert provider.requests == []


@pytest.mark.parametrize("shape", SHAPES)
def test_both_shapes_give_the_same_verdict_for_the_same_conversation(provider, shape):
    messages = [message("u1", "user", RU_REQUEST, t=0), final(RU_FINAL)]
    flat_e, flat_v = judge(provider, "flat", messages)
    other_e, other_v = judge(provider, shape, messages)
    assert flat_e == other_e
    assert flat_v.to_mapping() == other_v.to_mapping()
