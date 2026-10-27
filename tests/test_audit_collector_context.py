"""Bounded context expansion: a bare "go" in a cut window is re-read with more turns, never forever.

The reader is asked for 4 turns first; while the latest user message is contextless (no earlier
substantive request in the window) and the window is cut, the collector re-reads with 8, then 16
turns (the hard cap) through the same guarded reader, so the minimum gap between requests applies.
At the cap a still-missing goal stays ``truncated-context`` and the decision is unknown.
"""

from __future__ import annotations

import itertools

import pytest

from pmlib.chatgpt_control.audit_guard import MIN_GAP_SECONDS
from pmlib.chatgpt_control.audit_reader import ReaderHttpError, ReaderTimeout
from tests.audit_collector_support import FakeReader, listing_item, rig
from tests.native_support import as_flat, flat_payload, message

GO = "\u0433\u043e"  # the Russian word for "go"
CID = "c0"


def only_go():
    return as_flat(
        flat_payload(
            [
                message("u9", "user", GO, t=40),
                message(
                    "a9",
                    "assistant",
                    "Option B wins; here is why.",
                    t=45,
                    end_turn=True,
                    channel="final",
                ),
            ],
            previous_page=True,
            conversation_id=CID,
        )
    )


def with_goal(*, previous_page: bool = True):
    return as_flat(
        flat_payload(
            [
                message("u1", "user", "Summarize the three options.", t=0),
                message("a1", "assistant", "Here is a first summary.", t=5, end_turn=False),
                message("u9", "user", GO, t=40),
                message(
                    "a9",
                    "assistant",
                    "Option B wins; here is why.",
                    t=45,
                    end_turn=True,
                    channel="final",
                ),
            ],
            previous_page=previous_page,
            conversation_id=CID,
        )
    )


class TurnsReader(FakeReader):
    """Serves a different native payload (or error) for each requested turn count."""

    def __init__(self, by_turns, **kw):
        super().__init__(scopes={"recent": [listing_item(CID, 600)]}, **kw)
        self.by_turns = by_turns

    def read_conversation(self, conversation_id, turns):
        self._enter("read_conversation", conversation_id, turns)
        step = self.by_turns[turns]
        if isinstance(step, Exception):
            raise step
        return step


def turns_of(reader):
    return [call[2] for call in reader.called("read_conversation")]


def conversation_of(report):
    (found,) = report["accounts"]["home"]["conversations"]
    return found


def test_a_bare_go_is_re_read_with_more_turns_until_the_goal_appears(tmp_path):
    reader = TurnsReader({4: only_go(), 8: only_go(), 16: with_goal()})
    r = rig(tmp_path, {"home": reader})
    found = conversation_of(r.run())
    assert turns_of(reader) == [4, 8, 16]
    assert "truncated-context" not in found["reasons"]
    assert r.classifier.seen == [CID]  # the goal-bearing read is the one classified


def test_the_expansion_stops_as_soon_as_the_goal_is_in_the_window(tmp_path):
    reader = TurnsReader({4: only_go(), 8: with_goal()})
    r = rig(tmp_path, {"home": reader})
    conversation_of(r.run())
    assert turns_of(reader) == [4, 8]


def test_expansion_requests_keep_the_minimum_gap(tmp_path):
    reader = TurnsReader({4: only_go(), 8: only_go(), 16: with_goal()})
    r = rig(tmp_path, {"home": reader})
    conversation_of(r.run())
    times = [
        t
        for call, t in zip(reader.calls, reader.times, strict=True)
        if call[0] == "read_conversation"
    ]
    assert len(times) == 3
    assert all(b - a >= MIN_GAP_SECONDS for a, b in itertools.pairwise(times))


def test_the_cap_is_hard_and_a_missing_goal_stays_unknown(tmp_path):
    reader = TurnsReader({4: only_go(), 8: only_go(), 16: only_go()})
    r = rig(tmp_path, {"home": reader})
    found = conversation_of(r.run())
    assert turns_of(reader) == [4, 8, 16]
    assert found["task_state"] == "unknown" and "truncated-context" in found["reasons"]
    assert r.classifier.seen == []  # unknown context never reaches the classifier


def test_a_window_that_holds_the_goal_is_read_once(tmp_path):
    reader = TurnsReader({4: with_goal()})
    r = rig(tmp_path, {"home": reader})
    conversation_of(r.run())
    assert turns_of(reader) == [4]


def test_a_complete_history_is_never_expanded(tmp_path):
    reader = TurnsReader({4: with_goal(previous_page=False)})
    r = rig(tmp_path, {"home": reader})
    conversation_of(r.run())
    assert turns_of(reader) == [4]


def test_a_request_already_above_the_cap_is_not_expanded(tmp_path):
    reader = TurnsReader({30: only_go()})
    r = rig(tmp_path, {"home": reader})
    found = conversation_of(r.run(turns=30))
    assert turns_of(reader) == [30]
    assert "truncated-context" in found["reasons"]


@pytest.mark.parametrize("failure", [ReaderTimeout("slow"), ReaderHttpError(500, None)])
def test_a_failed_expansion_keeps_the_honest_truncated_verdict(tmp_path, failure):
    reader = TurnsReader({4: only_go(), 8: failure})
    r = rig(tmp_path, {"home": reader})
    found = conversation_of(r.run())
    assert turns_of(reader) == [4, 8]
    assert found["task_state"] == "unknown" and "truncated-context" in found["reasons"]


def test_a_rate_limit_during_expansion_stops_the_account(tmp_path):
    reader = TurnsReader({4: only_go(), 8: ReaderHttpError(429, "30")})
    r = rig(tmp_path, {"home": reader})
    report = r.run()
    assert turns_of(reader) == [4, 8]
    assert report["accounts"]["home"]["conversations"] == []
    assert report["accounts"]["home"]["not_before"] is not None


def test_the_budget_also_bounds_the_expansion_reads(tmp_path):
    from pmlib.chatgpt_control.audit_guard import Limits

    reader = TurnsReader({4: only_go(), 8: only_go(), 16: with_goal()})
    r = rig(tmp_path, {"home": reader}, limits=Limits(budget_seconds=40))
    acct = r.run()["accounts"]["home"]
    assert len(turns_of(reader)) < 3  # the budget cut the expansion short
    assert acct["status"] == "partial" and acct["coverage"]["budget_exhausted"] is True
    assert acct["coverage"]["requests"] == len(reader.calls)
