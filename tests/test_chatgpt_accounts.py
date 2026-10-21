"""Multi-account discovery, combined presentation and confirmed TUI actions (no live changes)."""

import argparse
import json
import os
import subprocess
import sys
import time

import pytest

from tests.test_chatgpt_tui import CHAT, NOW, project, sample


def snap(workspace, rows=None, **kw):
    data = project(rows)
    data["workspace"] = workspace
    data.update(kw)
    return data


def fake_fetch(monkeypatch, failing=()):
    from pmlib import chatgpt_monitor

    def fetch(workspace, state=None):
        assert workspace in ("home", "glide.vc")
        if workspace in failing:
            raise chatgpt_monitor.MonitorError(f"{workspace} telemetry down")
        return snap(workspace)

    monkeypatch.setattr(chatgpt_monitor, "fetch_snapshot", fetch)


def combined(**per):
    from pmlib.chatgpt_monitor import combine

    return combine([(ws, per.get(ws.replace(".", "_")), None) for ws in ("home", "glide.vc")])


def test_one_account_failure_does_not_hide_other(monkeypatch):
    from pmlib.chatgpt_monitor import fetch_combined
    from pmlib.chatgpt_tui import ViewState, render_lines

    fake_fetch(monkeypatch, failing=("home",))
    data = fetch_combined(("home", "glide.vc"))
    accounts = {a["workspace"]: a for a in data["accounts"]}
    assert not accounts["home"]["ok"] and "telemetry down" in accounts["home"]["error"]
    assert accounts["glide.vc"]["ok"]
    assert {r["account"] for r in data["sessions"]} == {"glide.vc"}
    assert all(e["account"] == "glide.vc" for e in data["events"])
    text = "\n".join(render_lines(data, ViewState(), 160, 40, now=NOW))
    assert "home: UNAVAILABLE" in text and "telemetry down" in text
    assert "glide.vc: service active" in text and "[glide.vc]" in text


def test_same_chat_two_accounts_routes_to_selected_account(monkeypatch):
    from pmlib.chatgpt_tui import plan_action, run_action

    data = combined(home=snap("home"), glide_vc=snap("glide.vc"))
    glide_row = next(r for r in data["sessions"] if r["account"] == "glide.vc")
    action, refusal = plan_action(data, glide_row, "watch", now=NOW)
    assert refusal is None and action.account == "glide.vc" and action.chat == CHAT
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    result = run_action(action, runner=runner)
    command, kwargs = calls[0]
    assert isinstance(command, list) and not kwargs.get("shell")
    assert command[command.index("--workspace") + 1] == "glide.vc"
    assert command[-3:] == ["watch", CHAT, "--apply"] and "home" not in command
    assert result.outcome == "success"


@pytest.mark.parametrize(
    "code,outcome", [(0, "success"), (3, "pending"), (124, "uncertain"), (1, "failed")]
)
def test_action_outcomes_honest(code, outcome):
    from pmlib.chatgpt_tui import Action, run_action

    result = run_action(
        Action("glide.vc", "service-start"),
        runner=lambda c, **k: subprocess.CompletedProcess(c, code, "", "boom"),
    )
    assert result.outcome == outcome
    if outcome == "success":
        assert "not all chats" in result.message


def test_action_timeout_is_uncertain_and_not_retried():
    from pmlib.chatgpt_tui import Action, run_action

    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    result = run_action(Action("home", "resume", CHAT), runner=runner)
    assert result.outcome == "uncertain" and len(calls) == 1


def test_resume_reports_remaining_blocks():
    from pmlib.chatgpt_tui import Action, run_action

    result = run_action(
        Action("home", "resume", CHAT),
        runner=lambda c, **k: subprocess.CompletedProcess(c, 0, "", ""),
    )
    assert "quota" in result.message and "not cleared" in result.message


def test_refusals_conservative_but_pause_available():
    from pmlib.chatgpt_tui import plan_action

    data = combined(home=snap("home", stale=True), glide_vc=None)
    row = data["sessions"][0]
    for kind in ("watch", "unwatch", "resume"):
        assert plan_action(data, row, kind, now=NOW)[1]
    assert plan_action(data, row, "pause", now=NOW)[1] is None
    assert plan_action(data, row, "service-stop", now=NOW)[0].account == "home"
    assert plan_action(data, dict(row, account="all"), "service-stop", now=NOW)[1]
    assert plan_action(data, dict(row, chat="https://evil/c/x"), "pause", now=NOW)[1]
    dup = combined(home=snap("home", [sample(), sample()]))
    assert "ambiguous" in plan_action(dup, dup["sessions"][0], "pause", now=NOW)[1]


def test_menu_confirm_and_cancel_flow():
    from pmlib.chatgpt_tui import ViewState, render_lines

    data = combined(home=snap("home"), glide_vc=snap("glide.vc"))
    state = ViewState()
    state.key("j", 2)
    assert state.key("a", 2, data) is None and state.menu
    assert "w watch" in "\n".join(render_lines(data, state, 160, 40, now=NOW))
    assert state.key("w", 2, data, now=NOW) is None and state.confirm
    prompt = "\n".join(render_lines(data, state, 160, 40, now=NOW))
    assert "watch" in prompt and "glide.vc" in prompt and CHAT in prompt and "y/n" in prompt
    assert state.key("\x1b", 2, data) is None and not state.confirm and not state.menu
    state.key("a", 2, data)
    state.key("w", 2, data, now=NOW)
    assert state.key("n", 2, data) is None and state.confirm is None
    state.key("a", 2, data)
    state.key("w", 2, data, now=NOW)
    kind, action = state.key("y", 2, data, now=NOW)
    assert kind == "action" and action.account == "glide.vc"
    assert state.key("r", 2, data) == "refresh"


def test_cli_workspace_all_guards(monkeypatch, capsys):
    from pmlib.commands import chatgpt

    parser = argparse.ArgumentParser()
    chatgpt.configure(parser)
    for argv in (
        ["--workspace", "all", "--state", "x", "monitor", "--json"],
        ["--workspace", "all", "watch", CHAT, "--apply"],
        ["--workspace", "all", "service", "stop", "--apply"],
        ["--workspace", "all", "status"],
    ):
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not delegate"))
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("must not delegate"))
        assert chatgpt.run(parser.parse_args(argv)) == 2


def test_monitor_json_all_combines(monkeypatch, capsys):
    from pmlib.commands import chatgpt

    fake_fetch(monkeypatch, failing=("glide.vc",))
    parser = argparse.ArgumentParser()
    chatgpt.configure(parser)
    assert chatgpt.run(parser.parse_args(["--workspace", "all", "monitor", "--json"])) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["workspace"] == "all" and len(data["accounts"]) == 2
    assert chatgpt.run(parser.parse_args(["--workspace", "all", "monitor"])) == 0
    out = capsys.readouterr().out
    assert "[home]" in out and "glide.vc: UNAVAILABLE" in out


def test_accounts_lists_unknown_not_zero(monkeypatch, capsys):
    from pmlib import chatgpt_monitor
    from pmlib.commands import chatgpt

    fake_fetch(monkeypatch, failing=("glide.vc",))
    monkeypatch.setattr(
        chatgpt_monitor,
        "service_state",
        lambda ws: (
            {"configured": True, "active": "active", "error": None}
            if ws == "home"
            else {"configured": None, "active": "unknown", "error": "systemctl unavailable"}
        ),
    )
    parser = argparse.ArgumentParser()
    chatgpt.configure(parser)
    assert chatgpt.run(parser.parse_args(["accounts", "--json"])) == 0
    rows = {r["workspace"]: r for r in json.loads(capsys.readouterr().out)["accounts"]}
    assert rows["home"]["configured"] is True and rows["home"]["monitor"]["sessions"] == 1
    glide = rows["glide.vc"]
    assert glide["configured"] is None and glide["monitor"]["ok"] is False
    assert glide["monitor"]["sessions"] is None and "down" in glide["monitor"]["error"]
    assert chatgpt.run(parser.parse_args(["accounts"])) == 0
    out = capsys.readouterr().out
    assert "glide.vc" in out and "unknown" in out and "systemctl unavailable" in out


def test_service_state_readonly_and_bounded(monkeypatch):
    from pmlib import chatgpt_monitor

    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, "LoadState=not-found\nActiveState=inactive\n", ""
        )

    monkeypatch.setattr(subprocess, "run", runner)
    state = chatgpt_monitor.service_state("glide.vc")
    command, kwargs = calls[0]
    assert "show" in command and "chatgpt-guardian-glide.service" in command
    assert kwargs["timeout"] <= 10 and not kwargs.get("shell")
    assert state["configured"] is False


@pytest.mark.skipif(sys.platform == "win32", reason="Unix PTY is unavailable on Windows")
def test_real_terminal_action_confirm_cancel(tmp_path):
    import fcntl
    import select
    import struct
    import termios

    root = os.path.dirname(os.path.dirname(__file__))
    log = tmp_path / "actions.json"
    script = tmp_path / "pty_actions.py"
    script.write_text(f"""import json, time
from pmlib.chatgpt_monitor import combine
from pmlib.chatgpt_tui import ActionResult, run_tui
from tests.test_chatgpt_tui import project
def snap(ws):
    d = project(); d["workspace"] = ws; d["generated_at"] = time.time(); return d
calls = []
def act(action):
    calls.append([action.account, action.kind, action.chat])
    open({str(log)!r}, "w").write(json.dumps(calls))
    return ActionResult("success", "watch accepted (fake)")
raise SystemExit(run_tui(lambda: combine([("home", snap("home"), None), ("glide.vc", snap("glide.vc"), None)]), interval=2, act=act))
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

    try:
        deadline = time.monotonic() + 5
        while b"[glide.vc]" not in output and time.monotonic() < deadline:
            drain(0.1)
        assert b"[glide.vc]" in output
        os.write(master, b"ja")
        drain(0.3)
        os.write(master, b"w")
        drain(0.3)
        assert b"y/n" in output
        os.write(master, b"n")
        drain(0.3)
        assert not log.exists()
        os.write(master, b"aw\x1b")
        drain(0.5)
        assert not log.exists()
        os.write(master, b"awy")
        deadline = time.monotonic() + 3
        while not log.exists() and time.monotonic() < deadline:
            drain(0.1)
        drain(0.3)
        assert json.loads(log.read_text()) == [["glide.vc", "watch", CHAT]]
        assert b"watch accepted (fake)" in output
        os.write(master, b"q")
        assert process.wait(timeout=3) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)
        os.close(master)
    assert b"Traceback" not in output


def test_accounts_rejects_state_and_single_workspace(monkeypatch, capsys):
    from pmlib.commands import chatgpt

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not probe"))
    parser = argparse.ArgumentParser()
    chatgpt.configure(parser)
    for argv in (["--state", "x", "accounts"], ["--workspace", "home", "accounts"]):
        assert chatgpt.run(parser.parse_args(argv)) == 2


def test_account_focus_reaches_unavailable_account_for_service_actions():
    from pmlib.chatgpt_monitor import combine
    from pmlib.chatgpt_tui import ViewState

    data = combine([("home", snap("home"), None), ("glide.vc", None, "telemetry down")])
    state = ViewState()
    state.key("A", 1, data)
    state.key("A", 1, data)
    assert state.focus == "glide.vc"
    state.key("a", 0, data, now=NOW)
    state.key("S", 0, data, now=NOW)
    assert state.confirm is None and "Refused" in state.notice and "unavailable" in state.notice
    state.key("a", 0, data, now=NOW)
    state.key("X", 0, data, now=NOW)
    assert state.confirm.account == "glide.vc" and state.confirm.kind == "service-stop"
    state.key("n", 0, data)
    state.key("a", 0, data, now=NOW)
    state.key("w", 0, data, now=NOW)
    assert state.confirm is None and "Refused" in state.notice


def test_custom_state_view_is_read_only():
    from pmlib.chatgpt_tui import ViewState

    state = ViewState(actions=False)
    assert state.key("a", 1, project()) is None and not state.menu
    assert "read-only" in state.notice
