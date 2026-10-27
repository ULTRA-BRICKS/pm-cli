"""Builders for native conversation payloads in BOTH wire shapes (flat ``messages`` and ``mapping``).

Everything is invented neutral text. The message dicts carry the same keys as the live plural
endpoint's messages (``id, author, create_time, update_time, content, status, end_turn, weight,
metadata, recipient, channel``); the same dicts are wrapped either as a flat list or as a
``mapping`` chain, so one description yields two equivalent payloads.
"""

from __future__ import annotations

import json
from pathlib import Path

from pmlib.chatgpt_control import audit_reader, audit_urls
from tests.audit_support import CONVERSATION, NOW

FIXTURES = Path(__file__).parent / "fixtures"
BASE = NOW - 1000


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def message(mid, role, text=None, *, ctype="text", t=0.0, parts=None, **kw) -> dict:
    status = kw.pop("status", "finished_successfully")
    if ctype == "thoughts":
        content = {"content_type": "thoughts", "thoughts": [{"content": text}]}
    elif ctype == "reasoning_recap":
        content = {"content_type": "reasoning_recap", "content": text or ""}
    elif ctype == "execution_output":
        content = {"content_type": "execution_output", "text": text or ""}
    else:
        content = {
            "content_type": ctype,
            "parts": parts if parts is not None else ([] if text is None else [text]),
        }
    built = {
        "id": mid,
        "author": {"role": role, "name": None, "metadata": {}},
        "create_time": BASE + t,
        "update_time": None,
        "content": content,
        "status": status,
        "end_turn": kw.pop("end_turn", None),
        "weight": 1,
        "metadata": kw.pop("meta", None) or {},
        "recipient": kw.pop("recipient", "all"),
        "channel": kw.pop("channel", None),
    }
    assert not kw, kw
    return built


def flat_payload(messages, *, current=None, previous_page=False, **top) -> dict:
    return {
        "title": "t",
        "conversation_id": CONVERSATION,
        "update_time": NOW - 300,
        "messages": list(messages),
        "current_node": current or messages[-1]["id"],
        "page_info": {
            "start_cursor": "c0",
            "end_cursor": "c1",
            "has_previous_page": previous_page,
            "has_next_page": False,
        },
        **top,
    }


def mapping_payload(messages, *, current=None, **top) -> dict:
    mapping = {"root": {"id": "root", "message": None, "parent": None, "children": []}}
    parent = "root"
    for msg in messages:
        mapping[msg["id"]] = {"id": msg["id"], "message": msg, "parent": parent, "children": []}
        parent = msg["id"]
    return {
        "title": "t",
        "conversation_id": CONVERSATION,
        "update_time": NOW - 300,
        "current_node": current or messages[-1]["id"],
        "mapping": mapping,
        **top,
    }


def as_flat(payload: dict) -> dict:
    return audit_reader.mark_native(payload, audit_urls.NATIVE_FLAT)


def as_mapping(payload: dict) -> dict:
    return audit_reader.mark_native(payload, audit_urls.NATIVE_MAPPING)


def both(messages, **top) -> tuple[dict, dict]:
    """(marked flat payload, marked mapping payload) describing the same conversation."""
    return as_flat(flat_payload(messages, **top)), as_mapping(mapping_payload(messages, **top))
