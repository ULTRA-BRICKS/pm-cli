"""The private audit cache: what may be remembered and when a remembered verdict is trusted."""

from __future__ import annotations

import json
import os
import stat

import pytest

from pmlib.chatgpt_control import audit_classifier as clf
from pmlib.chatgpt_control import audit_decision as dec
from pmlib.chatgpt_control import audit_files as files
from pmlib.chatgpt_control import audit_store as store
from tests.audit_support import ACCOUNT, CONVERSATION, NOW, evidence, laya, replace

MODEL = "convaiinnovations/laya@512"


def entry(e=None, c="default", **kw):
    e = e if e is not None else evidence()
    c = laya() if c == "default" else c
    v = dec.decide(e, c, now=NOW)
    return store.CachedVerdict.from_run(e, v, dec.DEFAULT_THRESHOLDS, **kw)


def trusted(s, e, **kw):
    args = {
        "now": NOW,
        "prompt_version": clf.PROMPT_VERSION,
        "model": MODEL,
        "thresholds_digest": dec.thresholds_digest(dec.DEFAULT_THRESHOLDS),
        "max_age": 3600.0,
        "fingerprint": e.fingerprint(),
    }
    args.update(kw)
    return s.trusted(e.account, e.conversation_id, **args)


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_put_then_get_round_trips_privately_and_carries_no_chat_text(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence(final_text="SECRET-FINAL-TEXT", request_text="SECRET-REQUEST-TEXT")
    cached = entry(e)
    s.put(cached)
    assert s.get(ACCOUNT, CONVERSATION) == cached
    raw = (tmp_path / "audit" / store.VERDICTS_FILE).read_text()
    assert "SECRET" not in raw
    assert mode(tmp_path / "audit") == 0o700
    assert mode(tmp_path / "audit" / store.VERDICTS_FILE) == 0o600
    assert cached.verdict["task_state"] == "done"
    assert cached.fingerprint == e.fingerprint() and cached.evidence_digest == e.digest()


def test_entries_are_keyed_by_account_and_conversation(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    s.put(entry())
    other = evidence(account="glide.vc", observed_account="glide.vc")
    s.put(entry(other, laya(task_finished=0.1, steps_remaining=0.9)))
    assert s.get("home", CONVERSATION).verdict["task_state"] == "done"
    assert s.get("glide.vc", CONVERSATION).verdict["task_state"] == "incomplete"
    assert s.get("home", "no-such-conversation") is None


def test_a_matching_fresh_entry_is_trusted(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence()
    s.put(entry(e))
    assert trusted(s, e).verdict["task_state"] == "done"


@pytest.mark.parametrize(
    "override",
    [
        {"now": NOW + 7200},  # too old
        {"now": NOW - 7200},  # verified in the future
        {"prompt_version": "completion-v0"},
        {"model": "laya-9.9.9"},
        {"thresholds_digest": dec.thresholds_digest(dec.Thresholds(done_min=0.95))},
        {"fingerprint": "0" * 64},
    ],
)
def test_a_cached_verdict_is_not_trusted_when_anything_it_judged_has_changed(tmp_path, override):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence()
    s.put(entry(e))
    assert trusted(s, e, **override) is None


def test_the_latest_message_changing_invalidates_the_cached_verdict(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence()
    s.put(entry(e))
    changed = replace(e, final_text="A newer final answer that was never audited.")
    assert trusted(s, changed) is None


def test_without_a_fingerprint_only_freshness_and_versions_are_checked(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence()
    s.put(entry(e))
    assert trusted(s, e, fingerprint=None) is not None
    assert trusted(s, e, fingerprint=None, now=NOW + 7200) is None
    assert trusted(s, e, fingerprint=None, model="laya-9.9.9") is None


def test_a_verdict_computed_without_the_classifier_is_trusted_for_any_expected_model(tmp_path):
    """A deterministic verdict never depended on the model, so a model change cannot void it."""
    s = store.AuditStore(tmp_path / "audit")
    e = evidence(progress_indicator=True)
    cached = entry(e, c=None)
    assert cached.verdict["task_state"] == "working" and cached.model == ""
    s.put(cached)
    assert trusted(s, e, model="laya-9.9.9") is not None
    assert trusted(s, e, model="") is not None


def test_a_classifier_outage_verdict_is_cached_as_unknown_never_as_done(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    e = evidence()
    cached = entry(e, c=clf.unavailable("no socket"))
    s.put(cached)
    assert trusted(s, e).verdict["task_state"] == "unknown"


def test_corrupt_or_foreign_cache_files_are_an_empty_cache(tmp_path):
    directory = tmp_path / "audit"
    s = store.AuditStore(directory)
    s.put(entry())
    path = directory / store.VERDICTS_FILE
    path.write_text("{not json")
    assert s.get(ACCOUNT, CONVERSATION) is None
    path.write_text('{"v": 99, "entries": {}}')
    assert s.get(ACCOUNT, CONVERSATION) is None
    path.write_text('{"v": 1, "entries": {"home/' + CONVERSATION + '": {"fingerprint": 5}}}')
    assert s.get(ACCOUNT, CONVERSATION) is None


def test_a_symlinked_cache_is_never_followed_or_written(tmp_path):
    directory = tmp_path / "audit"
    directory.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (directory / store.VERDICTS_FILE).symlink_to(target)
    s = store.AuditStore(directory)
    assert s.get(ACCOUNT, CONVERSATION) is None
    with pytest.raises(files.UnsafePath):
        s.put(entry())
    assert target.read_text() == "{}"


def test_the_cache_is_bounded_and_evicts_the_least_recently_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_ENTRIES", 3)
    s = store.AuditStore(tmp_path / "audit")
    for i in range(5):
        e = evidence(conversation_id=f"conv-{i}", url=f"https://chatgpt.com/c/conv-{i}")
        s.put(entry(e, verified_at=NOW + i))
    kept = sorted(c for c in (f"conv-{i}" for i in range(5)) if s.get(ACCOUNT, c))
    assert kept == ["conv-2", "conv-3", "conv-4"]


def test_an_entry_records_every_native_identifier_of_the_latest_turn(tmp_path):
    e = evidence()
    cached = entry(e)
    assert set(cached.turn_ids) == {"u1", "turn-1", "a1"} and len(cached.turn_ids) == 3
    s = store.AuditStore(tmp_path / "audit")
    s.put(cached)
    assert s.get(ACCOUNT, CONVERSATION).turn_ids == cached.turn_ids


@pytest.mark.parametrize("bad", ["turn-1", [1], [""], None, {"a": "b"}])
def test_malformed_turn_ids_make_the_entry_unreadable(bad):
    wire = entry().to_mapping()
    wire["turn_ids"] = bad
    assert store.CachedVerdict.from_mapping(wire) is None


def test_an_old_schema_cache_is_ignored(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    s.put(entry())
    path = tmp_path / "audit" / store.VERDICTS_FILE
    old = json.loads(path.read_text())
    old["v"] = 1
    path.write_text(json.dumps(old))
    assert s.get(ACCOUNT, CONVERSATION) is None


def test_the_last_run_report_round_trips_and_is_bounded(tmp_path):
    s = store.AuditStore(tmp_path / "audit")
    assert s.last_report() is None
    s.put_report({"ok": True, "n": 1})
    assert s.last_report() == {"ok": True, "n": 1}
    with pytest.raises(ValueError):
        s.put_report({"blob": "x" * (store.MAX_REPORT_BYTES + 1)})
    assert s.last_report() == {"ok": True, "n": 1}


def test_the_notification_log_only_accepts_delivery_outcomes(tmp_path):
    log = store.AuditStore(tmp_path / "audit").notification_log()
    log.append({"kind": "sent", "at": 1.0, "key": "k1"})
    log.append({"kind": "delivery_uncertain", "at": 2.0, "key": "k2"})
    with pytest.raises(ValueError):
        log.append({"kind": "approved", "at": 3.0, "key": "k3"})
    assert [e["key"] for e in log.read_all()] == ["k1", "k2"]
