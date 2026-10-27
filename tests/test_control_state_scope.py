"""Continuation evidence and dispatch must use the same configured Guardian state."""

from __future__ import annotations

import json
import os

import pytest

from pmlib.chatgpt_control import api
from pmlib.chatgpt_control.guardian_client import GuardianConfig, GuardianUnavailable
from tests.control_support import CHAT_A, NOW
from tests.test_control_worker import Harness


@pytest.fixture
def configured_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    state = home / ".local/state/agent-tools/chatgpt-guardian/home/state.sqlite"
    state.parent.mkdir(parents=True)
    state.touch()
    config = home / ".config/agent-tools/chatgpt-guardian/home.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"workspace": "home", "state": str(state)}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CHATGPT_GUARDIAN_STATE", raising=False)
    return home, state


def test_mismatched_state_override_refuses_admission_without_queuing(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    override = str(configured_state.with_name("other.sqlite"))
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", override)
    harness = Harness(tmp_path / "request")

    response = api.dispatch(
        harness.services,
        "actions.request",
        {"workspace": "home", "chat": CHAT_A, "action": "continue"},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "refused"
    assert harness.ledger.read_all() == []
    assert os.environ["CHATGPT_GUARDIAN_STATE"] == override


def test_state_override_changed_during_approval_prevents_dispatch(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    harness = Harness(tmp_path / "dispatch")
    request_id = harness.request(workspace="home", chat=CHAT_A, action="continue")
    override = str(configured_state.with_name("other.sqlite"))
    harness.on_ask = lambda: monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", override)

    harness.worker.process_next()

    assert harness.status(request_id) == "invalidated"
    assert harness.guardian_posts == []
    assert "dispatch_started" not in harness.kinds()
    assert os.environ["CHATGPT_GUARDIAN_STATE"] == override


def test_matching_state_override_preserves_admission_and_dispatch(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(configured_state))
    harness = Harness(tmp_path / "matching")
    request_id = harness.request(workspace="home", chat=CHAT_A, action="continue")

    harness.worker.process_next()

    assert harness.status(request_id) == "sent"
    assert len(harness.guardian_posts) == 1
    assert os.environ["CHATGPT_GUARDIAN_STATE"] == str(configured_state)


def test_continuation_token_cannot_bypass_state_override_check(
    configured_home, monkeypatch
):
    home, configured_state = configured_home
    token = "private-test-token-" + "x" * 40
    token_path = configured_state.parent / "control.token"
    token_path.write_text(token)
    token_path.chmod(0o600)
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(configured_state.with_name("other.sqlite")))

    with pytest.raises(GuardianUnavailable) as raised:
        GuardianConfig(home).read_token("home")

    assert "state" in str(raised.value)
    assert token not in str(raised.value)


def test_database_alias_with_another_monitor_path_refuses_admission(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    alias = configured_state.with_name("other-name.sqlite")
    alias.symlink_to(configured_state)
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(alias))
    harness = Harness(tmp_path / "alias-request")

    response = api.dispatch(
        harness.services,
        "actions.request",
        {"workspace": "home", "chat": CHAT_A, "action": "continue"},
    )

    assert response["ok"] is False
    assert response["error"]["code"] == "refused"
    assert harness.ledger.read_all() == []
    assert os.environ["CHATGPT_GUARDIAN_STATE"] == str(alias)


def test_database_alias_selected_during_approval_prevents_dispatch(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    alias = configured_state.with_name("other-name.sqlite")
    alias.symlink_to(configured_state)
    harness = Harness(tmp_path / "alias-dispatch")
    request_id = harness.request(workspace="home", chat=CHAT_A, action="continue")
    harness.on_ask = lambda: monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(alias))

    harness.worker.process_next()

    assert harness.status(request_id) == "invalidated"
    assert harness.guardian_posts == []
    assert "dispatch_started" not in harness.kinds()


def test_parent_directory_alias_preserves_both_state_and_monitor_selection(
    configured_home, tmp_path, monkeypatch
):
    _, configured_state = configured_home
    alias_directory = tmp_path / "same-deployment"
    alias_directory.symlink_to(configured_state.parent, target_is_directory=True)
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(alias_directory / configured_state.name))
    harness = Harness(tmp_path / "same-deployment-request")
    request_id = harness.request(workspace="home", chat=CHAT_A, action="continue")

    harness.worker.process_next()

    assert harness.status(request_id) == "sent"
    assert len(harness.guardian_posts) == 1


def test_native_guardian_monitor_sidecars_cannot_bypass_state_scope(tmp_path, monkeypatch):
    monitor = pytest.importorskip("agenttools_chatgpt.monitor")
    core = pytest.importorskip("agenttools_chatgpt.core")
    home = tmp_path / "native-home"
    state = home / "guardian/state.sqlite"
    core.Store(state, account="home")
    alias = state.with_name("other-name.sqlite")
    alias.symlink_to(state)
    configured_monitor = monitor.MonitorStore(state, "home")
    alias_monitor = monitor.MonitorStore(alias, "home")
    assert configured_monitor.path != alias_monitor.path
    configured_monitor.record(
        monitor.project_snapshot(
            "home", {}, [], [], None, {"connection": "unavailable"}, NOW
        )
    )
    alias_monitor.record(
        monitor.project_snapshot("home", {}, [], [], None, {"connection": "connected"}, NOW)
    )
    assert configured_monitor.read(now=NOW)["stale"] is True
    assert alias_monitor.read(now=NOW)["stale"] is False
    config = home / ".config/agent-tools/chatgpt-guardian/home.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"workspace": "home", "state": str(state)}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CHATGPT_GUARDIAN_STATE", str(alias))

    with pytest.raises(GuardianUnavailable):
        GuardianConfig(home).check_state_override("home")
