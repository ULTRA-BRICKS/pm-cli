"""Pure mapping from native ChatGPT read payloads to listing rows and audit evidence.

Accessed via: ``audit_collector`` (after every ``PageReader`` call). No I/O, no clock of its own.

Two wire shapes of a conversation read carry the same conversation and parse to the SAME
``ConversationEvidence``. The reader stamps each payload with the shape of the endpoint it called
(``audit_urls.NATIVE_FORMAT_KEY``), and that marker is the format proof:

- ``flat``: the PLURAL ``/backend-api/conversations/<id>?num_turns=N`` endpoint, verified live. It
  is fast, honours ``num_turns`` and returns a chronological ``messages`` list plus ``current_node``
  (the last message) and ``page_info`` (``has_previous_page`` marks a cut window). The order is
  trusted ONLY with the ``flat`` marker, a unique-id list whose ``current_node`` is its last
  message, whose ``metadata.parent_id`` values never point forward, whose user messages never go
  back in time and whose timestamps never fall more than ``FLAT_MAX_SKEW`` behind (the live list
  has small in-turn regressions). A flat payload without the marker is refused.
- ``mapping``: the older SINGULAR full-mapping endpoint, a fallback that ignores ``num_turns`` and
  times out on large chats. Only the CURRENT branch (``current_node`` up through ``parent``) is
  read, with a cycle guard and the ``MAX_NODES``/``MAX_PATH`` bounds. An unmarked mapping payload
  is accepted (its parent chain is its own proof); a marker that contradicts the payload is not.

A payload that is neither shape, both shapes, or malformed raises ``NativeError``; nothing partial
is returned. Only visible ``user`` and ``assistant`` text messages become ``MessageRef`` values;
``system``, ``tool``, hidden-reasoning content (``thoughts`` and anything else that is not plain
visible text) are never copied: a reasoning recap is only a marker and a successful tool run is
only a marker on the next assistant message. The evidence carries the text of ONLY the last
substantive user request (a bare "go"/"continue" resolves to the prior substantive request) and the
last final assistant answer. A final answer is the assistant text message addressed to the user
whose ``channel`` is ``final`` (or, without a channel, whose ``end_turn`` is true); commentary after
it never undoes it. Closing-paragraph signals are derived only from a final that follows the latest
user message. A cut window with no substantive request is ``context_truncated``: the collector
re-reads it with more turns (``audit_urls.next_turns``) and, at the cap, the decision is unknown.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from . import audit_urls
from .audit_evidence import (
    FINISHED,
    MAX_ARTIFACTS,
    MAX_MESSAGES,
    MAX_TEXT,
    ConversationEvidence,
    EvidenceError,
    MessageRef,
    bounded_text,
    is_trivial_ack,
)
from .audit_redact import redact
from .audit_signals import EMPTY, detect, detect_request

MAX_NODES = 5000
MAX_PATH = 3000
FLAT_MAX_SKEW = 600.0  # seconds a flat message may be stamped before the latest one seen so far
_PROJECT = re.compile(r"g-p-[0-9a-f]{32}")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_STATUS = re.compile(r"[a-z][a-z0-9_]{0,39}")
_VISIBLE_TEXT = ("text", "multimodal_text")


class NativeError(ValueError):
    """A native payload does not have the expected structure."""


@dataclass(frozen=True)
class ListedConversation:
    conversation_id: str
    updated_at: float | None
    archived: bool
    project_id: str | None


@dataclass(frozen=True)
class ListPage:
    items: tuple[ListedConversation, ...]
    malformed: int
    total: int | None
    raw_count: int


def _timestamp(value: object) -> float | None:
    if type(value) in (int, float):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()
    return None


def _project_of(item: dict) -> str | None:
    gizmo = item.get("gizmo_id")
    if not isinstance(gizmo, str):
        return None
    match = _PROJECT.match(gizmo.lower())
    return match[0] if match else None


def parse_list_page(payload: object, *, archived: bool) -> ListPage:
    """One listing page; malformed items are counted and skipped, a malformed page raises."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise NativeError("list page has no items array")
    items: list[ListedConversation] = []
    malformed = 0
    for raw in payload["items"]:
        updated = _timestamp(raw.get("update_time")) if isinstance(raw, dict) else None
        cid = raw.get("id") if isinstance(raw, dict) else None
        if updated is None or not isinstance(cid, str) or _ID.fullmatch(cid) is None:
            malformed += 1
            continue
        items.append(
            ListedConversation(
                conversation_id=cid,
                updated_at=updated,
                archived=archived or raw.get("is_archived") is True,
                project_id=_project_of(raw),
            )
        )
    total = payload.get("total")
    total = total if type(total) is int and total >= 0 else None
    return ListPage(tuple(items), malformed, total, len(payload["items"]))


@dataclass(frozen=True)
class ProjectPage:
    project_ids: tuple[str, ...]
    malformed: int
    raw_count: int


def parse_project_page(payload: object) -> ProjectPage:
    """One page of project ids (already flattened to ``items[].id`` by the reader)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise NativeError("project page has no items array")
    ids: dict[str, None] = {}
    malformed = 0
    for raw in payload["items"]:
        pid = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(pid, str) or not audit_urls.valid_project_id(pid.lower()):
            malformed += 1
            continue
        ids[pid.lower()] = None
    return ProjectPage(tuple(ids), malformed, len(payload["items"]))


def _shape_of(payload: dict) -> str:
    """The wire shape of ``payload``; the reader's marker must agree with what is really there."""
    claimed = payload.get(audit_urls.NATIVE_FORMAT_KEY)
    if claimed is not None and claimed not in audit_urls.NATIVE_FORMATS:
        raise NativeError("unknown native format marker")
    has_mapping, has_messages = "mapping" in payload, "messages" in payload
    if has_mapping == has_messages:
        raise NativeError("conversation payload is neither a flat nor a mapping read")
    shape = audit_urls.NATIVE_MAPPING if has_mapping else audit_urls.NATIVE_FLAT
    if claimed is None and shape == audit_urls.NATIVE_FLAT:
        raise NativeError("flat messages need a native format proof from the reader")
    if claimed is not None and claimed != shape:
        raise NativeError("native format marker contradicts the payload shape")
    return shape


def _flat_path(payload: dict) -> tuple[list[dict], bool]:
    """The flat ``messages`` list as path nodes, once its documented order is proven."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise NativeError("conversation has no messages")
    if len(messages) > MAX_NODES:
        raise NativeError("conversation messages are too large")
    if not isinstance(payload.get("conversation_id"), str):
        raise NativeError("flat conversation does not name its conversation id")
    position: dict[str, int] = {}
    for index, message in enumerate(messages):
        mid = message.get("id") if isinstance(message, dict) else None
        if not isinstance(mid, str) or _ID.fullmatch(mid) is None:
            raise NativeError("message id is malformed")
        if mid in position:
            raise NativeError("flat messages repeat an id")
        position[mid] = index
    if payload.get("current_node") != messages[-1]["id"]:
        raise NativeError("current_node is not the last flat message")
    latest = latest_user = None
    for index, message in enumerate(messages):
        meta = message.get("metadata")
        parent = meta.get("parent_id") if isinstance(meta, dict) else None
        if parent is not None and (
            not isinstance(parent, str) or position.get(parent, -1) >= index
        ):
            raise NativeError("flat message parent is malformed or comes later")
        created = message.get("create_time")
        if created is None:
            continue
        if type(created) not in (int, float) or not math.isfinite(created):
            raise NativeError("message create_time is malformed")
        if latest is not None and created < latest - FLAT_MAX_SKEW:
            raise NativeError("flat messages are out of chronological order")
        latest = created if latest is None else max(latest, created)
        author = message.get("author")
        if isinstance(author, dict) and author.get("role") == "user":
            if latest_user is not None and created < latest_user:
                raise NativeError("flat user messages are out of chronological order")
            latest_user = created
    page = payload.get("page_info")
    if page is not None and not isinstance(page, dict):
        raise NativeError("page_info is malformed")
    previous = page.get("has_previous_page") if page is not None else None
    if previous is not None and not isinstance(previous, bool):
        raise NativeError("page_info.has_previous_page is malformed")
    # Without an explicit "nothing earlier" the window may hide older turns: assume it does.
    return [{"id": m["id"], "message": m} for m in messages], previous is not False


def _current_path(payload: dict) -> tuple[list[dict], bool]:
    """The current-branch messages (oldest first) and whether the window cut off earlier ones."""
    if _shape_of(payload) == audit_urls.NATIVE_FLAT:
        return _flat_path(payload)
    return _mapping_path(payload)


def _mapping_path(payload: dict) -> tuple[list[dict], bool]:
    mapping = payload.get("mapping")
    current = payload.get("current_node")
    if not isinstance(mapping, dict) or not mapping:
        raise NativeError("conversation has no mapping")
    if len(mapping) > MAX_NODES:
        raise NativeError("conversation mapping is too large")
    if not isinstance(current, str) or current not in mapping:
        raise NativeError("current_node is missing from the mapping")
    path: list[dict] = []
    seen: set[str] = set()
    node_id: str | None = current
    window_cut = False
    while node_id is not None:
        if node_id in seen or len(path) >= MAX_PATH:
            raise NativeError("conversation mapping has a cycle or is too deep")
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            window_cut = True  # the parent lies outside the returned window
            break
        path.append(node)
        parent = node.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise NativeError("node parent is malformed")
        node_id = parent
    path.reverse()
    return path, window_cut


def _text_of(content: dict) -> str:
    parts = content.get("parts")
    if not isinstance(parts, list):
        return ""
    return "\n".join(p for p in parts if isinstance(p, str))


def _status_of(message: dict) -> str:
    status = message.get("status")
    return status if isinstance(status, str) and _STATUS.fullmatch(status) else "unknown"


def _artifact_counts(content: dict, meta: dict) -> tuple[int, int]:
    """(images, files) carried by a message: attachment kinds only, never names or content."""
    images = files = 0
    parts = content.get("parts")
    if isinstance(parts, list):
        for part in parts[:MAX_ARTIFACTS]:
            if isinstance(part, dict):
                if part.get("content_type") == "image_asset_pointer":
                    images += 1
                else:
                    files += 1
    attachments = meta.get("attachments")
    if isinstance(attachments, list):
        for item in attachments[:MAX_ARTIFACTS]:
            if isinstance(item, dict):
                mime = item.get("mime_type")
                if isinstance(mime, str) and mime.startswith("image/"):
                    images += 1
                else:
                    files += 1
    return min(images, MAX_ARTIFACTS), min(files, MAX_ARTIFACTS)


def _visible_messages(path: list[dict]) -> list[tuple[MessageRef, str]]:
    """(ref, text) for each visible user/assistant message on the path, in order."""
    out: list[tuple[MessageRef, str]] = []
    tool_ok = False
    tool_images = tool_files = 0
    for node in path:
        message = node.get("message")
        if message is None:
            continue
        if not isinstance(message, dict):
            raise NativeError("message is malformed")
        author, content = message.get("author"), message.get("content")
        if not isinstance(author, dict) or not isinstance(content, dict):
            raise NativeError("message author/content is malformed")
        created = message.get("create_time")
        if created is not None and (
            type(created) not in (int, float) or not math.isfinite(created)
        ):
            raise NativeError("message create_time is malformed")
        mid, role = message.get("id") or node.get("id"), author.get("role")
        if not isinstance(mid, str) or _ID.fullmatch(mid) is None:
            raise NativeError("message id is malformed")
        ctype = content.get("content_type")
        status = _status_of(message)
        meta = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if role == "tool":
            if status == FINISHED:
                tool_ok = True
                images, files = _artifact_counts(content, meta)
                tool_images, tool_files = tool_images + images, tool_files + files
            continue
        if role not in ("user", "assistant") or meta.get("is_visually_hidden_from_conversation"):
            continue
        turn = meta.get("turn_exchange_id")
        turn = turn if isinstance(turn, str) and _ID.fullmatch(turn) else ""
        end_turn = message.get("end_turn")
        end_turn = end_turn if isinstance(end_turn, bool) else None
        recap = role == "assistant" and ctype == "reasoning_recap"
        visible = ctype in _VISIBLE_TEXT and message.get("recipient", "all") in (None, "all")
        if not (visible or recap):
            continue
        text = _text_of(content) if visible else ""
        images, files = _artifact_counts(content, meta) if visible else (0, 0)
        if role == "assistant" and visible:
            images, files = images + tool_images, files + tool_files
        channel = message.get("channel")
        final = (
            role == "assistant"
            and visible
            and (bool(text.strip()) or images + files > 0)
            and (channel == "final" if channel else end_turn is True)
        )
        ref = MessageRef(
            id=mid,
            role=role,
            created_at=float(created) if created is not None else None,
            turn_uuid=turn,
            status=status,
            end_turn=end_turn,
            final=final,
            tool_success=role == "assistant" and tool_ok,
            recap=recap,
            image_artifacts=min(images, MAX_ARTIFACTS),
            file_artifacts=min(files, MAX_ARTIFACTS),
        )
        if role == "assistant":
            tool_ok, tool_images, tool_files = False, 0, 0
        out.append((ref, text))
    return out


def build_evidence(
    payload: object,
    *,
    listed: ListedConversation,
    account: str,
    observed_account: str | None,
    now: float,
    running: bool = False,
) -> ConversationEvidence:
    """Evidence for one conversation read at ``now``; raises ``NativeError`` when malformed."""
    if not isinstance(payload, dict):
        raise NativeError("conversation payload is not an object")
    named = payload.get("conversation_id")
    if named is not None and named != listed.conversation_id:
        raise NativeError("conversation id does not match the requested conversation")
    path, window_cut = _current_path(payload)
    visible = _visible_messages(path)
    if len(visible) > MAX_MESSAGES:
        visible = visible[-MAX_MESSAGES:]
        window_cut = True
    refs = tuple(ref for ref, _ in visible)
    texts = {ref.id: text for ref, text in visible}

    request = next(
        (
            r
            for r in reversed(refs)
            if r.role == "user" and texts[r.id].strip() and not is_trivial_ack(texts[r.id])
        ),
        None,
    )
    final = next((r for r in reversed(refs) if r.final), None)
    request_raw, request_cut = bounded_text(texts[request.id], MAX_TEXT) if request else ("", False)
    final_raw, final_cut = bounded_text(texts[final.id], MAX_TEXT) if final else ("", False)

    last_user = next((r for r in reversed(refs) if r.role == "user"), None)
    answers_latest = (
        final is not None and last_user is not None and refs.index(final) > refs.index(last_user)
    )
    signals = detect(final_raw, request_raw) if answers_latest else EMPTY
    after_user = refs[refs.index(last_user) + 1 :] if last_user is not None else ()
    end_turn_without_reply = not answers_latest and any(
        r.role == "assistant"
        and r.end_turn is True
        and r.status == FINISHED
        and not r.recap
        and r.artifacts == 0
        and not texts[r.id].strip()
        for r in after_user
    )
    updated = (
        listed.updated_at
        if listed.updated_at is not None
        else _timestamp(payload.get("update_time"))
    )
    try:
        return ConversationEvidence(
            account=account,
            observed_account=observed_account,
            conversation_id=listed.conversation_id,
            url=audit_urls.conversation_url(listed.conversation_id),
            project_id=listed.project_id,
            updated_at=updated,
            archived=listed.archived,
            messages=refs,
            request_id=request.id if request else None,
            request_text=redact(request_raw),
            request_truncated=request_cut,
            final_id=final.id if final else None,
            final_text=redact(final_raw),
            final_truncated=final_cut,
            window_cut=window_cut,
            context_truncated=window_cut and request is None,
            read_at=now,
            verified_at=now,
            pending_approval=signals.pending_approval,
            permission_expired=signals.permission_expired,
            human_input_required=signals.human_input_required,
            progress_indicator=running,
            continuation_announced=signals.continuation_announced,
            login_required=signals.login_required,
            stop_intent=request is not None and detect_request(request_raw).stop_intent,
            end_turn_without_reply=end_turn_without_reply,
            unfinished_cue=signals.unfinished_cue,
            doubt_cue=signals.doubt_cue,
        )
    except EvidenceError as exc:
        raise NativeError(f"evidence rejected: {exc}") from exc
