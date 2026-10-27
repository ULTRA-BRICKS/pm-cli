"""Account bindings: how the audit knows which Guardian account a browser session really is."""

from __future__ import annotations

import os
import stat

import pytest

from pmlib.chatgpt_control import audit_bindings as bindings
from pmlib.chatgpt_control import audit_files as files
from pmlib.chatgpt_control.audit_reader import ReaderIdentity

HOME = ReaderIdentity("a" * 64)
GLIDE = ReaderIdentity("b" * 64)
STRANGER = ReaderIdentity("c" * 64)


def make(tmp_path):
    return bindings.AccountBindings(tmp_path / "audit" / "bindings.json")


def test_a_recorded_binding_identifies_the_account_it_was_recorded_for(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    assert b.bound("home") == HOME.account_hash
    assert b.observed_account(HOME, "home") == "home"


def test_an_unbound_requested_account_is_never_trusted(tmp_path):
    b = make(tmp_path)
    assert b.observed_account(HOME, "home") is None
    b.record("glide.vc", GLIDE)
    assert b.observed_account(HOME, "home") is None  # home itself was never bound


def test_a_browser_logged_into_another_known_account_reports_that_account(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    b.record("glide.vc", GLIDE)
    assert b.observed_account(GLIDE, "home") == "glide.vc"  # mismatch is visible, not hidden


def test_an_unknown_identity_or_no_identity_is_no_account(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    assert b.observed_account(STRANGER, "home") is None
    assert b.observed_account(None, "home") is None


def test_one_session_cannot_be_bound_to_two_accounts(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    with pytest.raises(bindings.BindingError):
        b.record("glide.vc", HOME)
    assert b.bound("glide.vc") is None


def test_rebinding_an_account_replaces_its_hash(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    b.record("home", STRANGER)
    assert b.observed_account(HOME, "home") is None
    assert b.observed_account(STRANGER, "home") == "home"


@pytest.mark.parametrize("account", ["", "Home", "../x", "a" * 40, "all"])
def test_only_real_account_ids_can_be_bound(tmp_path, account):
    with pytest.raises(bindings.BindingError):
        make(tmp_path).record(account, HOME)


@pytest.mark.parametrize("digest", ["", "short", "G" * 64, "a" * 63])
def test_only_a_sha256_hex_identity_can_be_bound(tmp_path, digest):
    with pytest.raises(bindings.BindingError):
        make(tmp_path).record("home", ReaderIdentity(digest))


def test_the_file_is_private_and_holds_only_hashes(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    path = tmp_path / "audit" / "bindings.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.read_text().count("a" * 64) == 1


def test_a_corrupt_or_symlinked_file_is_no_binding_and_never_written(tmp_path):
    b = make(tmp_path)
    b.record("home", HOME)
    path = tmp_path / "audit" / "bindings.json"
    path.write_text("{not json")
    assert b.bound("home") is None and b.observed_account(HOME, "home") is None
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    path.symlink_to(target)
    assert b.bound("home") is None
    with pytest.raises(files.UnsafePath):
        b.record("home", HOME)
    assert target.read_text() == "{}"
