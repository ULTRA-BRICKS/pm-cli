"""Exercise pm's real parser/subprocess seam with an isolated installed CLI stand-in."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CHAT = "https://chatgpt.com/c/example-chat"
CONTROLS = ("watch", "unwatch", "sol-opt-in", "sol-opt-out", "stop", "resume")


@pytest.fixture
def guardian(tmp_path, monkeypatch):
    executable = tmp_path / "chatgpt-guardian"
    calls = tmp_path / "calls.json"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['GUARDIAN_CALLS']).write_text(json.dumps(sys.argv[1:]))\n"
        "print('guardian output')\n"
        "code = int(os.environ.get('GUARDIAN_EXIT', '0'))\n"
        "if code < 0: os.kill(os.getpid(), -code)\n"
        "sys.exit(code)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("GUARDIAN_CALLS", str(calls))
    return calls


def invoke(*args):
    return subprocess.run(
        [sys.executable, "-m", "pmlib.cli", "chatgpt", *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("args", [(), ("status",)])
def test_default_observes(guardian, args):
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == ["--workspace", "home", "status"]
    assert result.stdout == "guardian output\n"


@pytest.mark.parametrize("action", ["watch", "unwatch", "sol-opt-in", "sol-opt-out"])
def test_controls_require_apply(guardian, action):
    result = invoke(action, CHAT)
    assert result.returncode == 2
    assert "--apply" in result.stderr
    assert not guardian.exists()
    result = invoke(action, CHAT, "--apply")
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == ["--workspace", "home", action, CHAT]


@pytest.mark.parametrize("action", ["stop", "resume"])
def test_stop_resume_explicit_target(guardian, action):
    assert invoke(action, "--apply").returncode == 2
    assert not guardian.exists()
    assert invoke(action, "--chat", CHAT).returncode == 2
    assert not guardian.exists()
    assert invoke(action, "--chat", CHAT, "--apply").returncode == 0
    assert json.loads(guardian.read_text()) == ["--workspace", "home", action, "--chat", CHAT]
    assert invoke(action, "--chat", "*", "--apply").returncode == 0
    assert json.loads(guardian.read_text()) == ["--workspace", "home", action, "--chat", "*"]


@pytest.mark.parametrize("action", CONTROLS)
@pytest.mark.parametrize("chat", [
    "--help", "--state=other.sqlite", "-h", "",
    "https://chatgpt.com", "https://chatgpt.com/share/example",
    "https://chatgpt.com/c/", "http://chatgpt.com/c/example",
    "https://chatgpt.com.evil/c/example", "https://chatgpt.com@evil/c/example",
    "https://user@chatgpt.com/c/example", "https://chatgpt.com:443/c/example",
    "HTTPS://chatgpt.com/c/example", "https://chatgpt.com/c/example?x=1",
    "https://chatgpt.com/c/example#part", "https://chatgpt.com/c/example/extra",
    "https://chatgpt.com/c/../example", "https://chatgpt.com/c/a%2Fb",
    "https://chatgpt.com/c/%65xample", "https://chatgpt.com/c/a\\b",
    " https://chatgpt.com/c/example", "https://chatgpt.com/c/example\n",
    "https://chatgpt.com/c/exa\tmple", "https://chatgpt.com/c/\u00e9xample",
    "https://[chatgpt.com/c/example", "https://chatgpt.com/c/a;echo injected",
    "https://chatgpt.com/c/$(echo injected)", "https://chatgpt.com/c/a\x1b[0m",
])
def test_invalid_chat_never_reaches_downstream(guardian, action, chat):
    if action in ("stop", "resume"):
        args = (action, "--apply", f"--chat={chat}")
    else:
        args = (action, "--apply", "--", chat)
    result = invoke(*args)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "permanent" in result.stderr
    assert not result.stdout
    assert not guardian.exists()


@pytest.mark.parametrize("action", ["watch", "unwatch", "sol-opt-in", "sol-opt-out"])
def test_all_chats_target_requires_stop_or_resume(guardian, action):
    result = invoke(action, "*", "--apply")
    assert result.returncode == 2
    assert "permanent" in result.stderr
    assert not guardian.exists()


@pytest.mark.parametrize("action", CONTROLS)
@pytest.mark.parametrize("chat", [CHAT, "https://chatgpt.com/c/Az09_-2/"])
def test_valid_identity_forwarded_literally(guardian, action, chat):
    target = ["--chat", chat] if action in ("stop", "resume") else ["--", chat]
    result = invoke(action, "--apply", *target)
    assert result.returncode == 0, result.stderr
    delegated = ["--chat", chat] if action in ("stop", "resume") else [chat]
    assert json.loads(guardian.read_text()) == ["--workspace", "home", action, *delegated]


def test_literal_help_cannot_become_guardian_option(guardian):
    executable = guardian.parent / "chatgpt-guardian"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import argparse, os\n"
        "from pathlib import Path\n"
        "Path(os.environ['GUARDIAN_CALLS']).write_text('called')\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--workspace')\n"
        "watch = parser.add_subparsers().add_parser('watch')\n"
        "watch.add_argument('chat')\n"
        "parser.parse_args()\n"
    )
    result = invoke("watch", "--apply", "--", "--help")
    assert result.returncode == 2, result.stdout
    assert "permanent" in result.stderr
    assert not result.stdout
    assert not guardian.exists()


@pytest.mark.parametrize(
    "args",
    [
        ("run", "--apply"),
        ("init",),
        ("quota-clear", "--apply"),
        ("status", "--apply"),
        ("watch", CHAT, "--app"),
        ("watch", CHAT, "--apply", "--token-file", "secret"),
    ],
)
def test_no_passthrough_or_opt_in_abbreviation(guardian, args):
    assert invoke(*args).returncode == 2
    assert not guardian.exists()


@pytest.mark.parametrize("code", [2, 19, -15])
def test_exit_code_propagation(guardian, monkeypatch, code):
    monkeypatch.setenv("GUARDIAN_EXIT", str(code))
    result = invoke("status")
    assert result.returncode == (128 - code if code < 0 else code)


def test_explicit_shared_state(guardian, tmp_path):
    state = tmp_path / "shared.sqlite"
    result = invoke("--state", str(state), "status")
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == [
        "--workspace", "home", f"--state={state}", "status"
    ]
    assert not state.exists()


@pytest.mark.parametrize("workspace", ["home", "glide.vc"])
@pytest.mark.parametrize(
    "args, delegated",
    [
        ((), ["status"]),
        (("status",), ["status"]),
    ]
    + [
        ((action, CHAT, "--apply"), [action, CHAT])
        for action in ("watch", "unwatch", "sol-opt-in", "sol-opt-out")
    ]
    + [
        ((action, "--chat", "*", "--apply"), [action, "--chat", "*"])
        for action in ("stop", "resume")
    ],
)
def test_workspace_forwarding(guardian, workspace, args, delegated):
    result = invoke("--workspace", workspace, *args)
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == ["--workspace", workspace, *delegated]


def test_workspace_and_state_forwarded_together(guardian, tmp_path):
    state = str(tmp_path / "glide.sqlite")
    result = invoke("--workspace", "glide.vc", "--state", state, "status")
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == [
        "--workspace", "glide.vc", f"--state={state}", "status"
    ]


@pytest.mark.parametrize("args", [
    ("--workspace", "unknown", "status"),
    ("--work", "glide.vc", "status"),
    ("status", "--workspace", "glide.vc"),
    ("--workspace", "glide.vc", "watch", CHAT),
    ("--workspace", "glide.vc", "stop", "--apply"),
    ("--workspace", "glide.vc", "resume", "--apply"),
])
def test_invalid_workspace_or_unguarded_controls_do_not_delegate(guardian, args):
    assert invoke(*args).returncode == 2
    assert not guardian.exists()


@pytest.fixture
def actual_guardian(tmp_path, monkeypatch):
    """Opt-in contract check against a Guardian checkout, without changing that repo."""
    source = os.environ.get("GUARDIAN_TEST_SOURCE")
    if not source:
        pytest.skip("set GUARDIAN_TEST_SOURCE to Guardian's lib directory")
    assert (Path(source) / "agenttools_chatgpt" / "cli.py").is_file()
    executable = tmp_path / "chatgpt-guardian"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {source!r})\n"
        "from agenttools_chatgpt.cli import main\n"
        "raise SystemExit(main())\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    return tmp_path / ".local/state/agent-tools/chatgpt-guardian"


@pytest.mark.parametrize("status_args", [(), ("status",)])
def test_actual_guardian_workspace_isolation(actual_guardian, status_args):
    assert invoke("watch", CHAT, "--apply").returncode == 0
    home = invoke("--workspace", "home", *status_args)
    glide = invoke("--workspace", "glide.vc", *status_args)
    assert home.returncode == 0, home.stderr
    assert glide.returncode == 0, glide.stderr
    assert json.loads(home.stdout)["controls"]["watch:" + CHAT] == "1"
    assert "watch:" + CHAT not in json.loads(glide.stdout)["controls"]
    assert (actual_guardian / "home/state.sqlite").is_file()
    assert (actual_guardian / "glide.vc/state.sqlite").is_file()
    mismatch = invoke(
        "--workspace", "glide.vc", "--state",
        str(actual_guardian / "home/state.sqlite"), "status",
    )
    assert mismatch.returncode == 2
    assert "different account" in mismatch.stderr
    assert not mismatch.stdout


@pytest.mark.parametrize("workspace", ["home", "glide.vc"])
@pytest.mark.parametrize("state", ["--help", "state with spaces.sqlite", "state.sqlite"])
def test_actual_guardian_literal_state(actual_guardian, tmp_path, monkeypatch, workspace, state):
    monkeypatch.setenv("PYTHONPATH", str(Path.cwd()))
    monkeypatch.chdir(tmp_path)
    result = invoke("--workspace", workspace, f"--state={state}", "status")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["controls"] == {}
    assert (tmp_path / state).is_file()
    assert not actual_guardian.exists()


def test_missing_install_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("AUTH_TOKEN", "must-not-appear")
    result = invoke()
    assert result.returncode == 127
    assert "install" in result.stderr.lower()
    assert "must-not-appear" not in result.stdout + result.stderr


@pytest.mark.parametrize("args", [("--help",), ("doctor",)])
def test_startup_is_stdlib_only(tmp_path, args):
    script = (
        "import sys\n"
        "class DenyGuardian:\n"
        " def find_spec(self, fullname, *args):\n"
        "  if fullname.startswith(('agenttools_chatgpt', 'playwright', 'yaml')):\n"
        "   raise AssertionError(fullname)\n"
        "sys.meta_path.insert(0, DenyGuardian())\n"
        "from pmlib.cli import main\n"
        f"raise SystemExit(main({list(args)!r}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-S", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PM_HOME": str(tmp_path / "pm")},
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("workspace", ["home", "glide.vc"])
@pytest.mark.parametrize("action", ["status", *CONTROLS])
@pytest.mark.parametrize("state", [None, "--help", "state with spaces;$(echo literal).sqlite"])
def test_state_selection_delegated_for_every_control(guardian, monkeypatch, workspace, action, state):
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", "/guardian/owns/environment.sqlite")
    args = ["--workspace", workspace]
    expected = ["--workspace", workspace]
    if state is not None:
        args.append(f"--state={state}")
        expected.append(f"--state={state}")
    args.append(action)
    expected.append(action)
    if action != "status":
        target = ["--chat", CHAT] if action in ("stop", "resume") else [CHAT]
        args.extend([*target, "--apply"])
        expected.extend(target)
    result = invoke(*args)
    assert result.returncode == 0, result.stderr
    assert json.loads(guardian.read_text()) == expected


@pytest.mark.parametrize("error,code,message", [
    (PermissionError("secret"), 126, "cannot execute chatgpt-guardian"),
    (subprocess.TimeoutExpired("secret", 30), 124, "timed out"),
])
def test_guardian_execution_errors_are_bounded(monkeypatch, capsys, error, code, message):
    from pmlib.cli import main

    def fail(command, **kwargs):
        assert command == ["chatgpt-guardian", "--workspace", "home", "status"]
        assert kwargs == {"check": False, "timeout": 30}
        raise error

    monkeypatch.setattr(subprocess, "run", fail)
    assert main(["chatgpt"]) == code
    output = capsys.readouterr()
    assert message in output.err
    assert "secret" not in output.out + output.err
