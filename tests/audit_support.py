"""Shared builders for the task-audit tests: synthetic evidence, classifications, clocks.

Everything here is tiny and synthetic; no real chat text, token or cookie ever appears.
"""

from __future__ import annotations

import dataclasses

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_evidence as ev

NOW = 1792254000.0
ACCOUNT = "home"
CONVERSATION = "11111111-2222-3333-4444-555555555555"
URL = f"https://chatgpt.com/c/{CONVERSATION}"
TURN = "turn-1"
REAL_META = {
    "api_version": 2,
    "model": "english",
    "checkpoint": "convaiinnovations/laya",
    "max_len": 512,
    "truncated": False,
    "input_tokens": 410,
    "state_tokens": 380,
    "state_tokens_dropped": 0,
}


def msg(mid: str, role: str, at: float, **kw) -> ev.MessageRef:
    base = {
        "id": mid,
        "role": role,
        "created_at": at,
        "turn_uuid": TURN,
        "status": "finished_successfully",
        "end_turn": True if role == "assistant" else None,
        "final": role == "assistant",
        "tool_success": False,
        "recap": False,
    }
    base.update(kw)
    return ev.MessageRef(**base)


def evidence(**kw) -> ev.ConversationEvidence:
    """A fresh, verified, answered, quiet conversation: the baseline that can be ``done``."""
    base = {
        "account": ACCOUNT,
        "observed_account": ACCOUNT,
        "conversation_id": CONVERSATION,
        "url": URL,
        "project_id": None,
        "updated_at": NOW - 600,
        "archived": False,
        "messages": (msg("u1", "user", NOW - 700), msg("a1", "assistant", NOW - 650)),
        "request_id": "u1",
        "request_text": "Summarize the three options and recommend one.",
        "request_truncated": False,
        "final_id": "a1",
        "final_text": "Here is the comparison and my recommendation: option B.",
        "final_truncated": False,
        "window_cut": False,
        "context_truncated": False,
        "read_at": NOW - 5,
        "verified_at": NOW - 5,
        "pending_approval": False,
        "permission_expired": False,
        "human_input_required": False,
        "progress_indicator": False,
        "continuation_announced": False,
    }
    base.update(kw)
    return ev.ConversationEvidence(**base)


def replace(evidence_obj: ev.ConversationEvidence, **kw) -> ev.ConversationEvidence:
    return dataclasses.replace(evidence_obj, **kw)


def laya(
    *,
    task_finished: float = 0.95,
    steps_remaining: float = 0.02,
    blocked_on_human: float = 0.02,
    response_finished: float = 0.95,
    model: str = "convaiinnovations/laya@512",
    meta: dict | None = None,
) -> clf.Classification:
    return clf.Classification(
        status="ok",
        scores={
            "task_finished": task_finished,
            "steps_remaining": steps_remaining,
            "blocked_on_human": blocked_on_human,
            "response_finished": response_finished,
        },
        model=model,
        prompt_version=clf.PROMPT_VERSION,
        reason="",
        meta=dict(REAL_META) if meta is None else meta,
    )
