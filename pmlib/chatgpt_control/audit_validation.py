"""Persisted classification-validation record and the ``auto_continue`` gate built on it.

Accessed via: ``audit_bench`` (``pm chatgpt audit bench --record`` writes the record),
``audit_collector``/``audit_report`` (every report and every conversation carries ``auto_continue``).

Invariants: auto-continue is FORBIDDEN by default. ``Gate.auto_continue`` answers
``{"allowed": false, "reason": "classification-not-validated"}`` unless a persisted record exists
that PASSED, was recorded for exactly the current prompt version, model, threshold digest and bench
corpus digest, whose own recorded held-out numbers still show zero false-done, AND the verdict being
judged still has the fingerprint it was computed for. Any mismatch, malformed or symlinked record,
or missing field is "no record". Even when allowed the gate only says a verdict may be used as a
proposal input: pm never sends, and the owner-approved continuation policy is unchanged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path

from . import audit_files
from .audit_decision import DEFAULT_THRESHOLDS, Thresholds, thresholds_digest

RECORD_FILE = "validation.json"
SCHEMA = 1
MAX_RECORD_BYTES = 16 * 1024
DENIED_REASON = "classification-not-validated"
_DIGEST_FIELDS = ("prompt_version", "model", "thresholds_digest", "bench_digest")


@dataclass(frozen=True)
class ValidationRecord:
    prompt_version: str
    model: str
    thresholds_digest: str
    bench_digest: str
    passed: bool
    recorded_at: float
    summary: dict

    def to_mapping(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_mapping(cls, data: object) -> ValidationRecord | None:
        names = {f.name for f in fields(cls)}
        if not isinstance(data, dict) or set(data) != names:
            return None
        if not all(isinstance(data[k], str) and data[k] for k in _DIGEST_FIELDS):
            return None
        stamp = data["recorded_at"]
        if type(data["passed"]) is not bool or type(stamp) not in (int, float):
            return None
        if not math.isfinite(stamp) or not isinstance(data["summary"], dict):
            return None
        return cls(**data)


def save_record(directory: Path, record: ValidationRecord) -> None:
    audit_files.write_json(
        Path(directory) / RECORD_FILE,
        {"v": SCHEMA, **record.to_mapping()},
        max_bytes=MAX_RECORD_BYTES,
    )


def load_record(directory: Path) -> ValidationRecord | None:
    data = audit_files.read_json(Path(directory) / RECORD_FILE, max_bytes=MAX_RECORD_BYTES)
    if not isinstance(data, dict) or data.get("v") != SCHEMA:
        return None
    return ValidationRecord.from_mapping({k: v for k, v in data.items() if k != "v"})


@dataclass(frozen=True)
class Gate:
    record: ValidationRecord | None
    prompt_version: str
    model: str
    thresholds_digest: str
    bench_digest: str

    def validated(self) -> bool:
        """A passing record exists for exactly the current ingredients."""
        r = self.record
        if r is None or not r.passed or not self.model:
            return False
        if any(getattr(r, name) != getattr(self, name) for name in _DIGEST_FIELDS):
            return False
        return r.summary.get("heldout_false_done") == 0

    def auto_continue(self, *, fingerprint_matches: bool) -> dict:
        if self.validated() and fingerprint_matches:
            return {"allowed": True, "reason": "validated"}
        return {"allowed": False, "reason": DENIED_REASON}

    def report_field(self) -> dict:
        """The run-level field: the record check alone (each verdict adds its own fingerprint)."""
        return self.auto_continue(fingerprint_matches=True)


def load_gate(
    directory: Path,
    *,
    model: str,
    thresholds: Thresholds = DEFAULT_THRESHOLDS,
    bench_digest: str | None = None,
) -> Gate:
    from .audit_classifier import PROMPT_VERSION

    if bench_digest is None:
        from .audit_bench import bench_digest as current_bench_digest

        bench_digest = current_bench_digest()
    return Gate(
        record=load_record(directory),
        prompt_version=PROMPT_VERSION,
        model=model,
        thresholds_digest=thresholds_digest(thresholds),
        bench_digest=bench_digest,
    )
