"""Keyboard, safe rendering and real-terminal tests for the read-only monitor."""

import os
import subprocess
import sys
import time

import pytest

NOW = 1792254000.0
CHAT = "https://chatgpt.com/c/one"
OTHER = "https://chatgpt.com/c/two"


def sample(chat=CHAT, **kw):
    return {
        "chat": chat,
        "title": "A task",
        "watched": chat == CHAT,
        "present": True,
        "status": "working",
        "reason": "" if chat == CHAT else "not-enrolled",
        "continuation": "running",
        "last_go_state": "sent",
        "last_go_at": NOW - 100,
        "last_checked_at": NOW,
        "next_check_at": NOW + 60,
        **kw,
    }


def project(samples=None):
    rows = samples or [sample()]
    return {
        "schema_version": 1,
        "workspace": "glide.vc",
        "generated_at": NOW,
        "heartbeat_at": NOW - 5,
        "connection": "connected",
        "stale": False,
        "max_pages": 6,
        "page_count": len(rows),
        "summary": {
            "enrolled": 1,
            "present": 1,
            "available": len(rows),
            "running": len(rows),
            "blocked": 0,
            "sent_24h": 1,
            "resumed_24h": 1,
            "completed_24h": 0,
        },
        "sessions": rows,
        "service": {"active": "active"},
        "events": [{"kind": "continuation-running", "chat": CHAT, "at": NOW}],
    }


def test_coverage_and_confirmation_separate():
    from pmlib.chatgpt_tui import ViewState, render_lines

    lines = "\n".join(render_lines(project([sample(), sample(OTHER)]), ViewState(), 160, 40, NOW))
    assert "watched 1" in lines and "available 2" in lines
    assert "running" in lines and "sent" in lines
    assert "Europe/Belgrade" in lines and "not-enrolled" in lines


def test_search_navigation_details_and_quit():
    from pmlib.chatgpt_tui import ViewState, render_lines

    snap = project([sample(title="Alpha"), sample(OTHER, title="Beta")])
    state = ViewState()
    state.key("/", 2)
    for key in "Beta":
        state.key(key, 2)
    state.key("\n", 2)
    lines = "\n".join(render_lines(snap, state, 120, 30, NOW))
    assert "Beta" in lines and "Alpha" not in lines
    state.key("\n", 1)
    assert state.details and OTHER in "\n".join(render_lines(snap, state, 120, 30, NOW))
    state.key("\x1b", 1)
    assert not state.details and state.key("q", 1) == "quit"


def test_events_filters_and_display_only_pause():
    from pmlib.chatgpt_tui import ViewState, render_lines, visible_rows

    snap = project([sample(), sample(OTHER)])
    state = ViewState()
    state.key("f", 2)
    assert len(visible_rows(snap, state)) == 1
    state.key("\t", 1)
    assert state.pane == "events"
    assert "continuation-running" in "\n".join(render_lines(snap, state, 160, 40, NOW))
    state.key(" ", 1)
    assert state.paused
    assert state.key("r", 1) == "refresh"


def test_terminal_injection_removed_everywhere():
    from pmlib.chatgpt_tui import ViewState, render_lines

    snap = project([sample(title="\x1b[31mRed\x1b[0m\u202evil\x07")])
    snap["sessions"][0]["reason"] = "\x1b]2;pwnd\x07bad\u2069"
    lines = "\n".join(render_lines(snap, ViewState(error="\x1b[31merror\x00"), 160, 40, NOW))
    assert "\x1b" not in lines and "\x07" not in lines and "\u202e" not in lines
    assert "Red" in lines


def test_failed_refresh_preserves_source_age():
    from pmlib.chatgpt_tui import ViewState, render_lines

    snap = project()
    lines = "\n".join(render_lines(snap, ViewState(error="source unavailable"), 160, 40, NOW + 400))
    assert "UNAVAILABLE" in lines and "STALE" in lines and "400s" in lines
    assert snap["generated_at"] == NOW


@pytest.mark.parametrize("width,height", [(20, 5), (40, 12), (80, 24), (160, 50)])
def test_resize_bounds(width, height):
    from pmlib.chatgpt_tui import ViewState, render_lines, text_width

    lines = render_lines(project(), ViewState(), width, height, NOW)
    assert len(lines) <= height and all(text_width(line) <= width for line in lines)


def test_snapshot_validation_rejects_account_and_types():
    from pmlib.chatgpt_monitor import validate_snapshot

    snap = project()
    assert validate_snapshot(snap, "glide.vc")["schema_version"] == 1
    with pytest.raises(ValueError):
        validate_snapshot(snap, "home")
    for invalid in (
        [],
        None,
        {"schema_version": 1},
        dict(snap, sessions=[None]),
        dict(snap, summary="bad"),
    ):
        with pytest.raises(ValueError):
            validate_snapshot(invalid, "glide.vc")


def test_cli_tui_help_and_json_exclusion():
    result = subprocess.run(
        [sys.executable, "-m", "pmlib.cli", "chatgpt", "tui", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and "--interval" in result.stdout
    result = subprocess.run(
        [sys.executable, "-m", "pmlib.cli", "chatgpt", "monitor", "--tui", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY is unavailable on Windows")
def test_real_terminal_search_resize_and_exit(tmp_path):
    import fcntl
    import select
    import signal
    import struct
    import termios

    root = os.path.dirname(os.path.dirname(__file__))
    script = tmp_path / "pty_run.py"
    script.write_text("""from pmlib.chatgpt_tui import run_tui
snapshot={"workspace":"glide.vc","generated_at":__import__("time").time(),"heartbeat_at":None,"stale":False,"connection":"connected","summary":{"enrolled":1,"present":1,"available":2,"running":0,"sent_24h":1,"resumed_24h":1,"completed_24h":1},"sessions":[{"chat":"https://chatgpt.com/c/abc","title":"Terminal proof","status":"complete","continuation":"completed","watched":True}],"events":[]}
raise SystemExit(run_tui(lambda:snapshot,interval=2))
""")
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 150, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=root)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()

    def drain(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break

    try:
        deadline = time.monotonic() + 5
        while (
            b"Terminal proof" not in output
            and time.monotonic() < deadline
            and process.poll() is None
        ):
            drain(0.1)
        assert b"Terminal proof" in output
        os.write(master, b"/Terminal\n\n")
        drain(0.4)
        assert b"continuation" in output and b"https://chatgpt.com/c/abc" in output
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 45, 0, 0))
        process.send_signal(signal.SIGWINCH)
        drain(0.2)
        os.write(master, b"\x1b")
        drain(0.2)
        os.write(master, b"\tr ")
        drain(0.2)
        os.write(master, b"q")
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
    assert b"Traceback" not in output


@pytest.mark.parametrize(
    "changes", [{"service": None}, {"stale": "false"}, {"sessions": [{"chat": CHAT, "title": []}]}]
)
def test_nested_invalid_snapshot_rejected_before_curses(changes):
    from pmlib.chatgpt_monitor import validate_snapshot

    with pytest.raises(ValueError):
        validate_snapshot(dict(project(), **changes), "glide.vc")


# --- review-round regressions: revalidation, source-time freshness, messages, quit ---------


def account_snap(workspace, rows=None, **kw):
    data = project(rows)
    data["workspace"] = workspace
    data.update(kw)
    return data


def two_accounts(home=None, glide=None, order=("glide.vc", "home")):
    from pmlib.chatgpt_monitor import combine

    snaps = {"home": home or account_snap("home"), "glide.vc": glide or account_snap("glide.vc")}
    return combine([(ws, snaps[ws], None) for ws in order])


def open_confirm(data, now, kind_key="w"):
    from pmlib.chatgpt_tui import ViewState

    state = ViewState()
    state.key("a", 1, data, now=now)
    state.key(kind_key, 1, data, now=now)
    assert state.confirm is not None
    return state


@pytest.mark.parametrize(
    "later,later_now,paused,error",
    [
        ({"stale": True}, NOW + 10, False, ""),
        ({"stale": False}, NOW + 3600, False, ""),
        ({"stale": False}, NOW + 3600, True, ""),
        ({"stale": False}, NOW + 3600, False, "refresh failed"),
        ({"stale": False, "generated_at": None}, NOW + 10, False, ""),
    ],
)
def test_confirm_revalidated_when_snapshot_goes_stale(later, later_now, paused, error):
    state = open_confirm(project(), NOW + 10)
    state.paused, state.error = paused, error
    result = state.key("y", 1, dict(project(), **later), now=later_now)
    assert result is None
    assert state.confirm is None and state.notice.startswith("Refused:")


def test_fresh_only_actions_refused_while_refresh_error_set():
    from pmlib.chatgpt_tui import ViewState

    d = dict(project(), workspace="home", generated_at=NOW, stale=False)
    s = ViewState()
    s.key("a", 1, d, NOW)
    s.key("w", 1, d, NOW)
    assert s.confirm is not None
    s.error = "read failed"
    assert s.key("y", 1, d, NOW + 1) is None
    assert s.notice.startswith("Refused:") and s.confirm is None

    s.key("a", 1, d, NOW + 2)
    s.key("w", 1, d, NOW + 2)
    assert s.confirm is None and s.notice.startswith("Refused:")

    for key in ("p", "X"):
        s.key("a", 1, d, NOW + 3)
        s.key(key, 1, d, NOW + 3)
        assert s.confirm is not None, key
        s.confirm = None

    s.error = ""
    s.key("a", 1, d, NOW + 4)
    s.key("w", 1, d, NOW + 4)
    assert s.confirm is not None
    result = s.key("y", 1, d, NOW + 5)
    assert result is not None and result[0] == "action"


def test_confirm_revalidated_when_account_unavailable_or_chat_gone():
    from pmlib.chatgpt_monitor import combine

    data = two_accounts()
    state = open_confirm(data, NOW + 10)
    down = combine([("glide.vc", None, "telemetry down"), ("home", account_snap("home"), None)])
    assert state.key("y", 1, down, now=NOW + 10) is None
    assert state.notice.startswith("Refused:") and state.confirm is None
    state = open_confirm(data, NOW + 10)
    gone = two_accounts(glide=account_snap("glide.vc", [sample(OTHER)]))
    assert state.key("y", 1, gone, now=NOW + 10) is None
    assert state.notice.startswith("Refused:")
    state = open_confirm(data, NOW + 10)
    dup = two_accounts(glide=account_snap("glide.vc", [sample(), sample()]))
    assert state.key("y", 1, dup, now=NOW + 10) is None
    assert "ambiguous" in state.notice


def test_confirmed_target_is_frozen_across_row_reorder():
    from pmlib.chatgpt_tui import Action

    data = two_accounts(order=("home", "glide.vc"))
    state = open_confirm(data, NOW + 10)
    assert state.confirm == Action("home", "watch", CHAT)
    reordered = two_accounts(order=("glide.vc", "home"))
    state.selected = 1  # now points at the other account's row
    assert state.key("y", 2, reordered, now=NOW + 10) == ("action", Action("home", "watch", CHAT))


def test_fresh_evidence_uses_source_time_not_flag():
    from pmlib.chatgpt_tui import plan_action

    old = project()  # generated_at NOW, stale flag False
    row = old["sessions"][0]
    for kind in ("watch", "unwatch", "resume"):
        assert plan_action(old, row, kind, now=NOW + 179)[1] is None
        assert "stale" in plan_action(old, row, kind, now=NOW + 3600)[1]
    assert plan_action(old, {"account": "glide.vc"}, "service-start", now=NOW + 179)[1] is None
    assert plan_action(old, {"account": "glide.vc"}, "service-start", now=NOW + 3600)[1]
    for bad in (None, float("nan"), float("inf")):
        data = dict(old, generated_at=bad)
        assert plan_action(data, row, "watch", now=NOW)[1]
        assert plan_action(data, {"account": "glide.vc"}, "service-start", now=NOW)[1]
    # precisely targeted pause and service-stop remain available on old data
    assert plan_action(old, row, "pause", now=NOW + 3600)[1] is None
    assert plan_action(old, {"account": "glide.vc"}, "service-stop", now=NOW + 3600)[1] is None
    assert plan_action(dict(old, stale=True), row, "pause", now=NOW)[1] is None


def test_service_start_refused_for_unavailable_account():
    from pmlib.chatgpt_monitor import combine
    from pmlib.chatgpt_tui import plan_action

    data = combine([("home", account_snap("home"), None), ("glide.vc", None, "telemetry down")])
    assert plan_action(data, {"account": "glide.vc"}, "service-start", now=NOW)[1]
    assert plan_action(data, {"account": "home"}, "service-start", now=NOW)[1] is None
    assert plan_action(data, {"account": "glide.vc"}, "service-stop", now=NOW)[1] is None


def test_stale_account_does_not_block_or_leak_to_fresh_account():
    from pmlib.chatgpt_tui import plan_action

    data = two_accounts(home=account_snap("home", generated_at=NOW - 3600))
    now = NOW + 10
    home = next(r for r in data["sessions"] if r["account"] == "home")
    glide = next(r for r in data["sessions"] if r["account"] == "glide.vc")
    for kind in ("watch", "unwatch", "resume"):
        assert plan_action(data, home, kind, now=now)[1]
        action, refusal = plan_action(data, glide, kind, now=now)
        assert refusal is None and action.account == "glide.vc"
    assert plan_action(data, {"account": "home"}, "service-start", now=now)[1]
    assert plan_action(data, {"account": "glide.vc"}, "service-start", now=now)[1] is None
    assert plan_action(data, home, "pause", now=now)[1] is None


def test_service_stop_message_separates_recovery_from_observer():
    from pmlib.chatgpt_tui import Action, run_action

    result = run_action(
        Action("home", "service-stop"),
        runner=lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""),
    )
    message = result.message.lower()
    assert result.outcome == "success"
    assert "recovery" in message and "observ" in message and "separate" in message
    assert "not stopped" in message


def test_quit_refused_while_action_pending_and_never_retried():
    from pmlib.chatgpt_tui import Action, ViewState, run_action

    state = ViewState(pending=True)
    assert state.key("q", 1, project(), now=NOW) is None
    assert "still running" in state.notice
    state.pending = False
    assert state.key("q", 1, project(), now=NOW) == "quit"
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = run_action(Action("glide.vc", "watch", CHAT), runner=runner)
    assert result.outcome == "uncertain" and len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY is unavailable on Windows")
def test_real_terminal_quit_waits_for_running_action(tmp_path):
    import fcntl
    import select
    import struct
    import termios

    root = os.path.dirname(os.path.dirname(__file__))
    marker = tmp_path / "done.marker"
    calls = tmp_path / "calls.log"
    script = tmp_path / "pty_pending.py"
    script.write_text(f"""import time
from pmlib.chatgpt_tui import ActionResult, run_tui
def fetch():
    return {{"workspace":"glide.vc","generated_at":__import__("time").time(),"heartbeat_at":None,"stale":False,"connection":"connected","summary":{{"enrolled":1,"present":1,"available":1,"running":0,"sent_24h":0,"resumed_24h":0,"completed_24h":0}},"sessions":[{{"chat":"https://chatgpt.com/c/abc","title":"Pending proof","status":"complete","continuation":"completed","watched":True}}],"events":[]}}
def act(action):
    with open({str(calls)!r}, "a") as f:
        f.write("call\\n")
    time.sleep(1.0)
    open({str(marker)!r}, "w").write("done")
    return ActionResult("success", "fake finished")
raise SystemExit(run_tui(fetch, interval=1, act=act))
""")
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=root)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()

    def drain(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break

    def wait_for(predicate, seconds):
        deadline = time.monotonic() + seconds
        while not predicate() and time.monotonic() < deadline and process.poll() is None:
            drain(0.1)
        return predicate()

    try:
        assert wait_for(lambda: b"Pending proof" in output, 5)
        os.write(master, b"a")
        drain(0.2)
        os.write(master, b"w")
        drain(0.2)
        assert b"y/n" in output
        os.write(master, b"y")
        assert wait_for(lambda: calls.exists(), 3)
        os.write(master, b"q")
        drain(0.4)
        assert process.poll() is None and not marker.exists()
        assert b"still running" in output
        assert wait_for(lambda: marker.exists(), 5)
        assert wait_for(lambda: b"fake finished" in output, 3)
        os.write(master, b"q")
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
    assert calls.read_text().count("call") == 1
    assert b"Traceback" not in output


# --- malformed event identity: never crash the Events pane, never spoof an account ---------


def events_view(events, snapshot=None):
    from pmlib.chatgpt_tui import ViewState, render_lines

    snap = dict(snapshot or project(), events=events)
    return "\n".join(render_lines(snap, ViewState(pane="events"), 160, 40, NOW))


@pytest.mark.parametrize("chat", [None, 7, 1.5, True, ["x"], {"a": 1}])
def test_validation_rejects_malformed_event_chat(chat):
    from pmlib.chatgpt_monitor import MonitorError, validate_snapshot

    event = {"kind": "continuation-running", "chat": chat, "at": NOW}
    with pytest.raises(MonitorError, match="event"):
        validate_snapshot(dict(project(), events=[event]), "glide.vc")


@pytest.mark.parametrize(
    "event",
    [
        {"kind": None, "at": NOW},
        {"kind": ["x"], "at": NOW},
        {"kind": "k" * 129, "at": NOW},
        {"kind": "k", "at": "now"},
        {"kind": "k", "at": float("nan")},
        {"kind": "k", "at": float("inf")},
        {"kind": "k", "at": True},
        {"kind": "k", "baseline": "yes"},
        {"kind": "k", "id": 5},
        {"kind": "k", "chat": CHAT + "a" * 2100},
    ],
)
def test_validation_rejects_malformed_event_fields(event):
    from pmlib.chatgpt_monitor import MonitorError, validate_snapshot

    with pytest.raises(MonitorError, match="event"):
        validate_snapshot(dict(project(), events=[event]), "glide.vc")


@pytest.mark.parametrize(
    "event",
    [
        {"kind": "service-restart", "at": NOW},
        {"kind": "service-restart", "chat": "", "at": NOW, "baseline": False},
        {"kind": "continuation-running", "chat": CHAT, "at": NOW, "baseline": True, "id": "a1"},
        {"kind": "x", "at": None, "extra": {"nested": [1]}},
    ],
)
def test_validation_keeps_service_level_and_normal_events(event):
    from pmlib.chatgpt_monitor import validate_snapshot

    assert validate_snapshot(dict(project(), events=[event]), "glide.vc")["events"] == [event]


@pytest.mark.parametrize("chat", [None, 7, ["x"], {"a": 1}, b"bytes"])
def test_events_pane_survives_unvalidated_malformed_chat(chat):
    text = events_view([{"kind": "odd-event", "chat": chat, "at": NOW}])
    assert "odd-event" in text


def test_events_pane_service_level_event_without_chat():
    text = events_view([{"kind": "service-restart", "at": NOW}, {"kind": "svc", "chat": ""}])
    assert "service-restart" in text and "svc" in text


def test_events_pane_shows_chat_tail_and_trailing_slash():
    text = events_view([{"kind": "k1", "chat": "https://chatgpt.com/c/abc123/", "at": NOW}])
    assert "abc123" in text


def test_events_pane_chat_cannot_spoof_account_or_inject_controls():
    from pmlib.chatgpt_monitor import combine

    hostile = "https://chatgpt.com/c/ok\x1b[31m\x07] [home] ok\u202e"
    snap = combine(
        [("glide.vc", dict(project(), events=[{"kind": "k", "chat": hostile, "at": NOW}]), None)]
    )
    text = events_view(snap["events"], snapshot=snap)
    assert "[glide.vc]" in text
    assert "[home]" not in text
    assert "\x1b" not in text and "\x07" not in text and "\u202e" not in text


@pytest.mark.parametrize(
    "kind",
    ["paused | [home] service stopped", "x\x1b[2Jy", "a\u202eb", "bell\x07", "k" * 500, None, 7],
)
def test_events_pane_kind_cannot_spoof_account_or_inject_controls(kind):
    from pmlib.chatgpt_monitor import combine

    snap = combine(
        [("glide.vc", dict(project(), events=[{"kind": kind, "chat": CHAT, "at": NOW}]), None)]
    )
    text = events_view(snap["events"], snapshot=snap)
    assert "[glide.vc]" in text and "[home]" not in text and "service stopped" not in text
    assert not {"\x1b", "\x07", "\u202e"} & set(text)


def test_events_pane_null_chat_is_malformed_not_service_level():
    assert "(unrecognized)" in events_view([{"kind": "k", "chat": None, "at": NOW}])
    assert "(unrecognized)" not in events_view(
        [{"kind": "k", "at": NOW}, {"kind": "k", "chat": ""}]
    )


def test_combined_account_label_not_overridable_by_event():
    from pmlib.chatgpt_monitor import combine

    event = {"kind": "k", "chat": CHAT, "at": NOW, "account": "home"}
    snap = combine([("glide.vc", dict(project(), events=[event]), None)])
    assert snap["events"][0]["account"] == "glide.vc"


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY is unavailable on Windows")
def test_real_terminal_events_view_with_malformed_and_service_events(tmp_path):
    import fcntl
    import select
    import struct
    import termios

    root = os.path.dirname(os.path.dirname(__file__))
    script = tmp_path / "pty_events.py"
    script.write_text("""from pmlib.chatgpt_tui import run_tui
now = __import__("time").time()
snapshot={"workspace":"glide.vc","generated_at":now,"heartbeat_at":None,"stale":False,"connection":"connected","summary":{"enrolled":1,"present":1,"available":1,"running":0,"sent_24h":0,"resumed_24h":0,"completed_24h":0},"sessions":[{"chat":"https://chatgpt.com/c/abc","title":"Events proof","status":"complete","continuation":"completed","watched":True}],"events":[{"kind":"evt-null","chat":None,"at":now},{"kind":"evt-int","chat":7,"at":now},{"kind":"evt-service","at":now},{"kind":"evt-real","chat":"https://chatgpt.com/c/realchat","at":now}]}
raise SystemExit(run_tui(lambda:snapshot,interval=2))
""")
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 32, 150, 0, 0))
    env = dict(os.environ, TERM="xterm-256color", PYTHONPATH=root)
    process = subprocess.Popen(
        [sys.executable, str(script)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    output = bytearray()

    def drain(duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break

    def wait_for(needle, seconds):
        deadline = time.monotonic() + seconds
        while needle not in output and time.monotonic() < deadline and process.poll() is None:
            drain(0.1)
        return needle in output

    try:
        assert wait_for(b"Events proof", 5)
        os.write(master, b"\t")
        assert wait_for(b"evt-real", 3)
        for needle in (b"evt-null", b"evt-int", b"evt-service", b"realchat"):
            assert needle in output
        os.write(master, b"jjj")
        drain(0.3)
        os.write(master, b"\n")
        drain(0.3)
        assert process.poll() is None
        os.write(master, b"q")
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
    assert b"Traceback" not in output and b"AttributeError" not in output
