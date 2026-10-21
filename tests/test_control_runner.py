"""The ``approvals run`` loop: one worker at a time, restart-safe, proposals plus processing."""

from __future__ import annotations

import io
import json
import stat
import sys

import pytest

from pmlib.chatgpt_control import api, runner
from pmlib.chatgpt_control.guardian_client import ContinuationStatus, ContinueReply
from pmlib.chatgpt_control.ledger import Ledger
from tests.control_support import CHAT_A, combined, make_services, row


class StubGuardian:
    def __init__(self):
        self.posts = []

    def request_continue(self, account, body):
        self.posts.append((account, body))
        return ContinueReply("queued", "")

    def continuation(self, account, request_id):
        return ContinuationStatus("sent", "")


def fake_tg(tmp_path, code=0, out="approved\n", err=""):
    script = tmp_path / "tg-ctl"
    calls = tmp_path / "tg-calls"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "if sys.argv[1:] == ['capabilities', '--json']:\n"
        '    sys.stdout.write(\'{"schema_version": 1, "capabilities": ["ask-owner-only-v1"]}\')\n'
        "    sys.exit(0)\n"
        f"open({str(calls)!r}, 'a').write(repr(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.stderr.write({err!r})\n"
        f"sys.exit({code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, calls


@pytest.fixture
def world(tmp_path):
    services, telemetry, clock = make_services(
        tmp_path, combined({"home": [row()], "glide.vc": []})
    )
    guardian = StubGuardian()
    return tmp_path, services, telemetry, clock, guardian


def build(world, script, **kw):
    _, services, _, clock, guardian = world
    worker = runner.build_worker(
        services,
        tg_argv=(str(script),),
        guardian=guardian,
        owner_ref=lambda: "ownerref",
        sleep=lambda s: clock.advance(1),
        **kw,
    )
    return worker


def test_once_proposes_asks_and_dispatches_in_one_pass(world):
    tmp_path, services, _, _, guardian = world
    script, calls = fake_tg(tmp_path)
    out = io.StringIO()
    code = runner.run_loop(services, build(world, script), once=True, interval=60, out=out)
    assert code == 0
    (line,) = out.getvalue().splitlines()
    report = json.loads(line)
    assert len(report["poll"]["proposed"]) == 1
    assert [r["status"] for r in report["processed"]] == ["sent"]
    assert len(guardian.posts) == 1
    argv = eval(calls.read_text().splitlines()[0])
    assert argv[:2] == ["ask", "--owner-only"] and argv[3:] == ["--timeout", "900"]
    assert "home" in argv[2]


def test_restart_replays_nothing(world):
    tmp_path, services, _, _, guardian = world
    script, calls = fake_tg(tmp_path)
    runner.run_loop(services, build(world, script), once=True, interval=60, out=io.StringIO())
    again = io.StringIO()
    runner.run_loop(services, build(world, script), once=True, interval=60, out=again)
    report = json.loads(again.getvalue())
    assert report["poll"]["proposed"] == [] and report["processed"] == []
    assert len(guardian.posts) == 1 and len(calls.read_text().splitlines()) == 1


def test_rejection_is_final_even_across_restarts(world):
    tmp_path, services, _, _, guardian = world
    script, calls = fake_tg(tmp_path, code=1, out="", err="denied: rejected in Telegram\n")
    for _ in range(3):
        runner.run_loop(services, build(world, script), once=True, interval=60, out=io.StringIO())
    assert guardian.posts == [] and len(calls.read_text().splitlines()) == 1
    (state,) = services.snapshot().requests.values()
    assert state.status == "rejected"


def test_no_propose_still_processes_queued_api_requests(world):
    tmp_path, services, _, _, guardian = world
    script, _ = fake_tg(tmp_path)
    receipt = api.dispatch(
        services, "actions.request", {"workspace": "home", "action": "continue", "chat": CHAT_A}
    )["result"]
    out = io.StringIO()
    runner.run_loop(services, build(world, script), once=True, interval=60, propose=False, out=out)
    report = json.loads(out.getvalue())
    assert "poll" not in report and report["processed"][0]["request_id"] == receipt["request_id"]
    assert len(guardian.posts) == 1


def test_max_requests_bounds_one_tick(world):
    tmp_path, services, telemetry, _, _ = world
    script, _ = fake_tg(tmp_path)
    rows = [row(f"https://chatgpt.com/c/chat{i}") for i in range(3)]
    telemetry.data = combined({"home": rows, "glide.vc": []})
    for r in rows:
        api.dispatch(
            services, "actions.request", {"workspace": "home", "action": "pause", "chat": r["chat"]}
        )
    out = io.StringIO()
    runner.run_loop(
        services,
        build(
            world,
            script,
            run_control=lambda *a: type("R", (), {"outcome": "success", "message": ""})(),
        ),
        once=True,
        interval=60,
        propose=False,
        max_requests=2,
        out=out,
    )
    assert len(json.loads(out.getvalue())["processed"]) == 2


def test_second_worker_is_refused_while_the_lock_is_held(world):
    tmp_path, services, _, _, _ = world
    script, _ = fake_tg(tmp_path)
    with runner.WorkerLock(services.state_dir):
        err = io.StringIO()
        code = runner.run_loop(
            services, build(world, script), once=True, interval=60, out=io.StringIO(), err=err
        )
    assert code == runner.EXIT_BUSY and "another approvals worker" in err.getvalue()
    with runner.WorkerLock(services.state_dir):
        pass  # released afterwards


def test_loop_ticks_until_interrupted_and_returns_130(world):
    tmp_path, services, _, _, _ = world
    script, _ = fake_tg(tmp_path)
    ticks = []

    def sleep(seconds):
        ticks.append(seconds)
        if len(ticks) == 3:
            raise KeyboardInterrupt

    out = io.StringIO()
    code = runner.run_loop(
        services, build(world, script), once=False, interval=45, out=out, sleep=sleep
    )
    assert code == 130 and ticks == [45, 45, 45]
    assert len(out.getvalue().splitlines()) == 3


def test_orphans_from_a_crashed_worker_are_resolved_at_startup(world):
    tmp_path, services, _, clock, guardian = world
    script, calls = fake_tg(tmp_path)
    rid = api.dispatch(
        services, "actions.request", {"workspace": "home", "action": "continue", "chat": CHAT_A}
    )["result"]["request_id"]
    Ledger(services.state_dir).append(
        {"kind": "ask_started", "at": clock.now, "request_id": rid, "timeout_s": 900}
    )
    runner.run_loop(
        services, build(world, script), once=True, interval=60, propose=False, out=io.StringIO()
    )
    assert services.snapshot().requests[rid].status == "asked_post_uncertain"
    assert not calls.exists() and guardian.posts == []


def test_telemetry_outage_does_not_stop_the_loop(world):
    tmp_path, services, _, _, _ = world
    script, _ = fake_tg(tmp_path)

    def boom(_accounts):
        raise RuntimeError("down")

    services.fetch = boom
    out = io.StringIO()
    assert runner.run_loop(services, build(world, script), once=True, interval=60, out=out) == 0
    assert json.loads(out.getvalue())["poll"]["error"] == "telemetry unavailable"


def test_interval_bounds():
    assert runner.check_interval(60) == 60
    for bad in (0, 29, 3601, True, "60"):
        with pytest.raises(ValueError):
            runner.check_interval(bad)
