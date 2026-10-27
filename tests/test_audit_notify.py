"""Owner-private audit notifications: significance, dedup, no blind replay, privacy, tg path."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

from pmlib.chatgpt_control import audit_notify as an
from pmlib.chatgpt_control.audit_store import AuditStore

SECRET = "SENTINEL secret text"


def conv(
    cid="c1", state="blocked", fp="f1", reasons=("needs-owner",), recovery="awaiting_owner", **extra
):
    return {
        "conversation_id": cid,
        "task_state": state,
        "fingerprint": fp,
        "recovery_state": recovery,
        "reasons": list(reasons),
        "source": "read",
        **extra,
    }


def report(convs=(), status="ok", reason="", account="home", others=None):
    accounts = {account: {"status": status, "reason": reason, "conversations": list(convs)}}
    accounts.update(others or {})
    return {"schema": 1, "accounts": accounts}


class Recorder:
    def __init__(self, verdict=an.SENT):
        self.verdict = verdict
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        return self.verdict


def run(rep, prev, store, rec, now=1000.0, **kw):
    return an.notify_report(rep, prev, store, now=now, send=rec, **kw)


def test_new_blocked_notifies_and_unchanged_does_not(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    rep = report([conv()])
    out = run(rep, None, store, rec)
    assert out["outcome"] == "sent" and out["sent_items"] == 1
    assert "https://chatgpt.com/c/c1" in rec.texts[0]
    again = run(rep, rep, AuditStore(tmp_path / "other"), rec)
    assert again["outcome"] == "none-significant" and len(rec.texts) == 1


def test_changed_fingerprint_or_state_notifies():
    for new in (conv(fp="f2"), conv(state="incomplete")):
        assert len(an.significant_items(report([new]), report([conv()]))) == 1


def test_done_working_unknown_never():
    rep = report([conv(state=s) for s in ("done", "working", "unknown")])
    assert an.significant_items(rep, None) == []


def test_cached_unchanged_is_quiet():
    rep = report([conv(source="cache")])
    assert an.significant_items(rep, rep) == []


def test_account_unavailable_once_same_reason_not_again():
    down = report(status="unavailable", reason="cdp-unreachable")
    assert len(an.significant_items(down, report())) == 1
    assert an.significant_items(down, down) == []
    other = report(status="unavailable", reason="http-429")
    assert len(an.significant_items(other, down)) == 1


def test_recovered_account_and_partial_ok_not_notified():
    assert an.significant_items(report(), report(status="unavailable", reason="x")) == []
    assert an.significant_items(report(status="partial", reason="budget-exhausted"), None) == []


def test_sent_key_never_resent_across_runs(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    rep = report([conv()])
    run(rep, None, store, rec)
    out = run(rep, None, store, rec, now=10**9)
    assert out["outcome"] == "suppressed" and out["suppressed_sent"] == 1 and len(rec.texts) == 1


def test_uncertain_never_replayed(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.UNCERTAIN)
    rep = report([conv()])
    first = run(rep, None, store, rec)
    assert first["outcome"] == "delivery_uncertain"
    rec.verdict = an.SENT
    later = run(rep, None, store, rec, now=10**10)
    assert later["outcome"] == "suppressed" and later["suppressed_uncertain"] == 1
    assert len(rec.texts) == 1


def test_failed_retried_after_cooldown_up_to_max_attempts(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.FAILED)
    rep = report([conv()])
    assert run(rep, None, store, rec, now=0)["outcome"] == "delivery_failed"
    early = run(rep, None, store, rec, now=100)
    assert early["suppressed_cooldown"] == 1 and len(rec.texts) == 1
    assert run(rep, None, store, rec, now=4000)["outcome"] == "delivery_failed"
    assert run(rep, None, store, rec, now=8000)["outcome"] == "delivery_failed"
    assert len(rec.texts) == 3
    capped = run(rep, None, store, rec, now=10**7)
    assert capped["suppressed_cooldown"] == 1 and len(rec.texts) == 3


def test_failed_then_success_marks_sent(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.FAILED)
    rep = report([conv()])
    run(rep, None, store, rec, now=0)
    rec.verdict = an.SENT
    assert run(rep, None, store, rec, now=5000)["outcome"] == "sent"
    assert run(rep, None, store, rec, now=10**7)["suppressed_sent"] == 1


def test_batching_and_more_line(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    rep = report([conv(cid=f"c{i}", fp=f"f{i}") for i in range(14)])
    out = run(rep, None, store, rec, max_items=10)
    assert out["sent_items"] == 14 and len(rec.texts) == 1
    lines = rec.texts[0].splitlines()
    assert len(lines) == 1 + 10 + 1 and lines[-1] == "+4 more"
    assert len(out["keys"]) == 14


def test_text_privacy_and_shape(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    rep = report(
        [
            conv(
                reasons=("ok-token", SECRET, "bad token"),
                scores={"x": 0.9},
                remaining_steps_count=7,
                title=SECRET,
                evidence_ids=[SECRET],
            ),
            conv(cid=SECRET, fp="f9"),
        ],
        others={SECRET: {"status": "unavailable", "reason": "x", "conversations": []}},
    )
    run(rep, None, store, rec)
    text = rec.texts[0]
    assert "SENTINEL" not in text and "secret" not in text
    assert "ok-token" in text and "0.9" not in text and "7" not in text.replace("chatgpt", "")
    assert not text.startswith("-") and len(text) <= 3500
    assert text.isascii() is False  # Russian header is present
    assert text.splitlines()[0] == an.HEADER


def test_unsafe_reason_on_unavailable_account_dropped():
    rep = report(status="unavailable", reason=SECRET)
    assert an.significant_items(rep, None) == []


def test_text_length_bounded():
    items = [{"key": str(i), "line": "x" * 600} for i in range(10)]
    text = an._text_for(items, 0)
    assert len(text) <= 3500 and "more" in text


def test_append_failure_reported_and_no_replay(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()

    class BrokenLog:
        def __init__(self):
            self.appended = 0

        def read_all(self):
            return []

        def append(self, event):
            self.appended += 1
            if self.appended > 1:  # the intent is durable; the outcome write fails
                raise OSError("disk")

    store.notification_log = lambda: BrokenLog()
    out = run(report([conv()]), None, store, rec)
    assert out["outcome"] == "sent" and out["log"] == "append-failed"


def test_raising_sender_is_uncertain(tmp_path):
    def boom(text):
        raise RuntimeError("x")

    out = an.notify_report(report([conv()]), None, AuditStore(tmp_path), now=1.0, send=boom)
    assert out["outcome"] == "delivery_uncertain"


# --- default tg sender -------------------------------------------------------------------


class FakeRun:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        res = self.results.pop(0)
        if isinstance(res, BaseException):
            raise res
        return res


def proc(code=0, out=b"OK sent\n"):
    return SimpleNamespace(returncode=code, stdout=out, stderr=b"")


def sender(fake, owner="abc"):
    return an.make_tg_sender(run=fake, owner=lambda: owner)


def test_unconfigured_owner_starts_nothing():
    fake = FakeRun()
    assert sender(fake, owner=None)("hi") == an.FAILED and fake.calls == []


def test_dry_run_refusal_blocks_real_send():
    fake = FakeRun(proc(1, b""))
    assert sender(fake)("hi") == an.FAILED and len(fake.calls) == 1
    assert "--dry-run" in fake.calls[0][0]


def test_ok_is_sent_and_argv_exact():
    fake = FakeRun(proc(0, b""), proc(0, b"OK message sent\nextra"))
    assert sender(fake)("hello") == an.SENT
    dry, real = fake.calls
    assert real[0] == ["tg", "--automated", "--tag", "report", "--format", "plain", "hello"]
    assert dry[0] == [
        "tg",
        "--dry-run",
        "--automated",
        "--tag",
        "report",
        "--format",
        "plain",
        "hello",
    ]
    for _, kw in fake.calls:
        assert kw["stdin"] == subprocess.DEVNULL and not kw.get("shell") and kw["timeout"] == 30


def test_uncertain_outcomes():
    for real in (
        proc(0, b"done\n"),
        proc(0, b""),
        proc(1, b"OK"),
        proc(-9),
        proc(3),
        subprocess.TimeoutExpired("tg", 30),
        OSError("x"),
    ):
        assert sender(FakeRun(proc(0, b""), real))("t") == an.UNCERTAIN, real


def test_definite_failures():
    for real in (proc(78), proc(2), FileNotFoundError("tg")):
        assert sender(FakeRun(proc(0, b""), real))("t") == an.FAILED, real
    assert sender(FakeRun(FileNotFoundError("tg")))("t") == an.FAILED


def test_failed_item_retried_even_though_previous_report_contains_it(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.FAILED)
    rep = report([conv()])
    run(rep, None, store, rec, now=0)
    rec.verdict = an.SENT
    out = run(rep, rep, store, rec, now=5000)
    assert out["outcome"] == "sent" and len(rec.texts) == 2
    assert run(rep, rep, store, rec, now=10**7)["outcome"] == "none-significant"


def test_failed_item_no_longer_actionable_is_not_retried(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.FAILED)
    run(report([conv()]), None, store, rec, now=0)
    gone = report([conv(state="done")])
    assert run(gone, report([conv()]), store, rec, now=5000)["outcome"] == "none-significant"


def test_uncertain_item_not_retried_via_unchanged_path(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.UNCERTAIN)
    rep = report([conv()])
    run(rep, None, store, rec, now=0)
    run(rep, rep, store, rec, now=10**7)
    assert len(rec.texts) == 1


def test_failed_unavailable_account_retried(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder(an.FAILED)
    down = report(status="unavailable", reason="cdp-unreachable")
    run(down, None, store, rec, now=0)
    rec.verdict = an.SENT
    assert run(down, down, store, rec, now=5000)["outcome"] == "sent"


def test_overflow_items_are_logged_and_not_resent(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    rep = report([conv(cid=f"c{i}", fp=f"f{i}") for i in range(14)])
    out = run(rep, None, store, rec, max_items=10)
    assert out["sent_items"] == 14 and rec.texts[0].splitlines()[-1] == "+4 more"
    again = run(rep, None, store, rec, max_items=10)
    assert (
        again["outcome"] == "suppressed" and again["suppressed_sent"] == 14 and len(rec.texts) == 1
    )


def test_default_max_items_is_20():
    import inspect

    assert inspect.signature(an.notify_report).parameters["max_items"].default == 20


def test_the_delivery_intent_is_durable_before_the_message_is_sent(tmp_path):
    store, seen = AuditStore(tmp_path), []

    def send(text):
        seen.append([e["kind"] for e in store.notification_log().read_all()])
        return an.SENT

    assert run(report([conv()]), None, store, send)["outcome"] == "sent"
    assert seen == [["delivery_intent"]]


def test_a_crash_during_send_never_replays_the_message(tmp_path):
    store = AuditStore(tmp_path)

    def crash(text):
        raise KeyboardInterrupt

    try:
        run(report([conv()]), None, store, crash)
    except KeyboardInterrupt:
        pass
    rec = Recorder()
    out = run(report([conv()]), None, AuditStore(tmp_path), rec, now=99999.0)
    assert rec.texts == [] and out["outcome"] == "suppressed"
    assert out["suppressed_uncertain"] == 1


def test_a_failed_intent_write_sends_nothing(tmp_path):
    store, rec = AuditStore(tmp_path), Recorder()
    log = store.notification_log()
    log.append = lambda event: (_ for _ in ()).throw(OSError("disk"))
    store.notification_log = lambda: log
    out = run(report([conv()]), None, store, rec)
    assert rec.texts == [] and out["outcome"] == "error"


def test_the_tg_receipt_is_the_proof_and_is_persisted(tmp_path):
    fake = FakeRun(proc(0, b""), proc(0, b"OK sent #4321\n"))
    verdict = sender(fake)("hello")
    assert verdict == an.SENT and verdict.receipt == "4321"
    store = AuditStore(tmp_path)
    out = run(report([conv()]), None, store, lambda text: verdict)
    assert out["receipt"] == "4321"
    sent = [e for e in store.notification_log().read_all() if e["kind"] == "sent"]
    assert sent and sent[0]["receipt"] == "4321"


def test_two_overlapping_audits_claim_the_delivery_once(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    barrier = threading.Barrier(2)
    store = AuditStore(tmp_path)
    real = store.notification_log()

    class Slow:
        def read_all(self):
            snapshot = real.read_all()
            try:
                barrier.wait(timeout=0.5)  # both readers see the same empty history
            except threading.BrokenBarrierError:
                pass
            return snapshot

        def append(self, event):
            real.append(event)

    store.notification_log = lambda: Slow()
    sends = []

    def send(text):
        sends.append(text)
        return an.SENT

    with ThreadPoolExecutor(2) as pool:
        results = list(
            pool.map(
                lambda _: an.notify_report(report([conv()]), None, store, now=1, send=send),
                range(2),
            )
        )
    assert len(sends) == 1
    assert sorted(r["outcome"] for r in results) == ["busy", "sent"]
