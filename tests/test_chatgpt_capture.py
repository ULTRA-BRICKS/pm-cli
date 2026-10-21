"""Bounded Guardian snapshot capture: output limits, timeout and reaping, with fake executables.

Every fake is a local, harmless Python script placed first on PATH; nothing here contacts a
browser, the real Guardian or any service.
"""

import json
import os
import subprocess
import sys
import time
import tracemalloc

import pytest

from tests.test_chatgpt_tui import project

LIMIT = 1048576


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Install ``chatgpt-guardian`` running ``body``; returns (pid_file, progress_file, argv_file)."""
    pid, progress, argv = tmp_path / "pid", tmp_path / "progress", tmp_path / "argv.json"

    def install(body):
        executable = tmp_path / "chatgpt-guardian"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            f"open({str(pid)!r}, 'w').write(str(os.getpid()))\n"
            f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            "def flood(fd, cap=128 * 2**20):\n"
            "    block, sent = b'x' * 65536, 0\n"
            "    while sent < cap:\n"
            "        sent += os.write(fd, block)\n"
            f"        open({str(progress)!r}, 'w').write(str(sent))\n"
            f"{body}\n"
        )
        executable.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "5")
        return pid, progress, argv

    return install


def gone(pid_file):
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def snapshot_json(**changes):
    return json.dumps(dict(project(), **changes))


def fetch(workspace="glide.vc", state=None):
    from pmlib.chatgpt_monitor import fetch_snapshot

    return fetch_snapshot(workspace, state)


def test_normal_snapshot_with_noisy_stderr_and_exact_argv(fake):
    _, _, argv = fake(f"sys.stderr.write('w' * 200000)\nsys.stdout.write({snapshot_json()!r})\n")
    assert fetch()["workspace"] == "glide.vc"
    assert json.loads(argv.read_text()) == ["--workspace", "glide.vc", "monitor", "--json"]


def test_state_option_is_forwarded_literally(fake):
    _, _, argv = fake(f"sys.stdout.write({snapshot_json()!r})\n")
    fetch("glide.vc", "x.sqlite")
    assert json.loads(argv.read_text()) == [
        "--workspace",
        "glide.vc",
        "--state=x.sqlite",
        "monitor",
        "--json",
    ]


def test_snapshot_just_under_the_limit_is_accepted(fake):
    pad = "p" * (LIMIT - len(snapshot_json(pad="")) - 64)
    fake(f"sys.stdout.write({snapshot_json(pad=pad)!r})\n")
    assert fetch()["pad"] == pad


def test_runs_without_shell_and_with_argument_list(fake, monkeypatch):
    fake(f"sys.stdout.write({snapshot_json()!r})\n")
    seen = []
    real = subprocess.Popen

    def spy(command, *args, **kwargs):
        seen.append((command, kwargs))
        return real(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", spy)
    fetch()
    command, kwargs = seen[0]
    assert isinstance(command, list) and not kwargs.get("shell")


@pytest.mark.parametrize("fd,stream", [(1, "stdout"), (2, "stderr")])
def test_oversized_stream_is_bounded_and_reader_stopped(fake, fd, stream):
    from pmlib.chatgpt_monitor import MonitorError

    pid, progress, _ = fake(f"flood({fd})\n")
    tracemalloc.start()
    try:
        with pytest.raises(MonitorError, match=f"{stream} exceeds"):
            fetch()
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert gone(pid)
    assert int(progress.read_text()) < 4 * LIMIT
    assert peak < 5 * LIMIT


def test_oversized_stderr_with_failing_exit_is_bounded(fake):
    from pmlib.chatgpt_monitor import MonitorError

    pid, _, _ = fake(f"sys.stderr.write('e' * {LIMIT + 4096})\nsys.exit(1)\n")
    with pytest.raises(MonitorError, match="stderr exceeds"):
        fetch()
    assert gone(pid)


def test_timeout_kills_only_reader_and_reports_unconfirmed(fake, monkeypatch):
    from pmlib.chatgpt_monitor import MonitorError

    pid, _, _ = fake("time.sleep(60)\n")
    monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "0.3")
    started = time.monotonic()
    with pytest.raises(MonitorError, match="timed out"):
        fetch()
    assert time.monotonic() - started < 4
    assert gone(pid)


def test_timeout_with_slow_trickle_output_still_stops(fake, monkeypatch):
    from pmlib.chatgpt_monitor import MonitorError

    pid, _, _ = fake(
        "while True:\n    sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.05)\n"
    )
    monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "0.4")
    started = time.monotonic()
    with pytest.raises(MonitorError, match="timed out"):
        fetch()
    assert time.monotonic() - started < 4
    assert gone(pid)


def test_timeout_when_reader_closes_pipes_but_keeps_running(fake, monkeypatch):
    from pmlib.chatgpt_monitor import MonitorError

    pid, _, _ = fake("os.close(1); os.close(2); time.sleep(60)\n")
    monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "0.3")
    started = time.monotonic()
    with pytest.raises(MonitorError, match="timed out"):
        fetch()
    assert time.monotonic() - started < 4
    assert gone(pid)


@pytest.mark.parametrize("parent_exits", [True, False])
def test_descendant_holding_the_pipes_is_killed_with_its_reader(
    fake, monkeypatch, tmp_path, parent_exits
):
    from pmlib.chatgpt_monitor import MonitorError

    marker = tmp_path / "grandchild_pid"
    fake(
        "import subprocess\n"
        "subprocess.Popen([sys.executable, '-c', "
        f"\"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)\"])\n"
        f"while not os.path.exists({str(marker)!r}) or not open({str(marker)!r}).read():\n"
        "    time.sleep(0.01)\n" + ("" if parent_exits else "time.sleep(60)\n")
    )
    monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "0.5")
    started = time.monotonic()
    with pytest.raises(MonitorError, match="timed out"):
        fetch()
    assert time.monotonic() - started < 5
    assert gone(marker)


def test_complete_snapshot_is_not_discarded_when_exit_lags_the_deadline(fake, monkeypatch):
    fake(
        f"sys.stdout.write({snapshot_json()!r}); sys.stdout.flush()\n"
        "os.close(1); os.close(2); time.sleep(0.45)\n"
    )
    monkeypatch.setenv("PMLIB_MONITOR_TIMEOUT", "0.3")
    assert fetch()["workspace"] == "glide.vc"


def test_nonzero_exit_reports_bounded_stderr(fake):
    from pmlib.chatgpt_monitor import MonitorError

    fake("sys.stderr.write('boom ' + 'z' * 2000)\nsys.exit(3)\n")
    with pytest.raises(MonitorError) as caught:
        fetch()
    message = str(caught.value)
    assert message.startswith("Guardian snapshot unavailable: boom") and len(message) < 600


def test_old_guardian_without_monitor_command_is_reported(fake):
    from pmlib.chatgpt_monitor import MonitorError

    fake("sys.stderr.write(\"argument: invalid choice: 'monitor'\")\nsys.exit(2)\n")
    with pytest.raises(MonitorError, match="Update chatgpt-guardian"):
        fetch()


def test_missing_executable_keeps_exit_code(monkeypatch, tmp_path):
    from pmlib.chatgpt_monitor import MonitorError

    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(MonitorError, match="unavailable") as caught:
        fetch()
    assert caught.value.code == 127


def test_non_executable_file_is_reported_not_raised(fake, tmp_path):
    from pmlib.chatgpt_monitor import MonitorError

    fake("pass\n")
    (tmp_path / "chatgpt-guardian").chmod(0o644)
    with pytest.raises(MonitorError, match="PermissionError|unavailable"):
        fetch()


@pytest.mark.parametrize(
    "body", ["sys.stdout.buffer.write(b'\\xff\\xfe{')\n", "sys.stdout.write('{')\n"]
)
def test_undecodable_or_truncated_stdout_is_a_monitor_error(fake, body):
    from pmlib.chatgpt_monitor import MonitorError

    fake(body)
    with pytest.raises(MonitorError, match="Invalid Guardian snapshot"):
        fetch()


def test_account_validation_still_applies_to_captured_stdout(fake):
    from pmlib.chatgpt_monitor import MonitorError

    fake(f"sys.stdout.write({snapshot_json(workspace='home')!r})\n")
    with pytest.raises(MonitorError, match="another workspace"):
        fetch("glide.vc")


def test_malformed_event_chat_in_captured_stdout_is_rejected(fake):
    from pmlib.chatgpt_monitor import MonitorError

    events = [{"kind": "k", "chat": None, "at": 1.0}]
    fake(f"sys.stdout.write({snapshot_json(events=events)!r})\n")
    with pytest.raises(MonitorError, match="event"):
        fetch()


def test_deeply_nested_json_is_a_monitor_error_not_a_crash(fake):
    from pmlib.chatgpt_monitor import MonitorError

    fake("sys.stdout.write('[' * 200000)\n")
    with pytest.raises(MonitorError, match="Invalid Guardian snapshot"):
        fetch()


@pytest.mark.parametrize("field", ["event", "generated_at"])
def test_oversized_integer_timestamp_is_a_monitor_error_not_a_crash(fake, field):
    from pmlib.chatgpt_monitor import MonitorError

    changes = (
        {"events": [{"kind": "k", "at": 10**400}]}
        if field == "event"
        else {"generated_at": 10**400}
    )
    fake(f"sys.stdout.write({snapshot_json(**changes)!r})\n")
    with pytest.raises(MonitorError, match="Invalid Guardian snapshot"):
        fetch()


def test_non_posix_host_is_refused_cleanly(fake, monkeypatch):
    from pmlib.chatgpt_monitor import MonitorError

    monkeypatch.delattr(os, "killpg")
    with pytest.raises(MonitorError, match="requires a POSIX host"):
        fetch()
