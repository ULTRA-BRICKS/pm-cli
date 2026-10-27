"""Honest validation of the task-completion audit against a fixed, synthetic corpus.

Accessed via: ``pm chatgpt audit bench`` (runs it against the real warm Laya socket), the
validation gate (``bench_digest`` ties a persisted record to this exact corpus) and the tests.

The corpus (``bench/cases_tune.json`` and ``bench/cases_heldout.json``) holds small synthetic
cases, each with an expected ``task_state`` and a split: prompts, thresholds and rules are tuned on
``tune`` ONLY, and ``heldout`` is measured untouched. Every case becomes evidence through the real
native parser (a synthetic native payload per ``layout``), so structure rules (stale in-progress
messages, truncated windows, wrong accounts, image-only finals) are exercised, not mocked.

Systems measured (each per split): the RAW baselines a naive integration would ship (per-question
noul with the original completion-v1 wording, and a four-way ``choice`` with a long rubric), both
mapped to a label with no deterministic precedence at all, and the full PIPELINE (deterministic
evidence and precedence from ``audit_decision`` plus the classifier). Metrics are exact accuracy,
per-label precision and recall, FALSE_DONE (predicting ``done`` where the task is not: the unsafe
error), false blocks, false incompletes, the abstention (``unknown``) rate and coverage. A run is
``passes`` only with zero false-done on both splits, no held-out false block, and real held-out
accuracy and coverage: abstaining on everything is reported as a failure, not as safety.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from . import audit_native
from .audit_classifier import ROUTES, Classification
from .audit_decision import DEFAULT_THRESHOLDS, decide, thresholds_digest
from .audit_evidence import ConversationEvidence

SPLITS = ("tune", "heldout")
LABELS = ("done", "incomplete", "blocked", "working", "unknown")
LAYOUTS = (
    "answered",
    "go-after",
    "bare-go",
    "final-before-user",
    "no-final",
    "stale-in-progress",
    "running",
    "image-only",
    "truncated",
    "wrong-account",
    "missing-account",
    "end-turn-no-reply",
    "archived",
    "late-commentary",
)
CORPUS_FILES = {"tune": "cases_tune.json", "heldout": "cases_heldout.json"}
_CASE_KEYS = {
    "id",
    "category",
    "expected",
    "layout",
    "request",
    "final",
    "prior_request",
    "prior_final",
}
_REQUIRED = {"id", "category", "expected", "request", "final"}
NOW = 1792254000.0
ACCOUNT = "home"
CONVERSATION = "bench-conversation"

# The pass rule is fixed here, before any held-out number is looked at.
PASS_MIN_HELDOUT_EXACT = 0.75
PASS_MIN_HELDOUT_COVERAGE = 0.70
# One global wall-clock budget for the whole run; running out is exit 3, never a pass.
DEFAULT_BUDGET_SECONDS = 180.0
PARTIAL_EXIT = 3


@dataclass(frozen=True)
class Case:
    id: str
    split: str
    category: str
    expected: str
    layout: str
    request: str
    final: str
    prior_request: str
    prior_final: str


# --- corpus -----------------------------------------------------------------------------------


def _corpus_dir(directory: Path | None):
    return directory if directory is not None else resources.files("pmlib.chatgpt_control.bench")


def corpus_files(directory: Path | None = None) -> list:
    root = _corpus_dir(directory)
    return [root.joinpath(CORPUS_FILES[split]) for split in SPLITS]


class CorpusError(ValueError):
    """The corpus is not the expected shape."""


_SPLIT_HEADER = (
    "| split | n | exact | false_done | false_block | false_incomplete | abstain | coverage |"
)


def _case(split: str, raw: object) -> Case:
    if not isinstance(raw, dict):
        raise CorpusError("a case must be an object")
    unknown = set(raw) - _CASE_KEYS
    missing = _REQUIRED - set(raw)
    if unknown or missing:
        raise ValueError(f"case keys: unknown {sorted(unknown)} missing {sorted(missing)}")
    text_fields = ("id", "category", "request", "final", "prior_request", "prior_final")
    for key in text_fields:
        if key in raw and not isinstance(raw[key], str):
            raise ValueError(f"case {key} must be a string")
    if not raw["id"]:
        raise ValueError("case id must not be empty")
    layout = raw.get("layout", "answered")
    if raw["expected"] not in LABELS or layout not in LAYOUTS:
        raise ValueError(f"case {raw['id']}: unknown expected label or layout")
    return Case(
        id=raw["id"],
        split=split,
        category=raw["category"],
        expected=raw["expected"],
        layout=layout,
        request=raw["request"],
        final=raw["final"],
        prior_request=raw.get("prior_request", ""),
        prior_final=raw.get("prior_final", ""),
    )


def load_cases(splits=SPLITS, *, directory: Path | None = None) -> list[Case]:
    """The corpus cases of the requested splits, strictly validated (ids unique across files)."""
    splits = tuple(splits)
    if not splits or any(s not in SPLITS for s in splits):
        raise ValueError(f"splits must be drawn from {SPLITS}")
    root = _corpus_dir(directory)
    cases: list[Case] = []
    seen: set[str] = set()
    for split in SPLITS:  # every file is validated, so a bad held-out file cannot hide
        data = json.loads(root.joinpath(CORPUS_FILES[split]).read_text(encoding="ascii"))
        if not isinstance(data, list):
            raise CorpusError(f"{CORPUS_FILES[split]} must be a JSON array")
        for raw in data:
            case = _case(split, raw)
            if case.id in seen:
                raise ValueError(f"duplicate case id {case.id}")
            seen.add(case.id)
            if split in splits:
                cases.append(case)
    return cases


def bench_digest(directory: Path | None = None) -> str:
    """SHA-256 over the exact bytes of both corpus files: a record applies to this corpus only."""
    digest = hashlib.sha256(b"pm-audit-bench-v1")
    for path in corpus_files(directory):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def current_thresholds_digest() -> str:
    return thresholds_digest(DEFAULT_THRESHOLDS)


# --- cases as native payloads -----------------------------------------------------------------


def _node(nid: str, parent: str, role: str, text: str, at: float, **kw) -> dict:
    content = kw.pop("content", None) or {"content_type": "text", "parts": [text]}
    message = {
        "id": nid,
        "author": {"role": role},
        "create_time": NOW + at,
        "content": content,
        "status": kw.pop("status", "finished_successfully"),
        "end_turn": kw.pop("end_turn", None),
        "recipient": "all",
        "channel": None,
        "metadata": {},
    }
    return {"id": nid, "message": message, "parent": parent, "children": []}


def build_payload(case: Case) -> dict:
    """A synthetic native conversation payload for the case's layout."""
    layout = case.layout
    nodes: list[dict] = []

    def chain(*specs) -> None:
        parent = "root"
        for nid, role, text, at, kw in specs:
            nodes.append(_node(nid, parent, role, text, at, **kw))
            parent = nid

    if layout == "go-after":
        chain(
            ("u1", "user", case.request, -900, {}),
            ("a1", "assistant", case.prior_final, -850, {"end_turn": False}),
            ("u2", "user", "go", -700, {}),
            ("a2", "assistant", case.final, -650, {"end_turn": True}),
        )
    elif layout == "final-before-user":
        chain(
            ("u0", "user", case.prior_request, -5000, {}),
            ("a0", "assistant", case.prior_final, -4900, {"end_turn": True}),
            ("u1", "user", case.request, -3000, {}),
        )
    elif layout == "no-final":
        chain(("u1", "user", case.request, -5000, {}))
    elif layout == "stale-in-progress":
        chain(
            ("u0", "user", case.prior_request, -90000, {}),
            ("a0", "assistant", case.prior_final, -89000, {"status": "in_progress"}),
            ("u1", "user", case.request, -700, {}),
            ("a1", "assistant", case.final, -650, {"end_turn": True}),
        )
    elif layout == "running":
        chain(
            ("u1", "user", case.request, -300, {}),
            ("a1", "assistant", case.final, -200, {"status": "in_progress"}),
        )
    elif layout == "image-only":
        image = {"content_type": "image_asset_pointer", "asset_pointer": "file-service://x"}
        content = {"content_type": "multimodal_text", "parts": [image, ""]}
        chain(
            ("u1", "user", case.request, -700, {}),
            ("a1", "assistant", "", -650, {"end_turn": True, "content": content}),
        )
    elif layout == "truncated":
        nodes.append(_node("a1", "gone", "assistant", case.final, -650, end_turn=True))
    elif layout == "end-turn-no-reply":
        chain(
            ("u1", "user", case.request, -700, {}),
            ("a1", "assistant", "", -650, {"end_turn": True}),
        )
    elif layout == "late-commentary":
        chain(
            ("u1", "user", case.request, -700, {}),
            ("a1", "assistant", case.final, -650, {"end_turn": True}),
            ("a2", "assistant", case.prior_final, -600, {"end_turn": False}),
        )
    else:  # answered, bare-go, wrong-account, missing-account, archived
        chain(
            ("u1", "user", case.request, -700, {}),
            ("a1", "assistant", case.final, -650, {"end_turn": True}),
        )
    mapping = {n["id"]: n for n in nodes}
    if layout != "truncated":
        mapping["root"] = {"id": "root", "message": None, "parent": None, "children": []}
    return {
        "conversation_id": CONVERSATION,
        "title": "bench",
        "update_time": NOW - 300,
        "current_node": nodes[-1]["id"],
        "mapping": mapping,
    }


def case_evidence(case: Case, *, now: float = NOW) -> ConversationEvidence:
    """The case as evidence, built by the same native parser a real read goes through."""
    listed = audit_native.ListedConversation(
        conversation_id=CONVERSATION,
        updated_at=NOW - 300,
        archived=case.layout == "archived",
        project_id=None,
    )
    observed = {"wrong-account": "glide.vc", "missing-account": None}.get(case.layout, ACCOUNT)
    return audit_native.build_evidence(
        build_payload(case),
        listed=listed,
        account=ACCOUNT,
        observed_account=observed,
        now=now,
        running=case.layout == "running",
    )


# --- systems ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Prediction:
    case_id: str
    label: str
    reasons: tuple[str, ...] = ()
    detail: str = ""


def predict_pipeline(
    case: Case,
    classify: Callable[[ConversationEvidence], Classification | None],
    *,
    now: float = NOW,
) -> Prediction:
    """The full pipeline: deterministic evidence and precedence, then the classifier if needed."""
    e = case_evidence(case, now=now)
    verdict = decide(e, lambda: classify(e), now=now)
    return Prediction(case.id, verdict.task_state, verdict.reasons)


def baseline_label(scores: dict) -> str:
    """The original completion-v1 score mapping with NO deterministic precedence (the baseline)."""
    if scores["blocked_on_human"] >= 0.60:
        return "blocked"
    if scores["steps_remaining"] >= 0.60 or scores["task_finished"] <= 0.30:
        return "incomplete"
    if scores["task_finished"] >= 0.85 and scores["steps_remaining"] < 0.35:
        return "done"
    return "unknown"


# --- metrics ----------------------------------------------------------------------------------


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def score(rows: list[tuple[str, str]]) -> dict:
    """Metrics for ``(expected, predicted)`` pairs; empty denominators are ``None``, never 0 or 1."""
    n = len(rows)
    per_label = {}
    for label in LABELS:
        tp = sum(1 for e, p in rows if e == label and p == label)
        predicted = sum(1 for _, p in rows if p == label)
        support = sum(1 for e, _ in rows if e == label)
        per_label[label] = {
            "precision": _ratio(tp, predicted),
            "recall": _ratio(tp, support),
            "support": support,
            "predicted": predicted,
        }
    abstained = sum(1 for _, p in rows if p == "unknown")
    return {
        "n": n,
        "exact": _ratio(sum(1 for e, p in rows if e == p), n),
        "false_done": sum(1 for e, p in rows if p == "done" and e != "done"),
        "false_block": sum(1 for e, p in rows if p == "blocked" and e != "blocked"),
        "false_incomplete": sum(1 for e, p in rows if p == "incomplete" and e != "incomplete"),
        "abstained": abstained,
        "abstention_rate": _ratio(abstained, n),
        "abstained_on_decidable": sum(1 for e, p in rows if p == "unknown" and e != "unknown"),
        "coverage": _ratio(n - abstained, n),
        "per_label": per_label,
    }


def passes(by_split: dict) -> bool:
    """True only for a real validation: no false-done anywhere, no held-out false block, and the
    held-out accuracy and coverage clear their floors (so total abstention cannot pass)."""
    tune, held = by_split.get("tune", {}), by_split.get("heldout", {})
    try:
        return (
            tune["false_done"] == 0
            and held["false_done"] == 0
            and held["false_block"] == 0
            and held["exact"] is not None
            and held["exact"] >= PASS_MIN_HELDOUT_EXACT
            and held["coverage"] is not None
            and held["coverage"] >= PASS_MIN_HELDOUT_COVERAGE
        )
    except (KeyError, TypeError):
        return False


# --- the raw baselines a naive integration would ship -----------------------------------------

# Frozen copy of the original completion-v1 wording: the baseline must not move when the
# production questions are improved.
V1_QUESTIONS = {
    "task_finished": (
        "Considering the user request and the assistant final answer quoted below, did the "
        "assistant fully deliver every concrete thing the user asked for (actual results, not "
        "only a plan or a promise), with nothing still required before the request counts as "
        "complete?"
    ),
    "steps_remaining": (
        "Does the assistant final answer admit or show that some requested part is not yet done, "
        "not installed, not verified, not tested, only planned, or promised for later?"
    ),
    "blocked_on_human": (
        "Is the assistant waiting for the user or another person to act first: grant permission "
        "or approval, log in, make a decision, or take a physical action such as plugging in, "
        "moving or flashing a device?"
    ),
    "response_finished": (
        "Is the assistant final answer a complete, self-contained reply that is not cut off or "
        "abandoned halfway?"
    ),
}
# A reconstruction of the pilot's long four-way rubric (the original wording was not kept).
CHOICE_QUESTION = {
    "type": "choice",
    "instructions": "Judge whether the assistant finished the user's TASK, not merely whether it "
    "replied.",
    "criteria": {
        "done": "The assistant delivered every concrete thing the user asked for, as actual "
        "results rather than a plan or a promise.",
        "incomplete": "The assistant only planned, promised later work, left a requested part "
        "undone, or admits something is not installed, not tested or not finished.",
        "blocked": "The assistant cannot continue until a person acts: grants permission or "
        "approval, logs in, or performs a physical step.",
        "unknown": "There is not enough context to tell whether the task is finished.",
    },
}
# Counterexamples recorded by main before this benchmark existed (kept as documented failures).
PILOT_RAW_CHOICE = "3/12 exact (long four-way rubric, hand-labelled pilot pairs)"
PREVIOUS_PIPELINE = "2/8 exact (completion-v1 pipeline; all 8 classified blocked)"


def _v1_message(request: str, final: str) -> str:
    from . import audit_classifier as c

    return "\n".join(
        (
            c.PREAMBLE,
            "",
            c.REQUEST_OPEN,
            c.neutralize(request),
            c.REQUEST_CLOSE,
            "",
            c.FINAL_OPEN,
            c.neutralize(final),
            c.FINAL_CLOSE,
        )
    )


def predict_raw_noul(case: Case, ask) -> Prediction:
    """Per-question noul over the raw request/final pair, scores mapped with no precedence."""
    questions = {k: {"type": "noul", "instructions": v} for k, v in V1_QUESTIONS.items()}
    got = ask(_v1_message(case.request, case.final), questions)
    if got.status != "ok":
        return Prediction(case.id, "unknown", (f"classifier-{got.status}",))
    try:
        scores = {k: float(got.answers[k]["noul"]) for k in V1_QUESTIONS}
    except (KeyError, TypeError, ValueError):
        return Prediction(case.id, "unknown", ("classifier-invalid",))
    return Prediction(case.id, baseline_label(scores))


def predict_raw_choice(case: Case, ask) -> Prediction:
    """The four-way choice baseline: the winning label, nothing else."""
    got = ask(_v1_message(case.request, case.final), {"verdict": CHOICE_QUESTION})
    choice = got.answers.get("verdict", {}).get("choice") if got.status == "ok" else None
    label = choice if choice in ("done", "incomplete", "blocked", "unknown") else "unknown"
    return Prediction(case.id, label)


# --- the run ----------------------------------------------------------------------------------


def case_route(case: Case) -> str:
    """The language route the classifier would pick for this case (from its own texts)."""
    from .audit_classifier import route_for

    return route_for(f"{case.request} {case.final}")


def evaluate(
    cases: list[Case], classifier, *, now: float = NOW, expired: Callable[[], bool] | None = None
) -> dict:
    """Metrics per system and split, measured through ``classifier`` (a ``LayaClassifier``).

    ``expired`` is the global budget: once it reports True no further case is predicted, and each
    system is scored over the cases it did reach (``evaluated``/``total`` say how many).
    """
    systems = {
        "raw-noul-v1": lambda c: predict_raw_noul(c, classifier.ask),
        "raw-choice": lambda c: predict_raw_choice(c, classifier.ask),
        "pipeline": lambda c: predict_pipeline(c, classifier.classify, now=now),
    }
    splits = sorted({c.split for c in cases})
    results: dict = {}
    # The pipeline is the system that decides things, so it spends the budget first.
    for name in ("pipeline", "raw-choice", "raw-noul-v1"):
        preds: dict[str, Prediction] = {}
        for c in cases:
            if expired is not None and expired():
                break
            preds[c.id] = systems[name](c)
        reached = [c for c in cases if c.id in preds]
        results[name] = {
            "evaluated": len(reached),
            "total": len(cases),
            "cases": {c.id: preds[c.id].label for c in reached},
            "by_split": {
                s: score([(c.expected, preds[c.id].label) for c in reached if c.split == s])
                for s in splits
            },
            "by_route": {
                r: score([(c.expected, preds[c.id].label) for c in reached if case_route(c) == r])
                for r in ROUTES
            },
            "errors": {
                s: [
                    {
                        "id": c.id,
                        "expected": c.expected,
                        "got": preds[c.id].label,
                        "reasons": list(preds[c.id].reasons),
                    }
                    for c in reached
                    if c.split == s and preds[c.id].label != c.expected
                ]
                for s in splits
            },
        }
    return {name: results[name] for name in systems}


COMPARABILITY = (
    "The english and multilingual routes are two DIFFERENT models (421M vs 322M parameters) "
    "run with non-equivalent prompt token budgets (max_len 512/2048 vs 2048), and Laya's "
    "confidence numbers are entropy-like scores, not calibrated probabilities. The per-route "
    "numbers describe this small synthetic corpus only: they are not comparable accuracy "
    "estimates of the two models and not evidence of general understanding."
)


def build_report(
    cases: list[Case],
    classifier,
    *,
    now: float,
    probe_meta: dict | None = None,
    expired: Callable[[], bool] | None = None,
) -> dict:
    from datetime import UTC, datetime

    from .audit_classifier import PROMPT_VERSION

    model = classifier.profile()
    systems = evaluate(cases, classifier, now=NOW, expired=expired)
    pipeline = systems["pipeline"]["by_split"]
    planned = sum(body["total"] for body in systems.values())
    evaluated = sum(body["evaluated"] for body in systems.values())
    # A budget that ran out DURING the last call still means the run overshot: never a pass.
    partial = evaluated < planned or (expired is not None and expired())
    return {
        "model": model,
        "prompt_version": PROMPT_VERSION,
        "thresholds_digest": current_thresholds_digest(),
        "thresholds": DEFAULT_THRESHOLDS.to_mapping(),
        "bench_digest": bench_digest(),
        "date": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%d"),
        "splits": sorted({c.split for c in cases}),
        "counts": {s: sum(1 for c in cases if c.split == s) for s in SPLITS},
        "systems": systems,
        "routes": {r: sum(1 for c in cases if case_route(c) == r) for r in ROUTES},
        "model_probe": probe_meta or {},
        "comparability": COMPARABILITY,
        "partial": partial,
        "planned": planned,
        "evaluated": evaluated,
        "passed": passes(pipeline) and not partial,
        "pilot_raw_choice": PILOT_RAW_CHOICE,
        "previous_pipeline": PREVIOUS_PIPELINE,
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def render_markdown(report: dict) -> str:
    """The validation document, generated from an actual run (never hand-edited numbers)."""
    lines = [
        "# Classifier validation (generated)",
        "",
        f"Generated by `pm chatgpt audit bench` on {report['date']}.",
        "",
        f"- model: `{report['model']}`; prompt version: `{report['prompt_version']}`",
        f"- decision logic + thresholds digest: `{report['thresholds_digest'][:16]}`",
        f"- corpus digest: `{report['bench_digest'][:16]}`; cases: {report['counts']}",
        f"- thresholds: `{json.dumps(report['thresholds'], sort_keys=True)}`",
        (
            f"- verdict: **{_verdict_word(report)}** (pass rule: zero "
            "false-done on both splits, zero held-out false blocks, held-out exact accuracy >= "
            f"{PASS_MIN_HELDOUT_EXACT:.0%} and coverage >= {PASS_MIN_HELDOUT_COVERAGE:.0%})"
        ),
        (
            f"- budget: {report['evaluated']}/{report['planned']} predictions reached"
            + (
                " (PARTIAL: the budget ran out, metrics cover the reached cases only)"
                if report["partial"]
                else ""
            )
        ),
        (
            f"- documented counterexamples: raw pilot choice classifier "
            f"{report['pilot_raw_choice']}; previous pipeline {report['previous_pipeline']}"
        ),
        f"- real provider probe meta: `{json.dumps(report['model_probe'], sort_keys=True)}`",
        f"- routes (cases per route): {report['routes']}",
        f"- comparability: {report['comparability']}",
        "",
        "Laya is ADVISORY evidence only: it can never alone mark a task done, blocked, or send.",
        "",
    ]
    for name, body in report["systems"].items():
        lines += [
            f"## {name}",
            "",
            _SPLIT_HEADER,
            "|---|---|---|---|---|---|---|---|",
        ]
        for split, s in body["by_split"].items():
            lines.append(
                f"| {split} | {s['n']} | {_pct(s['exact'])} | {s['false_done']} | "
                f"{s['false_block']} | {s['false_incomplete']} | {_pct(s['abstention_rate'])} | "
                f"{_pct(s['coverage'])} |"
            )
        lines += [
            "",
            "| route | n | exact | false_done | false_block | abstain | coverage |",
            "|---|---|---|---|---|---|---|",
        ]
        for route, s in body["by_route"].items():
            lines.append(
                f"| {route} | {s['n']} | {_pct(s['exact'])} | {s['false_done']} | "
                f"{s['false_block']} | {_pct(s['abstention_rate'])} | {_pct(s['coverage'])} |"
            )
        lines.append("")
        for split, s in body["by_split"].items():
            lines += [
                f"Per-label precision/recall ({split}):",
                "",
                "| label | precision | recall | support | predicted |",
                "|---|---|---|---|---|",
            ]
            for label, m in s["per_label"].items():
                lines.append(
                    f"| {label} | {_pct(m['precision'])} | {_pct(m['recall'])} | "
                    f"{m['support']} | {m['predicted']} |"
                )
            lines.append("")
        mistakes = body["errors"]
        for split, items in mistakes.items():
            lines.append(
                f"Mismatches ({split}): "
                + (", ".join(f"`{i['id']}` {i['expected']}->{i['got']}" for i in items) or "none")
            )
        lines.append("")
    return "\n".join(lines) + "\n"


def _verdict_word(report: dict) -> str:
    if report["partial"]:
        return "NOT VALIDATED (PARTIAL RUN)"
    return "PASSED" if report["passed"] else "NOT VALIDATED"


def run(
    *,
    record: bool = False,
    as_json: bool = False,
    out=None,
    splits=SPLITS,
    classifier=None,
    directory: Path | None = None,
    clock=None,
    budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    monotonic=None,
) -> int:
    """Run the benchmark against the real warm Laya socket; exit 0 only when validated."""
    import sys
    import time

    from . import audit_store, audit_validation
    from .audit_classifier import PROMPT_VERSION, LayaClassifier

    monotonic = monotonic or time.monotonic
    out = out or sys.stdout
    now = (clock or time.time)()
    if not (math.isfinite(budget_seconds) and budget_seconds > 0):
        print("error: --budget must be a finite number of seconds above zero", file=sys.stderr)
        return 2
    classifier = classifier or LayaClassifier()
    deadline = monotonic() + budget_seconds
    probe = classifier.ask("probe", {"p": {"type": "noul", "instructions": "Is this a probe?"}})
    if probe.status != "ok":
        print(f"error: classifier {probe.status}: {probe.reason}", file=sys.stderr)
        return 2
    report = build_report(
        load_cases(splits),
        classifier,
        now=now,
        probe_meta=probe.meta,
        expired=lambda: monotonic() >= deadline,
    )
    out.write((json.dumps(report, sort_keys=True) if as_json else render_markdown(report)) + "\n")
    if record and report["passed"] and tuple(splits) == SPLITS:
        held = report["systems"]["pipeline"]["by_split"]["heldout"]
        tune = report["systems"]["pipeline"]["by_split"]["tune"]
        audit_validation.save_record(
            directory or audit_store.default_dir(),
            audit_validation.ValidationRecord(
                prompt_version=PROMPT_VERSION,
                model=report["model"],
                thresholds_digest=report["thresholds_digest"],
                bench_digest=report["bench_digest"],
                passed=report["passed"],
                recorded_at=now,
                summary={
                    "heldout_false_done": held["false_done"],
                    "heldout_cases": held["n"],
                    "heldout_exact": held["exact"],
                    "heldout_coverage": held["coverage"],
                    "tune_false_done": tune["false_done"],
                },
            ),
        )
    if report["partial"]:
        return PARTIAL_EXIT
    return 0 if report["passed"] else 1
