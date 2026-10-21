"""The approval ledger is the only request state: append-only, locked, fsynced, torn-line safe."""

from __future__ import annotations

import multiprocessing
import os
import stat

import pytest

from pmlib.chatgpt_control.ledger import Ledger, LedgerError


def event(kind="observation", at=1.0, **kw):
    return {"kind": kind, "at": at, **kw}


def test_roundtrip_and_private_modes(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.append(event(key="a"))
    ledger.append(event(key="b"))
    assert [e["key"] for e in ledger.read_all()] == ["a", "b"]
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.directory.stat().st_mode) == 0o700
    assert all(e["v"] == 1 for e in ledger.read_all())


def test_each_append_is_fsynced(tmp_path, monkeypatch):
    calls = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (calls.append(fd), real(fd))[1])
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.append(event())
    ledger.append(event())
    assert len(calls) >= 2


def test_torn_last_line_is_skipped_and_never_glued_to_next_event(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.append(event(key="a"))
    with open(ledger.path, "ab") as fh:
        fh.write(b'{"v":1,"kind":"observation","at":2.0,"key":"tor')  # crash mid-write
    assert [e["key"] for e in ledger.read_all()] == ["a"]
    ledger.append(event(key="c"))
    assert [e["key"] for e in ledger.read_all()] == ["a", "c"]


def test_garbage_and_foreign_lines_are_ignored(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.directory.mkdir(parents=True, mode=0o700)
    ledger.path.write_text('not json\n[1,2]\n{"kind":5}\n{"v":1,"kind":"observation","at":1}\n\n')
    assert len(ledger.read_all()) == 1


def test_unknown_kind_and_bad_time_rejected(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    with pytest.raises(ValueError):
        ledger.append(event(kind="approved_by_caller"))
    with pytest.raises(ValueError):
        ledger.append(event(at=float("nan")))
    with pytest.raises(ValueError):
        ledger.append({"kind": "observation"})
    assert ledger.read_all() == []


def test_symlinked_ledger_is_refused(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.directory.mkdir(mode=0o700)
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("")
    ledger.path.symlink_to(target)
    with pytest.raises(LedgerError):
        ledger.append(event())
    assert target.read_text() == ""


def test_wide_permissions_are_tightened(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    ledger.append(event())
    ledger.path.chmod(0o666)
    ledger.append(event())
    assert stat.S_IMODE(ledger.path.stat().st_mode) == 0o600


def _hammer(directory: str, worker: int) -> None:
    ledger = Ledger(__import__("pathlib").Path(directory))
    for i in range(25):
        ledger.append(event(worker=worker, i=i, pad="x" * 500))


def test_concurrent_processes_never_interleave(tmp_path):
    directory = tmp_path / "chatgpt"
    procs = [multiprocessing.Process(target=_hammer, args=(str(directory), n)) for n in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    events = Ledger(directory).read_all()
    assert len(events) == 100
    assert {(e["worker"], e["i"]) for e in events} == {(w, i) for w in range(4) for i in range(25)}


def test_transaction_serialises_read_decide_append(tmp_path):
    ledger = Ledger(tmp_path / "chatgpt")
    with ledger.transaction() as tx:
        assert tx.events() == []
        tx.append(event(key="x"))
        assert [e["key"] for e in tx.events()] == ["x"]
    assert len(ledger.read_all()) == 1
