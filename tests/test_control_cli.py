"""``pm chatgpt`` routing for the control plane: JSON API client, MCP, worker, connector, units."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from pmlib.chatgpt_control import cli
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import CHAT_A, make_services

ROOT = Path(__file__).resolve().parent.parent


def ns(**kw):
    base = {"api_method": "requests.list", "params": None, "port": 8792, "rotate": False}
    base.update(kw)
    return argparse.Namespace(**base)


def envelope(capsys):
    out, err = capsys.readouterr()
    lines = out.splitlines()
    assert len(lines) == 1, out  # exactly one JSON envelope
    return json.loads(lines[0]), err


def test_api_reads_and_writes_through_the_one_dispatch(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    code = cli.run_api(
        ns(
            api_method="actions.request",
            params=json.dumps({"workspace": "home", "chat": "alpha", "action": "continue"}),
        ),
        services,
    )
    body, _ = envelope(capsys)
    assert code == 0 and body["ok"] and body["result"]["status"] == "queued"
    assert body["result"]["chat"] == CHAT_A
    assert [e["kind"] for e in Ledger(services.state_dir).read_all()] == ["request_created"]
    cli.run_api(
        ns(
            api_method="requests.get",
            params=json.dumps({"request_id": body["result"]["request_id"]}),
        ),
        services,
    )
    got, _ = envelope(capsys)
    assert got["ok"] and got["result"]["status"] == "queued"


@pytest.mark.parametrize(
    "params,code",
    [
        ("{not json", "invalid_request"),
        ('{"workspace": "home", "approved": true}', "forbidden_key"),
        ("[]", "invalid_params"),
        ('"x"', "invalid_params"),
        ("x" * 70000, "payload_too_large"),
    ],
)
def test_api_errors_are_envelopes_with_exit_one(tmp_path, capsys, params, code):
    services, _, _ = make_services(tmp_path)
    exit_code = cli.run_api(ns(api_method="sessions.get", params=params), services)
    body, _ = envelope(capsys)
    assert exit_code == 1 and body["ok"] is False and body["error"]["code"] == code
    assert Ledger(services.state_dir).read_all() == []


def test_api_unknown_method_fails_closed(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    assert cli.run_api(ns(api_method="shell.exec"), services) == 1
    assert envelope(capsys)[0]["error"]["code"] == "unknown_method"


def test_api_reads_params_from_stdin(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    stdin = io.StringIO(json.dumps({"workspace": "glide.vc"}))
    assert cli.run_api(ns(api_method="sessions.list", params="-"), services, stdin) == 0
    assert envelope(capsys)[0]["result"]["workspace"] == "glide.vc"


def test_stdin_input_is_bounded(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    huge = io.StringIO('{"a":"' + "x" * 200000 + '"}')
    assert cli.run_api(ns(api_method="sessions.list", params="-"), services, huge) == 1
    assert envelope(capsys)[0]["error"]["code"] == "payload_too_large"


def test_token_init_prints_the_path_never_the_secret(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    assert cli.run_api(ns(api_method="token-init"), services) == 0
    out = capsys.readouterr().out
    from pmlib.chatgpt_control import http_api

    assert str(http_api.token_path(services.state_dir)) in out
    assert http_api.load_token(services.state_dir) not in out
    assert cli.run_api(ns(api_method="token-init"), services) == 2  # no silent overwrite


def test_serve_without_token_or_with_bad_port_exits_two(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    assert cli.run_api(ns(api_method="serve"), services) == 2
    assert "token-init" in capsys.readouterr().err
    assert cli.run_api(ns(api_method="serve", port=0), services) == 2
    assert cli.run_api(ns(api_method="serve", port=70000), services) == 2


def test_connector_status_and_tunnel_id(tmp_path, capsys):
    services, _, _ = make_services(tmp_path)
    assert cli.run_connector(argparse.Namespace(connector_action="status"), services) == 0
    body, _ = envelope(capsys)
    entry = body["result"]["connectors"][0]
    assert entry["state"] == "not_installed" and entry["platform_verified"] is False
    bad = argparse.Namespace(connector_action="tunnel-id", tunnel_id="tunnel_abcdefgh1")
    assert cli.run_connector(bad, services) == 1  # not installed yet: refused, not faked
    assert "install" in capsys.readouterr().err


def test_unit_templates_are_printed_not_installed(capsys):
    for which in ("api", "worker"):
        assert cli.run_unit(argparse.Namespace(which=which)) == 0
        text = capsys.readouterr().out
        assert "[Service]" in text and "ExecStart=" in text and "pm chatgpt" in text
        assert "Bearer" not in text and "token=" not in text.lower()


# --- real process seam -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        ("--workspace", "home"),
        ("--workspace", "glide.vc"),
        ("--workspace", "all"),
        ("--state", "/not-the-selected-account.sqlite"),
    ],
)
@pytest.mark.parametrize(
    "command",
    [
        ("api", "sessions.list", "--params", "{}"),
        ("mcp",),
        ("approvals", "run", "--once", "--no-propose"),
        ("connector", "status"),
        ("unit", "worker"),
    ],
)
def test_process_control_commands_reject_outer_account_selectors(tmp_path, selector, command):
    result = pm(tmp_path, *selector, *command, stdin="")
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert "--workspace/--state" in result.stderr
    assert result.stdout == ""
    assert not (tmp_path / "pm-home" / "chatgpt").exists()


def pm(tmp_path, *args, stdin=None, extra_env=None, timeout=20):
    env = {
        **os.environ,
        "PM_HOME": str(tmp_path / "pm-home"),
        "HOME": str(tmp_path / "home"),
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        **(extra_env or {}),
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "pmlib.cli", "chatgpt", *args],
        capture_output=True,
        text=True,
        input=stdin,
        timeout=timeout,
        env=env,
        cwd=ROOT,
        check=False,
    )


def test_process_api_roundtrip_without_any_service(tmp_path):
    first = pm(tmp_path, "api", "requests.list")
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {"ok": True, "result": {"requests": []}}
    bad = pm(tmp_path, "api", "requests.get", "--params", '{"request_id":"nope"}')
    assert bad.returncode == 1 and json.loads(bad.stdout)["error"]["code"] == "invalid_params"
    ledger_dir = tmp_path / "pm-home" / "chatgpt"
    if ledger_dir.exists():
        assert stat.S_IMODE(ledger_dir.stat().st_mode) == 0o700


def test_process_mcp_stdio_smoke(tmp_path):
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18"},
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "chatgpt_requests", "arguments": {}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "chatgpt_request_continue",
                "arguments": {"workspace": "home", "chat": "a", "approved": True},
            },
        },
    ]
    result = pm(tmp_path, "mcp", stdin="\n".join(json.dumps(m) for m in messages) + "\n")
    assert result.returncode == 0, result.stderr
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert len(replies[1]["result"]["tools"]) >= 10
    assert replies[2]["result"]["structuredContent"] == {"requests": []}
    assert replies[3]["result"]["isError"] is True


def test_process_approvals_once_with_nothing_queued(tmp_path):
    result = pm(tmp_path, "approvals", "run", "--once", "--no-propose")
    assert result.returncode == 0, result.stderr
    line = json.loads(result.stdout.splitlines()[0])
    assert line["processed"] == [] and "poll" not in line


@pytest.mark.parametrize("interval", ["0", "29", "3601", "x"])
def test_process_approvals_rejects_bad_interval(tmp_path, interval):
    assert pm(tmp_path, "approvals", "run", "--once", "--interval", interval).returncode == 2


def test_process_unit_and_help_stay_cheap(tmp_path):
    assert "ExecStart=" in pm(tmp_path, "unit", "worker").stdout
    helped = pm(tmp_path, "api", "--help")
    assert helped.returncode == 0 and "sessions.get" in helped.stdout


def test_existing_actions_still_reject_workspace_all(tmp_path):
    assert pm(tmp_path, "--workspace", "all", "status").returncode == 2


@pytest.mark.parametrize("which", ["api", "worker"])
def test_unit_templates_exec_valid_cli_and_keep_safety_settings(which, capsys):
    import shlex

    from pmlib.cli import build_parser

    cli.run_unit(argparse.Namespace(which=which))
    text = capsys.readouterr().out
    exec_line = next(ln for ln in text.splitlines() if ln.startswith("ExecStart="))
    argv = shlex.split(exec_line.removeprefix("ExecStart="))
    assert argv[0].endswith("/.local/bin/pm")
    parsed = build_parser().parse_args(argv[1:])  # exits on any invalid flag
    assert parsed.guardian_action in ("api", "approvals")
    for setting in (
        "NoNewPrivileges=true",
        "KillSignal=SIGINT",
        "UMask=0077",
        "Restart=on-failure",
    ):
        assert setting in text
    assert "WantedBy=default.target" in text
    assert "--no-verify" not in text and "0.0.0.0" not in text
