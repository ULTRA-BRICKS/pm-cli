"""``pm chatgpt audit``: parsing, wiring, output contract, exit codes, notify, bind, bench, units."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from pmlib.chatgpt_control import audit_report as rep
from pmlib.chatgpt_control import cli
from pmlib.chatgpt_control.audit_bindings import AccountBindings
from pmlib.chatgpt_control.audit_guard import RunLock
from pmlib.chatgpt_control.audit_reader import ReaderUnavailable
from pmlib.cli import build_parser
from tests.audit_collector_support import (
    BENCH,
    GLIDE_HASH,
    HOME_HASH,
    MODEL,
    SENTINEL_FINAL,
    SENTINEL_REQUEST,
    FakeClassifier,
    FakeReader,
    conversation_payload,
    listing_item,
    rig,
    simple_account,
)
from tests.audit_support import laya

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bench_digest(monkeypatch):
    """The real bench module belongs to another worker; the gate only needs its digest."""
    import types

    stub = types.ModuleType("pmlib.chatgpt_control.audit_bench")
    stub.bench_digest = lambda: BENCH
    monkeypatch.setitem(sys.modules, "pmlib.chatgpt_control.audit_bench", stub)


def parse(*argv):
    return build_parser().parse_args(["chatgpt", *argv])


def two_account_rig(tmp_path, **kw):
    return rig(
        tmp_path,
        {
            "home": simple_account(n=2),
            "glide.vc": simple_account(n=1, account_hash=GLIDE_HASH, prefix="g"),
        },
        **kw,
    )


def test_defaults_cover_every_account_with_the_contract_window():
    args = parse("audit")
    assert args.guardian_action == "audit" and args.audit_action is None
    assert (args.audit_workspace, args.days, args.turns) == (None, 14.0, 4)
    assert not (args.audit_json or args.notify or args.include_text)
    assert args.playwright_site_packages is None
    assert cli.audit_accounts(args) == ("home", "glide.vc")


@pytest.mark.parametrize("workspace", ["home", "glide.vc", "all", "global"])
def test_audit_accepts_its_own_workspace_including_all_and_its_global_alias(workspace):
    args = parse("audit", "--workspace", workspace)
    expected = ("home", "glide.vc") if workspace in ("all", "global") else (workspace,)
    assert cli.audit_accounts(args) == expected
    assert args.workspace is None  # the group-level option is untouched


def test_the_group_workspace_still_applies_when_audit_names_none():
    args = build_parser().parse_args(["chatgpt", "--workspace", "glide.vc", "audit"])
    assert cli.audit_accounts(args) == ("glide.vc",)


@pytest.mark.parametrize(
    "argv",
    [
        ("--days", "0"),
        ("--days", "-1"),
        ("--days", "61"),
        ("--days", "nan"),
        ("--days", "inf"),
        ("--days", "abc"),
        ("--turns", "0"),
        ("--turns", "51"),
        ("--turns", "x"),
        ("--workspace", "nope"),
    ],
)
def test_invalid_numbers_and_workspaces_are_usage_errors(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        parse("audit", *argv)
    assert exc.value.code == 2


def test_the_limits_are_accepted_at_their_edges():
    args = parse("audit", "--days", "60", "--turns", "50")
    assert (args.days, args.turns) == (60.0, 50)


def test_bind_requires_one_concrete_account():
    for argv in (("bind",), ("bind", "--workspace", "all")):
        with pytest.raises(SystemExit) as exc:
            parse("audit", *argv)
        assert exc.value.code == 2
    assert parse("audit", "bind", "--workspace", "home").bind_workspace == "home"


def test_json_output_is_one_bounded_document_and_exit_zero_when_ok(tmp_path, capsys):
    r = two_account_rig(tmp_path)
    code = cli.run_audit(parse("audit", "--json"), env=r.env)
    out = capsys.readouterr().out
    report = json.loads(out)  # exactly one document
    assert code == 0 and report["status"] == "ok"
    assert out.endswith("\n") and out.count("\n") == 1
    assert len(out.encode()) <= rep.MAX_JSON_BYTES
    assert SENTINEL_REQUEST not in out and SENTINEL_FINAL not in out
    assert r.store.last_report() == report
    assert "notify" not in report


def test_text_output_is_the_rendered_summary(tmp_path, capsys):
    r = two_account_rig(tmp_path)
    code = cli.run_audit(parse("audit"), env=r.env)
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert code == 0 and lines[0].startswith("pm chatgpt audit: ok (14 day window, 3 conversations")
    assert "home: ok" in lines and "glide.vc: ok" in lines
    assert "notify" not in out


def test_exit_codes_three_for_partial_and_four_for_unavailable(tmp_path, capsys):
    (tmp_path / "p").mkdir()
    partial = rig(tmp_path / "p", {"home": simple_account(n=2)})
    partial.readers["home"].raises[("read_conversation", "c0")] = ReaderUnavailable("x")
    assert cli.run_audit(parse("audit", "--workspace", "home", "--json"), env=partial.env) == 3
    capsys.readouterr()
    (tmp_path / "u").mkdir()
    down = rig(tmp_path / "u", {"home": simple_account()}, bound=())
    assert cli.run_audit(parse("audit", "--workspace", "home", "--json"), env=down.env) == 4


def test_the_include_text_flag_reaches_the_collector(tmp_path, capsys):
    reader = FakeReader(
        scopes={"recent": [listing_item("c0", 600)]},
        payloads={
            "c0": conversation_payload("c0", age=600, answer="Remaining:\n- Ship STEP-SENTINEL\n")
        },
    )
    incomplete = FakeClassifier(laya(task_finished=0.1, steps_remaining=0.9, model=MODEL))
    r = rig(tmp_path, {"home": reader}, classifier=incomplete)
    args = parse("audit", "--workspace", "home", "--include-text", "--json")
    cli.run_audit(args, env=r.env)
    out = json.loads(capsys.readouterr().out)
    assert out["accounts"]["home"]["conversations"][0]["remaining_steps"] == ["Ship STEP-SENTINEL"]


def test_a_busy_lock_prints_the_busy_document_and_exits_75_reading_nothing(tmp_path, capsys):
    r = two_account_rig(tmp_path)
    with RunLock(r.env.lock_path):
        code = cli.run_audit(parse("audit", "--json"), env=r.env)
    assert code == 75
    assert json.loads(capsys.readouterr().out) == {
        "schema": 1,
        "status": "busy",
        "reason": "lock-busy",
    }
    assert all(reader.calls == [] for reader in r.readers.values())
    assert r.store.last_report() is None


def test_notify_runs_after_the_audit_with_the_previous_report_and_is_stored(tmp_path, capsys):
    r = two_account_rig(tmp_path)
    seen = {}

    def notify(report, previous, store, *, now, send=None):
        seen.update(report=copy.deepcopy(report), previous=previous, now=now, store=store)
        return {"outcome": "sent", "kind": "changed"}

    r.store.put_report({"schema": 1, "marker": "previous"})
    code = cli.run_audit(parse("audit", "--json", "--notify"), env=r.env, notify=notify)
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert seen["previous"] == {"schema": 1, "marker": "previous"}
    assert seen["store"] is r.store and seen["now"] == r.clock.t
    assert "notify" not in seen["report"]  # the notifier judged the bare report
    assert out["notify"] == {"outcome": "sent", "kind": "changed"}
    assert r.store.last_report()["notify"] == {"outcome": "sent", "kind": "changed"}


def test_a_notify_failure_never_loses_the_report_or_changes_the_exit_code(tmp_path, capsys):
    r = two_account_rig(tmp_path, bound=("glide.vc",))  # home unbound: exit 4

    def boom(report, previous, store, *, now, send=None):
        raise RuntimeError("telegram down with a secret")

    code = cli.run_audit(parse("audit", "--json", "--notify"), env=r.env, notify=boom)
    out = capsys.readouterr().out
    assert code == 4
    report = json.loads(out)
    assert report["notify"] == {"outcome": "error"}
    assert "secret" not in out
    assert r.store.last_report()["notify"] == {"outcome": "error"}


@pytest.mark.parametrize("outcome", ["error", "busy"])
def test_a_claim_that_failed_before_sending_leaves_the_delta_eligible_next_run(
    tmp_path, capsys, outcome
):
    r = two_account_rig(tmp_path)
    seen = []

    def notify(report, previous, store, *, now, send=None):
        seen.append(previous)
        return {"outcome": outcome} if len(seen) == 1 else {"outcome": "sent"}

    r.store.put_report({"schema": 1, "marker": "previous"})
    cli.run_audit(parse("audit", "--json", "--notify"), env=r.env, notify=notify)
    cli.run_audit(parse("audit", "--json", "--notify"), env=r.env, notify=notify)
    assert seen[0] == {"schema": 1, "marker": "previous"}
    assert seen[1] is None  # nothing was sent, so the whole report is still new to the owner


def test_without_notify_the_notifier_is_not_called(tmp_path):
    r = two_account_rig(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("notify must be opt-in")

    assert cli.run_audit(parse("audit", "--json"), env=r.env, notify=forbidden) == 0


def test_no_reachable_browser_reports_every_account_unavailable_exit_four(
    tmp_path, monkeypatch, pm_home, capsys, bench_digest
):
    import types

    stub = types.ModuleType("pmlib.chatgpt_control.audit_browser")

    def make_reader(account, *, endpoint=None, playwright_site_packages=None):
        raise ReaderUnavailable("Playwright not installed")

    stub.make_reader = make_reader
    monkeypatch.setitem(sys.modules, "pmlib.chatgpt_control.audit_browser", stub)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    code = cli.run_audit(parse("audit", "--json"))
    report = json.loads(capsys.readouterr().out)
    assert code == 4 and report["status"] == "unavailable"
    for name in ("home", "glide.vc"):
        acct = report["accounts"][name]
        assert acct["status"] == "unavailable" and acct["reason"] == "playwright-not-installed"
        assert acct["coverage"]["listed_total"] == 0 and acct["conversations"] == []


def test_the_default_factory_forwards_the_playwright_path(
    tmp_path, monkeypatch, pm_home, capsys, bench_digest
):
    import types

    captured = []
    stub = types.ModuleType("pmlib.chatgpt_control.audit_browser")

    def make_reader(account, *, endpoint=None, playwright_site_packages=None):
        captured.append((account, playwright_site_packages))
        raise ReaderUnavailable("cdp unreachable")

    stub.make_reader = make_reader
    monkeypatch.setitem(sys.modules, "pmlib.chatgpt_control.audit_browser", stub)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cli.run_audit(parse("audit", "--workspace", "home", "--playwright-site-packages", "/x/sp"))
    assert captured == [("home", "/x/sp")]
    assert "cdp-unreachable" in capsys.readouterr().out


def test_bind_records_the_session_and_prints_only_a_short_prefix(tmp_path, capsys):
    r = rig(tmp_path, {"home": simple_account()}, bound=())
    code = cli.run_audit_bind(parse("audit", "bind", "--workspace", "home"), env=r.env)
    out = capsys.readouterr().out
    assert code == 0
    assert AccountBindings(r.directory / "bindings.json").bound("home") == HOME_HASH
    assert HOME_HASH[:8] in out and HOME_HASH[:9] not in out
    assert "home" in out and "bound" in out
    assert [c[0] for c in r.readers["home"].calls] == ["identity"]


def test_bind_refuses_a_session_already_bound_to_another_account(tmp_path, capsys):
    r = rig(tmp_path, {"glide.vc": simple_account(account_hash=HOME_HASH)}, bound=("home",))
    code = cli.run_audit_bind(parse("audit", "bind", "--workspace", "glide.vc"), env=r.env)
    captured = capsys.readouterr()
    assert code == 2
    assert "already bound" in captured.err
    assert AccountBindings(r.directory / "bindings.json").bound("glide.vc") is None


def test_bind_with_an_unavailable_browser_exits_four(tmp_path, capsys):
    def broken(account):
        raise ReaderUnavailable("no ChatGPT page")

    r = rig(tmp_path, {"home": simple_account()}, bound=(), factory=broken)
    code = cli.run_audit_bind(parse("audit", "bind", "--workspace", "home"), env=r.env)
    assert code == 4 and "no-chatgpt-page" in capsys.readouterr().err


def test_bench_delegates_to_the_bench_module(monkeypatch):
    import types

    calls = []
    stub = types.ModuleType("pmlib.chatgpt_control.audit_bench")

    def run(*, record, as_json, out=None):
        calls.append((record, as_json))
        return 7

    stub.run = run
    monkeypatch.setitem(sys.modules, "pmlib.chatgpt_control.audit_bench", stub)
    assert cli.run_audit_command(parse("audit", "bench", "--record", "--json")) == 7
    assert cli.run_audit_command(parse("audit", "bench")) == 7
    assert calls == [(True, True), (False, False)]


def invoke(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "pmlib.cli", *args],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
        cwd=ROOT,
        env=env,
    )


def test_the_group_level_workspace_all_still_errors_for_other_commands():
    result = invoke("chatgpt", "--workspace", "all", "status")
    assert result.returncode == 2
    assert "only for monitor, tui, accounts and audit" in result.stderr


@pytest.mark.parametrize(
    ("which", "name"),
    [("audit-service", "pm-chatgpt-audit.service"), ("audit-timer", "pm-chatgpt-audit.timer")],
)
def test_unit_prints_the_packaged_audit_templates(which, name):
    template = ROOT / "pmlib" / "chatgpt_control" / "units" / name
    if not template.exists():
        pytest.skip("unit template not created yet")
    result = invoke("chatgpt", "unit", which)
    assert result.returncode == 0, result.stderr
    assert result.stdout == template.read_text()


def test_startup_commands_import_nothing_third_party_and_not_the_browser(tmp_path):
    script = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "from pmlib.cli import main\n"
        "for argv in (['--help'], ['doctor'], ['chatgpt', 'audit', '--help'],"
        " ['chatgpt', 'audit', 'bind', '--help'], ['chatgpt', 'unit', 'audit-service'],"
        " ['chatgpt', 'unit', 'audit-timer']):\n"
        "    try:\n"
        "        main(argv)\n"
        "    except SystemExit:\n"
        "        pass\n"
        "names = ('playwright', 'yaml', 'pmlib.chatgpt_control.audit_browser',"
        " 'pmlib.chatgpt_control.audit_notify', 'pmlib.chatgpt_control.audit_bench')\n"
        "print(sorted(n for n in names if n in sys.modules))\n"
        "print(sorted(n for n, m in list(sys.modules.items()) if n not in before"
        " and any(p in (getattr(m, '__file__', None) or '')"
        " for p in ('site-packages', 'dist-packages'))))\n"
    )
    import os

    env = {**os.environ, "PM_HOME": str(tmp_path / "home")}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=ROOT,
        env=env,
    )
    lines = result.stdout.strip().splitlines()
    assert lines[-2:] == ["[]", "[]"], result.stdout + result.stderr
