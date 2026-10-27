"""The validation corpus and its metrics: honest numbers, fixed corpus, never a rubber stamp."""

from __future__ import annotations

import json

import pytest

from pmlib.chatgpt_control import audit_bench as bench
from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_decision as dec
from tests.audit_support import laya

# Categories whose verdict must never be ``done`` however confident the model is: a deterministic
# rule (blocked wait, unfinished promise, structure) owns them.
DETERMINISTIC_NOT_DONE = {
    "expired-permission",
    "physical-step",
    "login-required",
    "pending-owner-approval",
    "announced-continuation",
    "tests-pass-not-installed",
    "truncated-context",
    "wrong-or-missing-account",
    "running-generation",
    "image-only-final",
    "bare-ack-no-goal",
    "final-before-latest-user",
    "end-of-turn-omission",
}


def always_done():
    return laya(task_finished=0.99, steps_remaining=0.01, blocked_on_human=0.01)


def test_corpus_has_both_splits_with_valid_unique_labelled_cases():
    cases = bench.load_cases()
    assert {c.split for c in cases} == {"tune", "heldout"}
    assert len({c.id for c in cases}) == len(cases)
    assert all(c.expected in bench.LABELS and c.layout in bench.LAYOUTS for c in cases)
    assert sum(c.split == "tune" for c in cases) >= 40
    assert sum(c.split == "heldout" for c in cases) >= 40
    for label in bench.LABELS:
        assert any(c.expected == label for c in cases if c.split == "heldout"), label
        assert any(c.expected == label for c in cases if c.split == "tune"), label


def test_the_corpus_is_synthetic_ascii_json_with_no_credentials():
    for path in bench.corpus_files():
        raw = path.read_bytes()
        assert raw.isascii()  # Russian text travels as JSON \u escapes
        text = raw.decode()
        for marker in ("sk-", "ghp_", "BEGIN PRIVATE", "Bearer ", "password="):
            assert marker not in text
        json.loads(text)


def test_split_selection_loads_only_what_was_asked_for():
    assert {c.split for c in bench.load_cases(("tune",))} == {"tune"}
    assert {c.split for c in bench.load_cases(("heldout",))} == {"heldout"}
    with pytest.raises(ValueError):
        bench.load_cases(("tune", "everything"))


def test_bench_digest_is_stable_and_follows_the_corpus_content(tmp_path):
    first = bench.bench_digest()
    assert first == bench.bench_digest() and len(first) == 64
    copy = tmp_path / "bench"
    copy.mkdir()
    for path in bench.corpus_files():
        (copy / path.name).write_bytes(path.read_bytes())
    assert bench.bench_digest(copy) == first
    victim = copy / "cases_tune.json"
    victim.write_bytes(victim.read_bytes().replace(b"Canberra", b"Ottawa!!"))
    assert bench.bench_digest(copy) != first


@pytest.mark.parametrize(
    "bad",
    [
        {"expected": "maybe"},
        {"layout": "sideways"},
        {"surprise": 1},
        {"id": ""},
        {"request": 5},
    ],
)
def test_malformed_cases_are_rejected(tmp_path, bad):
    case = {"id": "x-1", "category": "c", "expected": "done", "request": "q", "final": "a"}
    case.update(bad)
    directory = tmp_path / "bench"
    directory.mkdir()
    (directory / "cases_tune.json").write_text(json.dumps([case]))
    (directory / "cases_heldout.json").write_text("[]")
    with pytest.raises(ValueError):
        bench.load_cases(directory=directory)


def test_duplicate_ids_across_files_are_rejected(tmp_path):
    case = {"id": "dup", "category": "c", "expected": "done", "request": "q", "final": "a"}
    directory = tmp_path / "bench"
    directory.mkdir()
    (directory / "cases_tune.json").write_text(json.dumps([case]))
    (directory / "cases_heldout.json").write_text(json.dumps([case]))
    with pytest.raises(ValueError):
        bench.load_cases(directory=directory)


@pytest.mark.parametrize("case", bench.load_cases(), ids=lambda c: c.id)
def test_every_case_builds_valid_evidence_through_the_real_native_parser(case):
    e = bench.case_evidence(case)
    assert e.account == "home" and e.messages
    if case.layout == "wrong-account":
        assert e.observed_account not in (None, "home")
    elif case.layout == "missing-account":
        assert e.observed_account is None
    else:
        assert e.observed_account == "home"
    assert e.progress_indicator == (case.layout == "running")
    assert e.archived == (case.layout == "archived")
    assert e.context_truncated == (case.layout == "truncated")
    if case.layout == "image-only":
        assert e.last_final is not None and e.last_final.image_artifacts >= 1
        assert e.final_text == ""
    if case.layout == "bare-go":
        assert e.request_id is None
    if case.layout == "end-turn-no-reply":
        assert e.end_turn_without_reply
    if case.layout == "stale-in-progress":
        first_assistant = next(m for m in e.messages if m.role == "assistant")
        assert first_assistant.in_progress


def test_score_counts_exact_precision_recall_and_the_unsafe_errors():
    rows = [
        ("done", "done"),
        ("done", "unknown"),
        ("incomplete", "done"),  # the unsafe error
        ("blocked", "blocked"),
        ("incomplete", "blocked"),  # a false block
        ("done", "incomplete"),  # a false incomplete
        ("unknown", "unknown"),
        ("working", "working"),
    ]
    s = bench.score(rows)
    assert s["n"] == 8 and s["exact"] == pytest.approx(4 / 8)
    assert s["false_done"] == 1 and s["false_block"] == 1 and s["false_incomplete"] == 1
    assert s["abstained"] == 2 and s["abstention_rate"] == pytest.approx(2 / 8)
    assert s["coverage"] == pytest.approx(6 / 8)
    assert s["abstained_on_decidable"] == 1
    assert s["per_label"]["done"] == {
        "precision": pytest.approx(1 / 2),
        "recall": pytest.approx(1 / 3),
        "support": 3,
        "predicted": 2,
    }
    assert s["per_label"]["working"]["precision"] == 1.0
    assert s["per_label"]["incomplete"]["recall"] == pytest.approx(0.0)


def test_score_reports_none_instead_of_inventing_a_ratio_for_empty_labels():
    s = bench.score([("done", "unknown")])
    assert s["per_label"]["done"]["precision"] is None
    assert s["per_label"]["blocked"]["recall"] is None
    assert bench.score([])["exact"] is None


def test_pipeline_with_an_always_done_classifier_still_never_says_done_for_vetoed_cases():
    tune = [c for c in bench.load_cases(("tune",)) if c.category in DETERMINISTIC_NOT_DONE]
    assert len(tune) >= 15
    for case in tune:
        got = bench.predict_pipeline(case, lambda e: always_done())
        assert got.label != "done", (case.id, got)


def test_pipeline_labels_are_the_verdict_task_state_and_carry_reasons():
    case = next(c for c in bench.load_cases(("tune",)) if c.id == "t-expired-1")
    got = bench.predict_pipeline(case, lambda e: always_done())
    assert got.label == "blocked" and "permission-expired" in got.reasons


def test_with_the_classifier_down_no_case_can_be_done():
    for case in bench.load_cases(("tune",)):
        got = bench.predict_pipeline(case, lambda e: clf.unavailable("down"))
        assert got.label != "done", case.id


def test_baseline_label_rule_reproduces_the_old_pipeline_mapping():
    rule = bench.baseline_label
    assert (
        rule({"task_finished": 0.95, "steps_remaining": 0.02, "blocked_on_human": 0.02}) == "done"
    )
    assert (
        rule({"task_finished": 0.9, "steps_remaining": 0.1, "blocked_on_human": 0.9}) == "blocked"
    )
    assert (
        rule({"task_finished": 0.1, "steps_remaining": 0.1, "blocked_on_human": 0.1})
        == "incomplete"
    )
    assert (
        rule({"task_finished": 0.5, "steps_remaining": 0.4, "blocked_on_human": 0.1}) == "unknown"
    )


def test_pass_rule_demands_zero_false_done_and_real_coverage():
    good = {
        "tune": {"false_done": 0},
        "heldout": {"false_done": 0, "false_block": 0, "exact": 0.8, "coverage": 0.8},
    }
    assert bench.passes(good)
    for key, value in (("false_done", 1), ("false_block", 1), ("exact", 0.5), ("coverage", 0.2)):
        bad = json.loads(json.dumps(good))
        bad["heldout"][key] = value
        assert not bench.passes(bad), key
    bad = json.loads(json.dumps(good))
    bad["tune"]["false_done"] = 1
    assert not bench.passes(bad)
    all_unknown = {
        "tune": {"false_done": 0},
        "heldout": {"false_done": 0, "false_block": 0, "exact": 0.8, "coverage": 0.0},
    }
    assert not bench.passes(all_unknown)  # abstaining on everything is not validation


def test_default_thresholds_digest_matches_the_logic_under_test():
    assert dec.thresholds_digest(dec.DEFAULT_THRESHOLDS) == bench.current_thresholds_digest()


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


class FakeLayaClassifier:
    """Stands in for the socket client: scores everything done-like, answers choices 'done'."""

    def __init__(self, status="ok"):
        self.status = status
        self.calls = 0

    def profile(self):
        return "convaiinnovations/laya@512"

    def ask(self, message, questions):
        self.calls += 1
        if self.status != "ok":
            return clf.Answers(self.status, {}, "convaiinnovations/laya@512", "down", {})
        answers = {}
        for name, q in questions.items():
            if q["type"] == "choice":
                answers[name] = {"type": "choice", "choice": "done"}
            else:
                high = name in ("task_finished", "response_finished")
                answers[name] = {"type": "noul", "noul": 0.95 if high else 0.02}
        return clf.Answers("ok", answers, "convaiinnovations/laya@512", "", dict(REAL_META))

    def classify(self, e):
        return laya(task_finished=0.95, steps_remaining=0.02, blocked_on_human=0.02)


def test_a_full_run_reports_every_system_and_split_and_never_rubber_stamps(tmp_path):
    import io

    out = io.StringIO()
    fake = FakeLayaClassifier()
    code = bench.run(classifier=fake, out=out, as_json=True, record=True, directory=tmp_path / "a")
    report = json.loads(out.getvalue())
    assert set(report["systems"]) == {"raw-noul-v1", "raw-choice", "pipeline"}
    for body in report["systems"].values():
        assert set(body["by_split"]) == {"tune", "heldout"}
    # An always-done model is exactly the unsafe baseline: the raw systems make false-dones...
    assert report["systems"]["raw-choice"]["by_split"]["heldout"]["false_done"] > 0
    # ...and the pipeline's deterministic rules keep its false-dones strictly below the baseline.
    pipe = report["systems"]["pipeline"]["by_split"]["heldout"]["false_done"]
    assert pipe < report["systems"]["raw-choice"]["by_split"]["heldout"]["false_done"]
    assert report["model"] == "convaiinnovations/laya@512"
    assert report["bench_digest"] == bench.bench_digest()
    assert report["model_probe"] == REAL_META
    from pmlib.chatgpt_control import audit_validation as val

    assert val.load_record(tmp_path / "a") is None  # a failing run never writes a record
    assert report["passed"] is False and code == 1


def test_a_passing_run_records_its_validation(tmp_path, monkeypatch):
    import io

    from pmlib.chatgpt_control import audit_validation as val

    monkeypatch.setattr(bench, "passes", lambda by_split: True)
    out = io.StringIO()
    code = bench.run(
        classifier=FakeLayaClassifier(),
        out=out,
        as_json=True,
        record=True,
        directory=tmp_path / "ok",
    )
    saved = val.load_record(tmp_path / "ok")
    assert code == 0 and saved is not None and saved.passed is True
    assert saved.model == "convaiinnovations/laya@512"


def test_markdown_report_states_the_verdict_the_counterexamples_and_advisory_status():
    import io

    out = io.StringIO()
    bench.run(classifier=FakeLayaClassifier(), out=out)
    text = out.getvalue()
    assert "3/12" in text and "2/8" in text and "ADVISORY" in text
    assert ("PASSED" in text) != ("NOT VALIDATED" in text)
    assert "| heldout |" in text and "| tune |" in text
    assert "comparab" in text.lower() and "entropy" in text.lower()


def test_every_system_is_also_reported_per_language_route():
    import io

    out = io.StringIO()
    bench.run(classifier=FakeLayaClassifier(), out=out, as_json=True)
    report = json.loads(out.getvalue())
    cases = bench.load_cases()
    expected = {"english": 0, "multilingual": 0}
    for case in cases:
        expected[clf.route_for(case.request + " " + case.final)] += 1
    assert expected["multilingual"] > 0 and expected["english"] > 0
    for body in report["systems"].values():
        assert {r: body["by_route"][r]["n"] for r in expected} == expected
    assert report["routes"] == expected
    assert "comparability" in report and "entropy" in report["comparability"]


def test_a_run_through_the_real_wire_binds_the_record_to_the_actual_checkpoints(tmp_path):
    import io

    from tests.laya_support import FakeLaya

    laya_dir = tmp_path / "state" / "task-cli" / "classify"
    server = FakeLaya(laya_dir, "laya-0.23.1.sock")
    try:
        out = io.StringIO()
        classifier = clf.LayaClassifier(socket_path=server.path)
        bench.run(classifier=classifier, out=out, as_json=True)
        report = json.loads(out.getvalue())
    finally:
        server.close()
    assert set(report["model"].split("|")) == {
        "convaiinnovations/laya@512",
        "convaiinnovations/laya@2048",
        "convaiinnovations/laya-multilingual@2048",
    }
    assert report["model_probe"]["checkpoint"] == "convaiinnovations/laya"
    sent = [json.loads(raw) for raw in server.requests]
    assert {s["model"] for s in sent} == {"english", "multilingual"}
    assert all(type(s["max_len"]) is int for s in sent)


def test_an_unavailable_classifier_is_exit_2_and_records_nothing(tmp_path):
    import io

    out = io.StringIO()
    code = bench.run(
        classifier=FakeLayaClassifier("unavailable"), out=out, record=True, directory=tmp_path / "b"
    )
    assert code == 2 and out.getvalue() == "" and not (tmp_path / "b").exists()


def test_a_tune_only_run_never_records(tmp_path):
    import io

    bench.run(
        classifier=FakeLayaClassifier(),
        out=io.StringIO(),
        splits=("tune",),
        record=True,
        directory=tmp_path / "c",
    )
    assert not (tmp_path / "c").exists()


class Ticking:
    """A monotonic clock that advances one second per reading."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 1.0
        return self.now


def test_an_exhausted_budget_is_a_partial_never_a_pass_and_records_nothing(tmp_path):
    import io

    out = io.StringIO()
    code = bench.run(
        classifier=FakeLayaClassifier(),
        out=out,
        as_json=True,
        record=True,
        directory=tmp_path / "p",
        budget_seconds=12.0,
        monotonic=Ticking(),
    )
    report = json.loads(out.getvalue())
    assert code == 3 and report["partial"] is True and report["passed"] is False
    assert 0 < report["evaluated"] < report["planned"]
    assert not (tmp_path / "p").exists()


def test_a_partial_markdown_report_says_so_and_is_not_validated():
    import io

    out = io.StringIO()
    bench.run(classifier=FakeLayaClassifier(), out=out, budget_seconds=12.0, monotonic=Ticking())
    assert "PARTIAL" in out.getvalue() and "PASSED" not in out.getvalue()


def test_a_budget_that_expires_during_the_last_prediction_is_still_partial(tmp_path):
    import io

    clock = [0.0]
    last_ask = 1 + 2 * len(bench.load_cases())  # the probe plus the two raw systems' calls

    class SlowLast(FakeLayaClassifier):
        def ask(self, message, questions):
            reply = super().ask(message, questions)
            if self.calls == last_ask:
                clock[0] = 100.0
            return reply

    out = io.StringIO()
    code = bench.run(
        classifier=SlowLast(),
        out=out,
        as_json=True,
        record=True,
        directory=tmp_path / "late",
        budget_seconds=1.0,
        monotonic=lambda: clock[0],
    )
    report = json.loads(out.getvalue())
    assert code == 3 and report["partial"] is True and report["passed"] is False
    assert not (tmp_path / "late").exists()


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_a_non_finite_or_non_positive_budget_is_refused_before_anything_runs(bad):
    import io

    out, fake = io.StringIO(), FakeLayaClassifier()
    assert bench.run(classifier=fake, out=out, budget_seconds=bad) == 2
    assert out.getvalue() == "" and fake.calls == 0
