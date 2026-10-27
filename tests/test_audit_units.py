"""The systemd templates for the audit: schedule, bounded logs and a configurable task launcher."""

from __future__ import annotations

import configparser
import os
import re
import subprocess
from importlib import resources

import pytest

UNITS = resources.files("pmlib.chatgpt_control.units")


def unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # unit keys are case-sensitive
    text = UNITS.joinpath(name).read_text(encoding="utf-8")
    parser.read_string(text)
    return parser


def exec_start_pre() -> str:
    text = UNITS.joinpath("pm-chatgpt-audit.service").read_text(encoding="utf-8")
    (line,) = [ln for ln in text.splitlines() if ln.startswith("ExecStartPre=")]
    return line


def shell_script(line: str) -> str:
    """The ``sh -c`` script of an ExecStartPre line, with systemd's ``$$`` escape resolved."""
    match = re.fullmatch(r"ExecStartPre=-?/bin/sh -c '(?P<script>.*)'", line)
    assert match, line
    return match["script"].replace("$$", "$")


def test_the_timer_runs_every_half_hour_without_catching_up():
    timer = unit("pm-chatgpt-audit.timer")
    assert timer["Timer"]["OnCalendar"] == "*:0/30"
    assert timer["Timer"]["Unit"] == "pm-chatgpt-audit.service"
    assert timer["Timer"]["Persistent"] == "false"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_the_service_is_a_non_overlapping_bounded_oneshot():
    service = unit("pm-chatgpt-audit.service")["Service"]
    assert service["Type"] == "oneshot" and service["Restart"] == "no"
    assert service["StandardOutput"] == "journal" and service["StandardError"] == "journal"
    assert service["LogRateLimitBurst"] and service["LogRateLimitIntervalSec"]
    assert int(service["TimeoutStartSec"]) < 1800  # shorter than the 30 minute period
    assert "chatgpt audit --workspace all --days 14 --json --notify" in service["ExecStart"]
    sections = [ln for ln in UNITS.joinpath("pm-chatgpt-audit.service").read_text().splitlines()]
    assert "[Install]" not in sections


def test_the_launcher_and_socket_are_configurable_through_an_environment_file():
    service = unit("pm-chatgpt-audit.service")["Service"]
    assert service["EnvironmentFile"].startswith("-%h/.config/")  # optional, per user
    assert "PM_AUDIT_TASK_LAUNCHER" in exec_start_pre()


def test_no_worktree_or_machine_specific_path_is_hardcoded():
    text = UNITS.joinpath("pm-chatgpt-audit.service").read_text(encoding="utf-8")
    assert ".worktrees" not in text and "/home/ultra" not in text and "xp/task-cli" not in text


def launcher(tmp_path, body="exit 0"):
    script = tmp_path / "task-launcher"
    log = tmp_path / "args.log"
    script.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\n{body}\n')
    script.chmod(0o755)
    return script, log


def run_pre(env: dict[str, str]) -> subprocess.CompletedProcess:
    full = {"PATH": os.environ["PATH"], **env}
    return subprocess.run(
        ["/bin/sh", "-c", shell_script(exec_start_pre())],
        env=full,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_a_configured_absolute_launcher_gets_the_warm_server_command(tmp_path):
    script, log = launcher(tmp_path)
    result = run_pre({"HOME": str(tmp_path / "nohome"), "PM_AUDIT_TASK_LAUNCHER": str(script)})
    assert result.returncode == 0
    assert log.read_text().split() == ["classify-server", "start"]


def test_without_a_setting_the_installed_user_launcher_is_used(tmp_path):
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    script, log = launcher(tmp_path)
    (home / ".local" / "bin" / "task").symlink_to(script)
    result = run_pre({"HOME": str(home)})
    assert result.returncode == 0 and log.read_text().split() == ["classify-server", "start"]


@pytest.mark.parametrize("bad", ["task", "./task", "bin/task", " task"])
def test_a_relative_launcher_is_refused_and_never_executed(tmp_path, bad):
    script, log = launcher(tmp_path)
    result = run_pre({"HOME": str(tmp_path), "PM_AUDIT_TASK_LAUNCHER": bad, "PATH": str(tmp_path)})
    assert result.returncode != 0 and "absolute" in result.stderr
    assert not log.exists()
    assert script.exists()


def test_a_failing_starter_does_not_fail_the_audit_unit():
    assert exec_start_pre().startswith("ExecStartPre=-")  # the leading dash ignores the failure
