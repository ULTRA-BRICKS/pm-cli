"""Private state files for the audit: 0600/0700, no symlinks, bounded, append-only logs."""

from __future__ import annotations

import json
import os
import stat
import threading

import pytest

from pmlib.chatgpt_control import audit_files as files


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_write_json_is_atomic_private_and_round_trips(tmp_path):
    path = tmp_path / "state" / "x.json"
    files.write_json(path, {"b": 2, "a": [1, 2]})
    assert mode(path) == 0o600 and mode(path.parent) == 0o700
    assert files.read_json(path) == {"a": [1, 2], "b": 2}
    assert not list(path.parent.glob("*.tmp"))


def test_read_json_missing_corrupt_oversized_or_nan_is_the_default(tmp_path):
    path = tmp_path / "x.json"
    assert files.read_json(path) is None
    assert files.read_json(path, default={}) == {}
    path.write_text("{broken")
    assert files.read_json(path, default="d") == "d"
    path.write_text('{"v": NaN}')
    assert files.read_json(path, default="d") == "d"
    path.write_text(json.dumps({"big": "x" * 5000}))
    assert files.read_json(path, max_bytes=100, default="d") == "d"


def test_write_json_refuses_nan_and_oversize(tmp_path):
    with pytest.raises(ValueError):
        files.write_json(tmp_path / "a.json", {"v": float("nan")})
    with pytest.raises(ValueError):
        files.write_json(tmp_path / "b.json", {"big": "x" * 5000}, max_bytes=100)
    assert not (tmp_path / "a.json").exists() and not (tmp_path / "b.json").exists()


def test_symlinked_state_file_is_never_followed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"v": 1}')
    link = tmp_path / "link.json"
    link.symlink_to(target)
    assert files.read_json(link, default="d") == "d"
    with pytest.raises(files.UnsafePath):
        files.write_json(link, {"v": 2})
    assert json.loads(target.read_text()) == {"v": 1}


def test_append_log_appends_lines_privately_and_reads_them_back(tmp_path):
    log = files.AppendLog(tmp_path / "audit" / "n.jsonl", kinds={"sent", "uncertain"})
    log.append({"kind": "sent", "at": 1.0, "key": "a"})
    log.append({"kind": "uncertain", "at": 2.0, "key": "b"})
    assert [e["key"] for e in log.read_all()] == ["a", "b"]
    assert mode(log.path) == 0o600 and mode(log.path.parent) == 0o700
    assert all(e["v"] == files.LOG_SCHEMA for e in log.read_all())


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "other", "at": 1.0},
        {"kind": "sent"},
        {"kind": "sent", "at": float("nan")},
        {"kind": "sent", "at": True},
        {"at": 1.0},
    ],
)
def test_append_log_rejects_bad_events(tmp_path, event):
    log = files.AppendLog(tmp_path / "n.jsonl", kinds={"sent"})
    with pytest.raises(ValueError):
        log.append(event)
    assert log.read_all() == []


def test_append_log_oversized_line_is_rejected(tmp_path):
    log = files.AppendLog(tmp_path / "n.jsonl", kinds={"sent"}, max_line=200)
    with pytest.raises(ValueError):
        log.append({"kind": "sent", "at": 1.0, "blob": "x" * 500})


def test_torn_last_line_is_skipped_and_the_next_append_starts_fresh(tmp_path):
    log = files.AppendLog(tmp_path / "n.jsonl", kinds={"sent"})
    log.append({"kind": "sent", "at": 1.0, "key": "a"})
    with open(log.path, "ab") as handle:
        handle.write(b'{"kind": "sent", "at": 2.0, "key"')  # a crash mid-write
    assert [e["key"] for e in log.read_all()] == ["a"]
    log.append({"kind": "sent", "at": 3.0, "key": "c"})
    assert [e["key"] for e in log.read_all()] == ["a", "c"]


def test_log_rotates_so_it_stays_bounded(tmp_path):
    log = files.AppendLog(tmp_path / "n.jsonl", kinds={"sent"}, max_bytes=600, max_line=300)
    for i in range(60):
        log.append({"kind": "sent", "at": float(i), "key": f"k{i:03d}"})
    assert log.path.stat().st_size <= 600 + 300
    assert mode(log.path) == 0o600
    keys = [e["key"] for e in log.read_all()]
    assert keys[-1] == "k059" and len(keys) < 60


def test_symlinked_log_is_refused(tmp_path):
    target = tmp_path / "t.jsonl"
    target.write_text("")
    link = tmp_path / "n.jsonl"
    link.symlink_to(target)
    log = files.AppendLog(link, kinds={"sent"})
    with pytest.raises(files.UnsafePath):
        log.append({"kind": "sent", "at": 1.0})


def test_concurrent_appends_never_interleave(tmp_path):
    log = files.AppendLog(tmp_path / "n.jsonl", kinds={"sent"}, max_bytes=10**7)

    def work(n):
        for i in range(25):
            log.append({"kind": "sent", "at": float(i), "key": f"{n}-{i}"})

    threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    keys = [e["key"] for e in log.read_all()]
    assert len(keys) == 100 and len(set(keys)) == 100
