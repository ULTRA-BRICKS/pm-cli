"""Exercise service parsing and the systemctl boundary without touching live user units."""

import io
import subprocess
import sys

import pytest

from pmlib.cli import main

SHOW = [
    "--property=LoadState", "--property=ActiveState", "--property=SubState",
    "--property=Job",
]


@pytest.mark.parametrize("delayed", [False, True])
def test_nonfollow_logs_realprocess_capture(tmp_path, monkeypatch, delayed):
    executable = tmp_path / "journalctl"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        f"time.sleep({12 if delayed else 0})\n"
        "print('journal stdout', flush=True)\n"
        "print('journal stderr', file=sys.stderr, flush=True)\n"
        "sys.exit(4)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "pmlib.cli", "chatgpt", "logs"],
        capture_output=True, text=True, timeout=11, check=False,
    )
    if delayed:
        assert result.returncode == 124
        assert result.stdout == ""
        assert "timed out" in result.stderr
        assert "journal stdout" not in result.stdout
        assert "journal stderr" not in result.stderr
    else:
        assert result.returncode == 4
        assert result.stdout == "journal stdout\n"
        assert result.stderr.startswith("journal stderr\n")
        assert "journalctl failed (exit 4)" in result.stderr


@pytest.fixture
def systemctl(monkeypatch):
    calls = []
    processes = []

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            processes.append(self)

        def communicate(self, *, timeout):
            assert timeout == 10
            if "show" in calls[processes.index(self)][0]:
                return "LoadState=loaded\nActiveState=active\nSubState=running\nJob=0\n", None
            return "", None

        def kill(self):
            pytest.fail("must not force kill systemctl or the service")

        def terminate(self):
            pytest.fail("must not terminate systemctl or the service")

    monkeypatch.setattr(subprocess, "Popen", Process)
    return calls, processes


def show_command(unit):
    return ["systemctl", "--user", "--no-pager", "show", unit, *SHOW]


@pytest.mark.parametrize("workspace,unit", [
    ("home", "chatgpt-guardian-home.service"),
    ("glide.vc", "chatgpt-guardian-glide.service"),
])
@pytest.mark.parametrize("action", [[], ["status"]])
def test_service_status_default_exact_argv(systemctl, capsys, workspace, unit, action):
    calls, _ = systemctl
    assert main(["chatgpt", "--workspace", workspace, "service", *action]) == 0
    assert calls == [(show_command(unit), {
        "stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "text": True,
    })]
    output = capsys.readouterr()
    assert unit in output.out and "active/running" in output.out
    assert not output.err


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
@pytest.mark.parametrize("workspace,unit", [
    ("home", "chatgpt-guardian-home.service"),
    ("glide.vc", "chatgpt-guardian-glide.service"),
])
def test_service_mutation_exact_argv_and_observation(systemctl, capsys, action, workspace, unit):
    calls, processes = systemctl
    if action == "stop":
        # The observed state may lag a queued systemd request.
        expected = 3
    else:
        expected = 0
    assert main(["chatgpt", "--workspace", workspace, "service", action, "--apply"]) == expected
    assert [call[0] for call in calls] == [
        ["systemctl", "--user", "--no-block", action, unit],
        show_command(unit),
    ]
    assert len(processes) == 2
    output = capsys.readouterr()
    assert "request accepted" in output.out
    if action == "stop":
        assert "pending" in output.out
    assert not output.err


@pytest.mark.parametrize("args", [
    ["service", "start"], ["service", "stop"], ["service", "restart"],
    ["--workspace", "glide", "service"],
    ["--workspace", "evil.service", "service", "start", "--apply"],
    ["service", "start", "--app"],
    ["service", "status", "--apply"], ["service", "disable", "--apply"],
    ["service", "start", "--apply", "arbitrary.service"],
])
def test_rejected_service_arguments_never_spawn(systemctl, args):
    with pytest.raises(SystemExit) as exc:
        main(["chatgpt", *args])
    assert exc.value.code == 2
    assert not systemctl[0]


def test_service_rejects_state_override(systemctl, capsys):
    assert main(["chatgpt", "--state", "shared.sqlite", "service"]) == 2
    assert "--state" in capsys.readouterr().err
    assert not systemctl[0]


@pytest.mark.parametrize("error,code,message", [
    (FileNotFoundError(), 127, "systemctl is unavailable"),
    (PermissionError("token-must-not-leak"), 126, "cannot execute systemctl"),
])
def test_service_spawn_errors(monkeypatch, capsys, error, code, message):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "Popen", fail)
    assert main(["chatgpt", "service"]) == code
    output = capsys.readouterr()
    assert message in output.err
    assert "token-must-not-leak" not in output.out + output.err


@pytest.mark.parametrize("code", [1, 4, 19, -15])
def test_service_nonzero_is_honest_and_redacted(systemctl, monkeypatch, capsys, code):
    calls, _ = systemctl
    process_type = subprocess.Popen
    monkeypatch.setattr(process_type, "returncode", code)
    monkeypatch.setattr(process_type, "communicate", lambda self, **kw: ("secret-token", None))
    assert main(["chatgpt", "service", "start", "--apply"]) == (
        code if code >= 0 else 128 - code
    )
    assert len(calls) == 1
    output = capsys.readouterr()
    assert "failed" in output.err and "systemctl" in output.err
    assert "secret-token" not in output.out + output.err
    assert "request accepted" not in output.out


@pytest.mark.parametrize("action", [[], ["start", "--apply"]])
def test_service_timeout_does_not_kill_or_claim_success(systemctl, monkeypatch, capsys, action):
    def timeout(self, **kwargs):
        raise subprocess.TimeoutExpired("sensitive-token", kwargs["timeout"], "secret-output")
    monkeypatch.setattr(subprocess.Popen, "communicate", timeout)
    assert main(["chatgpt", "service", *action]) == 124
    output = capsys.readouterr()
    assert "timed out" in output.err and "may still be pending" in output.err
    assert "sensitive-token" not in output.out + output.err
    assert "secret-output" not in output.out + output.err


@pytest.mark.parametrize("properties,code,message", [
    ("LoadState=not-found\nActiveState=inactive\nSubState=dead\nJob=0\n", 4, "not installed"),
    ("LoadState=loaded\nActiveState=activating\nSubState=start\nJob=37\n", 3, "pending"),
    ("LoadState=loaded\nActiveState=active\nSubState=running\nJob=37\n", 3, "pending"),
    ("LoadState=loaded\nActiveState=failed\nSubState=failed\nJob=0\n", 1, "failed"),
    ("LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=0\n", 0, "inactive/dead"),
    ("token=secret\n", 1, "unrecognized"),
    ("LoadState=loaded\nActiveState=secret\nSubState=secret\nJob=0\n", 1, "unrecognized"),
    ("LoadState=loaded\nActiveState=maintenance\nSubState=secret\nJob=\n", 1, "unrecognized"),
    ("LoadState=loaded\nActiveState=maintenance\nSubState=clean\n", 1, "unrecognized"),
    ("LoadState=loaded\nActiveState=maintenance\nSubState=clean\nJob=secret\n", 1, "unrecognized"),
    ("LoadState=loaded\nActiveState=maintenance\nSubState=clean\nJob=٣٧\n", 1, "unrecognized"),
])
def test_service_status_reports_observed_state(systemctl, monkeypatch, capsys,
                                               properties, code, message):
    monkeypatch.setattr(subprocess.Popen, "communicate", lambda self, **kw: (properties, None))
    assert main(["chatgpt", "service"]) == code
    output = capsys.readouterr()
    assert message in output.out + output.err
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("active,sub,job,code,message", [
    ("deactivating", "stop-watchdog", "37", 3, "pending"),
    ("deactivating", "final-watchdog", "37", 3, "pending"),
    ("maintenance", "cleaning", "", 3, "pending"),
    ("inactive", "dead-before-auto-restart", "37", 3, "pending"),
    ("failed", "failed-before-auto-restart", "37", 1, "failed or unavailable"),
    ("inactive", "dead-resources-pinned", "0", 0, "observed"),
    ("maintenance", "clean", "", 3, "pending"),
    ("maintenance", "clean", "0", 3, "pending"),
])
@pytest.mark.parametrize("action", [[], ["status"], ["restart", "--apply"]])
def test_service_legitimate_systemd_states(systemctl, monkeypatch, capsys,
                                         active, sub, job, code, message, action):
    properties = f"LoadState=loaded\nActiveState={active}\nSubState={sub}\nJob={job}\n"
    monkeypatch.setattr(subprocess.Popen, "communicate", lambda self, **kw: (properties, None))
    expected = 3 if action == ["restart", "--apply"] and code == 0 else code
    assert main(["chatgpt", "service", *action]) == expected
    output = capsys.readouterr()
    assert f"{active}/{sub}" in output.out
    assert ("pending" if expected == 3 else message) in output.out
    assert not output.err


@pytest.mark.parametrize("args", [
    ["--help"], ["chatgpt", "--help"], ["chatgpt", "service", "--help"],
    ["chatgpt", "service", "status", "--help"],
    ["chatgpt", "service", "start", "--help"],
    ["chatgpt", "service", "stop", "--help"],
    ["chatgpt", "service", "restart", "--help"],
    ["chatgpt", "logs", "--help"],
])
def test_help_has_no_subprocess(monkeypatch, args):
    def forbidden(*args, **kwargs):
        pytest.fail("help must not spawn any subprocess")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(SystemExit) as exc:
        main(args)
    assert exc.value.code == 0


@pytest.mark.parametrize("job", ["", "0", "0 /", "37 /org/freedesktop/systemd1/job/37"])
def test_service_job_forms(systemctl, monkeypatch, capsys, job):
    properties = f"LoadState=loaded\nActiveState=active\nSubState=running\nJob={job}\n"
    monkeypatch.setattr(subprocess.Popen, "communicate", lambda self, **kw: (properties, None))
    pending = job.startswith("37")
    assert main(["chatgpt", "service", "restart", "--apply"]) == (3 if pending else 0)
    output = capsys.readouterr()
    assert ("pending" in output.out) == pending


@pytest.mark.parametrize("workspace,unit", [
    ("home", "chatgpt-guardian-home.service"),
    ("glide.vc", "chatgpt-guardian-glide.service"),
])
def test_service_stop_observes_inactive(systemctl, monkeypatch, capsys, workspace, unit):
    properties = "LoadState=loaded\nActiveState=inactive\nSubState=dead\nJob=\n"
    monkeypatch.setattr(subprocess.Popen, "communicate", lambda self, **kw: (properties, None))
    assert main(["chatgpt", "--workspace", workspace, "service", "stop", "--apply"]) == 0
    assert [call[0] for call in systemctl[0]] == [
        ["systemctl", "--user", "--no-block", "stop", unit], show_command(unit),
    ]
    output = capsys.readouterr()
    assert "request accepted" in output.out and "inactive/dead" in output.out
    assert "pending" not in output.out


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_mutation_accepted_but_observation_fails(systemctl, monkeypatch, capsys, failure):
    calls, _ = systemctl

    def communicate(self, **kwargs):
        if len(calls) == 1:
            return "", None
        if failure == "timeout":
            raise subprocess.TimeoutExpired("systemctl", kwargs["timeout"])
        self.returncode = 4
        return "sensitive-output", None

    monkeypatch.setattr(subprocess.Popen, "communicate", communicate)
    expected = 124 if failure == "timeout" else 4
    assert main(["chatgpt", "service", "restart", "--apply"]) == expected
    output = capsys.readouterr()
    assert "request accepted" in output.out
    assert "observed" not in output.out
    assert ("timed out" if failure == "timeout" else "unconfirmed") in output.err
    assert "sensitive-output" not in output.out + output.err


@pytest.mark.parametrize("workspace,unit", [
    ("home", "chatgpt-guardian-home.service"),
    ("glide.vc", "chatgpt-guardian-glide.service"),
])
@pytest.mark.parametrize("options,lines,follow", [
    ([], 50, False), (["--lines", "1"], 1, False),
    (["--lines", "500"], 500, False), (["--follow"], 50, True),
    (["--follow", "--lines", "25"], 25, True),
])
def test_logs_exact_command_and_wait(monkeypatch, workspace, unit, options, lines, follow):
    calls = []

    class Journal:
        returncode = 0

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs))
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def communicate(self, *, timeout):
            assert not follow
            assert timeout == 10
            return b"", b""

        def wait(self, *, timeout):
            assert timeout == (None if follow else 10)
            return 0

    monkeypatch.setattr(subprocess, "Popen", Journal)
    assert main(["chatgpt", "--workspace", workspace, "logs", *options]) == 0
    expected = ["journalctl", "--user", "--unit", unit, "--lines", str(lines), "--no-pager"]
    if follow:
        expected.append("--follow")
    assert calls == [(expected, {} if follow else {
        "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
    })]


@pytest.mark.parametrize("options", [
    ["--lines", "0"], ["--lines", "501"], ["--lines", "-1"],
    ["--lines", "1.5"], ["--lines", "all"], ["--lines", "1;echo injected"],
    ["--unit", "arbitrary.service"], ["--apply"], ["--fol"],
])
def test_invalid_logs_never_spawn(systemctl, options):
    with pytest.raises(SystemExit) as exc:
        main(["chatgpt", "logs", *options])
    assert exc.value.code == 2
    assert not systemctl[0]


def test_logs_reject_state_override(systemctl, capsys):
    assert main(["chatgpt", "--state", "other.sqlite", "logs"]) == 2
    assert "--state" in capsys.readouterr().err
    assert not systemctl[0]


@pytest.mark.parametrize("error,code,message", [
    (FileNotFoundError(), 127, "journalctl is unavailable"),
    (PermissionError("secret"), 126, "cannot execute journalctl"),
])
def test_logs_spawn_errors(monkeypatch, capsys, error, code, message):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "Popen", fail)
    assert main(["chatgpt", "logs"]) == code
    output = capsys.readouterr()
    assert message in output.err
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("code", [1, 4, -15])
def test_logs_propagate_exit(monkeypatch, capsys, code):
    class Journal:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = code

        def communicate(self, *, timeout):
            return b"", b""

    monkeypatch.setattr(subprocess, "Popen", Journal)
    assert main(["chatgpt", "logs"]) == (code if code >= 0 else 128 - code)
    assert "journalctl failed" in capsys.readouterr().err


def test_logs_timeout_is_bounded_without_force_kill(monkeypatch, capsys):
    class Journal:
        def __init__(self, *args, **kwargs):
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()

        def communicate(self, *, timeout):
            assert timeout == 10
            raise subprocess.TimeoutExpired("secret", timeout)

        def terminate(self):
            pass

        def wait(self, *, timeout):
            assert timeout == 1
            return -15

        def kill(self):
            pytest.fail("must not force kill journalctl")

    monkeypatch.setattr(subprocess, "Popen", Journal)
    assert main(["chatgpt", "logs"]) == 124
    output = capsys.readouterr()
    assert "timed out" in output.err
    assert "secret" not in output.out + output.err


@pytest.mark.parametrize("cleanup_timeout", [False, True])
def test_follow_interrupt_stops_only_journal_client(monkeypatch, capsys, cleanup_timeout):
    calls = []

    class Journal:
        def __init__(self, *args, **kwargs):
            pass

        def wait(self, *, timeout):
            calls.append(("wait", timeout))
            if timeout is None:
                raise KeyboardInterrupt
            if cleanup_timeout:
                raise subprocess.TimeoutExpired("secret", timeout)
            return -15

        def terminate(self):
            calls.append(("terminate",))

        def kill(self):
            pytest.fail("must not force kill journalctl")

    monkeypatch.setattr(subprocess, "Popen", Journal)
    assert main(["chatgpt", "logs", "--follow"]) == 130
    assert calls == [("wait", None), ("terminate",), ("wait", 10)]
    output = capsys.readouterr()
    assert "secret" not in output.out + output.err
