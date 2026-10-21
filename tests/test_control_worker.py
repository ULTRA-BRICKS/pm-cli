"""The approval worker: persist-before-post, verified verdicts only, fresh re-check, no retries."""

from __future__ import annotations

import pytest

from pmlib.chatgpt_control import api, connector
from pmlib.chatgpt_control.guardian_client import (
    ContinuationStatus,
    ContinueReply,
    GuardianUnavailable,
    GuardianUncertain,
)
from pmlib.chatgpt_control.ledger import Ledger
from pmlib.chatgpt_control.tg_transport import OwnerVerdict
from pmlib.chatgpt_control.worker import Worker
from pmlib.chatgpt_tui import ActionResult
from tests.control_support import CHAT_A, CHAT_B, NOW, combined, make_services, row

APPROVED = OwnerVerdict("approved", 0, "")


class Harness:
    def __init__(self, tmp_path, data=None, verdict=APPROVED):
        self.services, self.telemetry, self.clock = make_services(tmp_path, data)
        self.verdict = verdict
        self.questions: list[tuple[str, int]] = []
        self.ledger_at_ask: list[list[str]] = []
        self.on_ask = None
        self.guardian_posts: list[tuple[str, dict]] = []
        self.guardian_gets: list[tuple[str, str]] = []
        self.post_result = ContinueReply("queued", "")
        self.get_results = [ContinuationStatus("sent", "")]
        self.controls: list[tuple] = []
        self.control_result = ActionResult("success", "ok")
        self.installs = 0
        self.owner = "ownerref"
        self.worker = Worker(
            self.services,
            transport=self._transport,
            guardian=self,
            run_control=self._run_control,
            install=self._install,
            owner_ref=lambda: self.owner,
            poll_seconds=3,
            sleep=lambda seconds: self.clock.advance(1),
        )

    # transport fake: records that the ledger already holds ask_started when the card is posted
    def _transport(self, question, timeout):
        self.questions.append((question, timeout))
        self.ledger_at_ask.append([e["kind"] for e in self.ledger.read_all()])
        if self.on_ask:
            self.on_ask()
        if isinstance(self.verdict, BaseException):
            raise self.verdict
        return self.verdict

    # guardian fake
    def request_continue(self, account, body):
        self.guardian_posts.append((account, body))
        if isinstance(self.post_result, BaseException):
            raise self.post_result
        return self.post_result

    def continuation(self, account, request_id):
        self.guardian_gets.append((account, request_id))
        if isinstance(self.get_results, BaseException):
            raise self.get_results
        return self.get_results[min(len(self.guardian_gets) - 1, len(self.get_results) - 1)]

    def _run_control(self, account, action, chat):
        self.controls.append((account, action, chat))
        return self.control_result

    def _install(self, state_dir):
        self.installs += 1
        return connector.install(state_dir)

    @property
    def ledger(self):
        return Ledger(self.services.state_dir)

    def request(self, **params):
        out = api.dispatch(self.services, "actions.request", params)
        assert out["ok"], out
        return out["result"]["request_id"]

    def status(self, request_id):
        return self.services.snapshot().requests[request_id].status

    def reason(self, request_id):
        return self.services.snapshot().requests[request_id].status_reason

    def kinds(self):
        return [e["kind"] for e in self.ledger.read_all()]


@pytest.fixture
def h(tmp_path):
    return Harness(tmp_path)


def test_happy_path_continue_end_to_end(h):
    rid = h.request(workspace="glide.vc", chat=CHAT_B, action="continue")
    report = h.worker.process_next()
    assert report["request_id"] == rid and report["status"] == "sent"
    assert h.kinds() == [
        "request_created",
        "ask_started",
        "ask_result",
        "dispatch_started",
        "dispatch_result",
        "continuation_status",
    ]
    (account, body) = h.guardian_posts[0]
    assert account == "glide.vc"
    assert set(body) == {"request_id", "chat", "expected_turn", "approved_at", "expires_at"}
    assert (
        body["request_id"] == rid and body["chat"] == CHAT_B and body["expected_turn"] == "turn-1"
    )
    request = h.services.snapshot().requests[rid]
    assert body["approved_at"] == int(request.approved_at) and isinstance(body["approved_at"], int)
    assert body["expires_at"] == request.expires_at <= body["approved_at"] + 900
    assert h.guardian_gets and h.guardian_gets[0] == ("glide.vc", rid)


def test_request_is_persisted_before_the_card_is_posted(h):
    h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.ledger_at_ask == [["request_created", "ask_started"]]


def test_question_binds_account_chat_turn_digest_and_expiry(h):
    rid = h.request(
        workspace="glide.vc", chat=CHAT_B, action="continue", reason="answer looks partial"
    )
    h.worker.process_next()
    question, timeout = h.questions[0]
    request = h.services.snapshot().requests[rid]
    for needle in ("glide.vc", "beta", "turn-1", "continue", request.plan_digest[:16], "partial"):
        assert needle in question
    assert timeout == 900


def test_ask_timeout_never_outlives_the_request(h):
    h.request(workspace="home", chat=CHAT_A, action="continue")
    h.clock.advance(300)
    h.telemetry.data = combined(
        {"home": [row(last_checked_at=NOW + 290)], "glide.vc": []}, generated_at=NOW + 295
    )
    h.worker.process_next()
    assert h.questions[0][1] == 600


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("rejected", "rejected"),
        ("timed_out", "timed_out"),
        ("delivery_unavailable", "delivery_unavailable"),
        ("uncertain", "asked_post_uncertain"),
    ],
)
def test_non_approvals_change_nothing(tmp_path, outcome, status):
    h = Harness(tmp_path, verdict=OwnerVerdict(outcome, 1, "x"))
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == status
    assert h.guardian_posts == [] and h.controls == [] and h.installs == 0
    assert h.worker.process_next() is None  # terminal: never re-asked
    assert len(h.questions) == 1


def test_missing_owner_configuration_posts_nothing(h):
    h.owner = None
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "delivery_unavailable" and h.questions == []


def test_one_request_at_a_time_in_ledger_order(h):
    first = h.request(workspace="home", chat=CHAT_A, action="pause")
    second = h.request(workspace="glide.vc", chat=CHAT_B, action="pause")
    h.worker.process_next()
    assert h.status(first) == "applied" and h.status(second) == "queued"
    h.worker.process_next()
    assert h.status(second) == "applied"
    assert h.worker.process_next() is None


def test_pre_ask_invalidation_never_bothers_the_owner(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.telemetry.data = combined({"home": [row(turn="turn-2")], "glide.vc": []})
    h.worker.process_next()
    assert (h.status(rid), h.reason(rid)) == ("invalidated", "turn-changed")
    assert h.questions == []


CHANGES = [
    (lambda: {"turn": "turn-2"}, "turn-changed"),
    (lambda: {"status": "working", "running": True}, "running"),
    (lambda: {"running": True}, "contradictory-completed-and-running"),
    (lambda: {"reason": "Astra quota is latched"}, "blocker"),
    (lambda: {"reason": "unresolved-send"}, "blocker"),
    (lambda: {"draft": True}, "draft-or-attachments"),
    (lambda: {"auth_challenge": True}, "auth-challenge"),
    (lambda: {"observation": "stale"}, "observation-not-current"),
    (lambda: {"check_stale": True}, "session-stale"),
    (lambda: {"last_go_state": "uncertain"}, "send-uncertain"),
]


@pytest.mark.parametrize("change,reason", CHANGES)
def test_changes_after_approval_invalidate_before_dispatch(h, change, reason):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.on_ask = lambda: setattr(
        h.telemetry, "data", combined({"home": [row(**change())], "glide.vc": []})
    )
    h.worker.process_next()
    assert (h.status(rid), h.reason(rid)) == ("invalidated", reason)
    assert h.guardian_posts == []


def test_account_down_or_stale_or_missing_after_approval_invalidates(h):
    for n, make in enumerate(
        (
            lambda: combined({"home": None, "glide.vc": []}),
            lambda: combined({"home": [row()], "glide.vc": []}, stale=True),
            lambda: combined({"home": [row(CHAT_B)], "glide.vc": []}),
            lambda: combined({"home": [row(), row()], "glide.vc": []}),
        )
    ):
        harness = Harness(h.services.state_dir.parent / f"case{n}")
        rid = harness.request(workspace="home", chat=CHAT_A, action="continue")
        harness.on_ask = lambda harness=harness, make=make: setattr(
            harness.telemetry, "data", make()
        )
        harness.worker.process_next()
        assert harness.status(rid) == "invalidated" and harness.guardian_posts == []


def test_expiry_during_the_wait_blocks_dispatch(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.on_ask = lambda: h.clock.advance(901)
    h.worker.process_next()
    assert h.status(rid) == "expired" and h.guardian_posts == []


def test_tampered_plan_digest_is_invalidated(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    lines = h.ledger.path.read_text().splitlines()
    h.ledger.path.write_text(
        lines[0].replace('"expected_turn":"turn-1"', '"expected_turn":"turn-0"') + "\n"
    )
    h.worker.process_next()
    assert h.status(rid) == "invalidated" and "plan" in h.reason(rid)
    assert h.guardian_posts == [] and h.questions == []


def test_guardian_unavailable_is_a_clean_failure_without_retry(h):
    h.post_result = GuardianUnavailable("home Guardian has no approved-continuation route")
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "failed" and "guardian continuation unavailable" in h.reason(rid)
    assert h.worker.process_next() is None and len(h.guardian_posts) == 1


def test_guardian_refusal_is_recorded_as_refused(h):
    h.post_result = ContinueReply("refused", "busy response")
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert (h.status(rid), h.reason(rid)) == ("refused", "busy response")


def test_ambiguous_dispatch_is_uncertain_never_retried_then_reconciled_by_status_only(h):
    h.post_result = GuardianUncertain("timeout")
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "uncertain"
    assert len(h.guardian_posts) == 1
    h.worker.reconcile()
    assert h.status(rid) == "sent"
    assert len(h.guardian_posts) == 1  # reconciliation reads; it never re-sends


def test_unexpected_exception_during_dispatch_is_uncertain(h):
    h.post_result = RuntimeError("socket exploded")
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "uncertain" and "socket exploded" not in h.reason(rid)


def test_interrupt_during_dispatch_records_uncertain_and_propagates(h):
    h.post_result = KeyboardInterrupt()
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    with pytest.raises(KeyboardInterrupt):
        h.worker.process_next()
    assert h.status(rid) == "uncertain"


def test_interrupt_during_ask_is_post_uncertain_and_never_re_asked(tmp_path):
    h = Harness(tmp_path, verdict=KeyboardInterrupt())
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    with pytest.raises(KeyboardInterrupt):
        h.worker.process_next()
    assert h.status(rid) == "asked_post_uncertain"
    h.verdict = APPROVED
    assert h.worker.process_next() is None and len(h.questions) == 1


def orphan(h, *events):
    for kind, extra in events:
        h.ledger.append({"kind": kind, "at": h.clock.now, **extra})


def test_recovery_abandons_an_orphaned_ask_instead_of_replaying_it(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    orphan(h, ("ask_started", {"request_id": rid, "timeout_s": 900, "question_digest": "q"}))
    assert h.status(rid) == "asked"
    h.worker.recover()
    assert h.status(rid) == "asked_post_uncertain"
    assert h.worker.process_next() is None and h.questions == []


def test_recovery_marks_an_orphaned_dispatch_uncertain_never_redispatches(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    orphan(
        h,
        ("ask_started", {"request_id": rid, "timeout_s": 900}),
        ("ask_result", {"request_id": rid, "outcome": "approved"}),
        ("dispatch_started", {"request_id": rid, "approved_at": int(h.clock.now)}),
    )
    h.worker.recover()
    assert h.status(rid) == "uncertain"
    h.worker.run_once()
    assert h.guardian_posts == [] and h.status(rid) in ("uncertain", "sent")


def test_approved_but_not_yet_dispatched_resumes_exactly_once(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    orphan(
        h,
        ("ask_started", {"request_id": rid, "timeout_s": 900}),
        ("ask_result", {"request_id": rid, "outcome": "approved"}),
    )
    h.worker.process_next()
    h.worker.process_next()
    assert len(h.guardian_posts) == 1 and h.questions == []
    assert h.status(rid) == "sent"


def test_poll_until_terminal_status_without_extra_posts(h):
    h.get_results = [
        ContinuationStatus("queued", ""),
        ContinuationStatus("queued", ""),
        ContinuationStatus("sent", ""),
    ]
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "sent" and len(h.guardian_posts) == 1 and len(h.guardian_gets) == 3


def test_guardian_reports_refusal_after_queueing(h):
    h.get_results = [ContinuationStatus("refused", "turn changed")]
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert (h.status(rid), h.reason(rid)) == ("refused", "turn changed")


def test_still_queued_after_the_poll_budget_stays_dispatch_queued_and_reconciles_later(h):
    h.get_results = [ContinuationStatus("queued", "")]
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    assert h.status(rid) == "dispatch_queued"
    h.get_results = [ContinuationStatus("sent", "")]
    h.guardian_gets.clear()
    h.worker.reconcile()
    assert h.status(rid) == "sent"


def test_reconcile_survives_unavailable_guardian(h):
    h.get_results = [ContinuationStatus("queued", "")]
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    h.get_results = GuardianUnavailable("down")
    h.worker.reconcile()
    assert h.status(rid) == "dispatch_queued"


def test_reconcile_ignores_unknown_guardian_status(h):
    h.get_results = [ContinuationStatus("queued", "")]
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.worker.process_next()
    h.get_results = [ContinuationStatus(None, "HTTP 404")]
    h.worker.reconcile()
    assert h.status(rid) == "dispatch_queued"


@pytest.mark.parametrize(
    "result,status",
    [
        (ActionResult("success", "done"), "applied"),
        (ActionResult("pending", "still changing"), "uncertain"),
        (ActionResult("uncertain", "timed out"), "uncertain"),
        (ActionResult("failed", "exit 4"), "failed"),
    ],
)
def test_controls_run_once_through_the_typed_runner(h, result, status):
    h.control_result = result
    rid = h.request(workspace="home", chat=CHAT_A, action="pause")
    h.worker.process_next()
    assert h.controls == [("home", "pause", CHAT_A)] and h.status(rid) == status
    assert h.worker.process_next() is None and len(h.controls) == 1


def test_service_stop_needs_approval_but_no_telemetry(tmp_path):
    h = Harness(tmp_path, combined({"home": None, "glide.vc": [row()]}))
    rid = h.request(workspace="home", action="service-stop")
    h.worker.process_next()
    assert h.status(rid) == "applied" and h.controls == [("home", "service-stop", None)]


def test_control_is_revalidated_after_approval(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="watch")
    h.on_ask = lambda: setattr(
        h.telemetry, "data", combined({"home": [row(CHAT_B)], "glide.vc": []})
    )
    h.worker.process_next()
    assert h.status(rid) == "invalidated" and h.controls == []


def test_account_isolation_one_down_other_proceeds(tmp_path):
    h = Harness(tmp_path)
    home = h.request(workspace="home", chat=CHAT_A, action="continue")
    glide = h.request(workspace="glide.vc", chat=CHAT_B, action="continue")
    h.telemetry.data = combined({"home": None, "glide.vc": [row(CHAT_B)]})
    h.worker.run_once()
    assert h.status(home) == "invalidated" and h.reason(home) == "account-unavailable"
    assert h.status(glide) == "sent"
    assert [a for a, _ in h.guardian_posts] == ["glide.vc"]


def test_connector_install_after_approval(h):
    out = api.dispatch(
        h.services, "connectors.request", {"connector": "guardian", "target": "chatgpt"}
    )
    rid = out["result"]["request_id"]
    h.worker.process_next()
    assert h.status(rid) == "applied" and h.installs == 1
    assert connector.local_state(h.services.state_dir)["state"] == "local_ready"


def test_connector_install_failure_is_failed_not_applied(h, monkeypatch):
    def boom(state_dir):
        raise connector.ConnectorError("manifest.json is a symlink")

    h.worker._install = boom
    rid = api.dispatch(
        h.services, "connectors.request", {"connector": "guardian", "target": "chatgpt"}
    )["result"]["request_id"]
    h.worker.process_next()
    assert h.status(rid) == "failed" and "symlink" in h.reason(rid)


def test_rejected_connector_installs_nothing(tmp_path):
    h = Harness(tmp_path, verdict=OwnerVerdict("rejected", 1, "x"))
    api.dispatch(h.services, "connectors.request", {"connector": "guardian", "target": "chatgpt"})
    h.worker.process_next()
    assert (
        h.installs == 0 and connector.local_state(h.services.state_dir)["state"] == "not_installed"
    )


def test_connector_question_names_the_plan_digest(h):
    api.dispatch(h.services, "connectors.request", {"connector": "guardian", "target": "chatgpt"})
    h.worker.process_next()
    request = next(iter(h.services.snapshot().requests.values()))
    assert request.plan_digest[:16] in h.questions[0][0]
    assert connector.plan()["plan_digest"][:16] in h.questions[0][0]


PASSIVE = {"model": "Thinking effortPro", "effort": "", "model_verified": False}


def test_real_passive_shape_reaches_the_owner_and_guardian_decides_model(tmp_path):
    """Eligibility to ASK ignores the unverified passive model; Guardian gets the exact command."""
    data = combined({"home": [row(**PASSIVE)], "glide.vc": []})
    h = Harness(tmp_path, data)
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    report = h.worker.process_next()
    assert report["status"] == "sent" and len(h.questions) == 1
    (_, body) = h.guardian_posts[0]
    assert set(body) == {"request_id", "chat", "expected_turn", "approved_at", "expires_at"}
    assert body["request_id"] == rid  # no model/effort guess is added to the command


def test_a_newer_turn_after_approval_invalidates_the_exact_turn_proposal(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.on_ask = lambda: setattr(
        h.telemetry, "data", combined({"home": [row(turn="turn-2")], "glide.vc": []})
    )
    h.worker.process_next()
    assert (h.status(rid), h.reason(rid)) == ("invalidated", "turn-changed")
    assert h.guardian_posts == []


def test_stale_session_check_invalidates_before_dispatch(h):
    rid = h.request(workspace="home", chat=CHAT_A, action="continue")
    h.on_ask = lambda: setattr(
        h.telemetry,
        "data",
        combined({"home": [row(session_stale=True, observation="stale")], "glide.vc": []}),
    )
    h.worker.process_next()
    assert h.status(rid) == "invalidated" and h.guardian_posts == []
