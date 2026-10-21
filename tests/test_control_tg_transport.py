"""The only route to an approval is a verified ``tg-ctl ask`` exit 0; everything else is a refusal.

A fake ``tg-ctl`` script stands in for the real one: nothing here posts to Telegram.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from pmlib.chatgpt_control import tg_transport
from pmlib.chatgpt_control.tg_transport import (
    OwnerVerdict,
    ask_owner,
    build_question,
    discover_owner,
)

QUESTION = "pm approval abcd1234: continue"


@pytest.fixture
def fake(tmp_path):
    """Build a fake tg-ctl: behaviour comes from env vars so one script covers every case."""
    script = tmp_path / "tg-ctl"
    calls = tmp_path / "calls.jsonl"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['capabilities']:\n"
        "    sys.stdout.write(os.environ.get('FAKE_CAPS', "
        '\'{"schema_version":1,"capabilities":["ask-owner-only-v1"]}\'))\n'
        "    sys.exit(int(os.environ.get('FAKE_CAPS_CODE', '0')))\n"
        "time.sleep(float(os.environ.get('FAKE_SLEEP', '0')))\n"
        "sys.stdout.write(os.environ.get('FAKE_OUT', ''))\n"
        "sys.stderr.write(os.environ.get('FAKE_ERR', ''))\n"
        "sys.exit(int(os.environ.get('FAKE_CODE', '0')))\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)

    def run(code=0, out="", err="", question=QUESTION, timeout=30, **extra):
        env = {**os.environ, "FAKE_CODE": str(code), "FAKE_OUT": out, "FAKE_ERR": err}
        env.update({k: str(v) for k, v in extra.items()})
        return ask_owner(question, timeout, argv_prefix=(str(script),), env=env)

    run.calls = lambda: [json.loads(line) for line in calls.read_text().splitlines()]
    run.asks = lambda: [c for c in run.calls() if c[0] == "ask"]
    run.script = script
    return run


def test_exact_argv_probe_then_owner_only_question_then_timeout(fake):
    verdict = fake(0, out="approved\n", timeout=777)
    assert verdict.outcome == "approved"
    assert fake.calls() == [
        ["capabilities", "--json"],
        ["ask", "--owner-only", QUESTION, "--timeout", "777"],
    ]


@pytest.mark.parametrize("out", ["approved\n", "approved: go ahead\n", "approved"])
def test_only_exit0_with_approved_stdout_is_approval(fake, out):
    assert fake(0, out=out).outcome == "approved"


@pytest.mark.parametrize(
    "out", ["", "ok\n", "Approved\n", "approvedX\n", "\napproved\n", "no\napproved\n"]
)
def test_exit0_without_the_approved_line_is_not_approval(fake, out):
    verdict = fake(0, out=out)
    assert verdict.outcome == "uncertain" and verdict.exit_code == 0


@pytest.mark.parametrize(
    "err,outcome",
    [
        (
            "denied: daemon unreachable — is it running? try `tg-ctl start`\n",
            "delivery_unavailable",
        ),
        ("denied: no bot credentials configured\n", "delivery_unavailable"),
        ("denied: timeout after 900s\n", "timed_out"),
        ("denied: rejected in Telegram\n", "rejected"),
        ("denied: rejected in Telegram: not now\n", "rejected"),
        ("denied: rejected or not answered\n", "rejected"),
        ("denied: malformed daemon response\n", "uncertain"),
        ("denied: empty question\n", "uncertain"),
        ("something else entirely\n", "uncertain"),
        ("", "uncertain"),
    ],
)
def test_exit1_is_classified_never_approved(fake, err, outcome):
    verdict = fake(1, err=err)
    assert verdict.outcome == outcome and verdict.exit_code == 1


@pytest.mark.parametrize("code", [2, 3, 69, 126, 127, 130, 137, 143, 255])
def test_every_other_exit_code_is_uncertain(fake, code):
    assert fake(code, out="approved\n").outcome == "uncertain"


def test_stdout_approved_with_nonzero_exit_is_not_approval(fake):
    assert fake(1, out="approved\n", err="denied: rejected in Telegram").outcome == "rejected"


def test_missing_executable_means_no_card_was_posted(tmp_path):
    verdict = ask_owner(QUESTION, 30, argv_prefix=(str(tmp_path / "absent"),))
    assert verdict.outcome == "delivery_unavailable" and verdict.exit_code is None


def test_wait_timeout_terminates_child_and_is_uncertain(fake, monkeypatch):
    monkeypatch.setattr(tg_transport, "GRACE_SECONDS", 1)
    started = time.monotonic()
    verdict = fake(0, out="approved\n", timeout=1, FAKE_SLEEP=30)
    assert verdict.outcome == "uncertain" and "timed out" in verdict.detail
    assert time.monotonic() - started < 20


def test_interrupt_terminates_child_and_propagates(monkeypatch):
    class Child:
        terminated = False
        returncode = None

        def communicate(self, timeout=None):
            if not self.terminated:
                raise KeyboardInterrupt
            return "", ""

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            self.returncode = 143
            return 143

        def poll(self):
            return self.returncode

    child = Child()
    with pytest.raises(KeyboardInterrupt):
        ask_owner(
            QUESTION,
            30,
            argv_prefix=("tg-ctl",),
            run=Probe(caps("ask-owner-only-v1")),
            popen=lambda *a, **k: child,
        )
    assert child.terminated


def test_detail_never_carries_unbounded_child_output(fake):
    verdict = fake(1, err="denied: rejected in Telegram: " + "x" * 5000)
    assert len(verdict.detail) <= 300


def test_stdin_is_closed_so_the_hook_client_mode_cannot_trigger(fake):
    # the question is never a bare flag, so tg-ctl cannot take it for an option
    fake(0, out="approved\n")
    assert not fake.asks()[0][2].startswith("-")


class Probe:
    """An injected runner standing in for subprocess.run on the capability probe only."""

    def __init__(self, stdout="", code=0, exc=None):
        self.stdout, self.code, self.exc, self.calls = stdout, code, exc, []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def caps(*names, version=1):
    return json.dumps({"schema_version": version, "capabilities": list(names)})


class NoAsk:
    """A popen that fails the test if an ask is ever started."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("tg-ctl ask must not be started")


def probe_ask(probe):
    return ask_owner(QUESTION, 30, argv_prefix=("tg-ctl",), run=probe, popen=NoAsk())


def test_capability_marker_present_allows_the_probe():
    probe = Probe(caps("other-thing", "ask-owner-only-v1"))
    assert tg_transport.owner_only_supported(("tg-ctl",), run=probe) is True
    argv, kwargs = probe.calls[0]
    assert argv == ["tg-ctl", "capabilities", "--json"]
    assert kwargs["timeout"] == tg_transport.CAPABILITY_TIMEOUT
    assert kwargs["stdin"] is subprocess.DEVNULL


@pytest.mark.parametrize(
    "probe",
    [
        Probe(caps("other-thing")),
        Probe(caps()),
        Probe(caps("ask-owner-only-v1", version=2)),
        Probe(caps("ask-owner-only-v1", version=True)),
        Probe(caps("ask-owner-only-v1"), code=2),
        Probe("", code=2),
        Probe(""),
        Probe("not json"),
        Probe("[]"),
        Probe('{"schema_version":1}'),
        Probe('{"schema_version":1,"capabilities":"ask-owner-only-v1"}'),
        Probe('{"schema_version":1,"capabilities":[["ask-owner-only-v1"]]}'),
        Probe('{"schema_version":1,"capabilities":["ask-owner-only-v2"]}'),
        Probe(caps("ask-owner-only-v1") + " " * tg_transport.CAPABILITY_MAX_BYTES),
        Probe(exc=subprocess.TimeoutExpired("tg-ctl", 5)),
        Probe(exc=FileNotFoundError("tg-ctl")),
        Probe(exc=PermissionError("tg-ctl")),
    ],
)
def test_missing_or_invalid_capability_asks_nothing(probe):
    assert tg_transport.owner_only_supported(("tg-ctl",), run=probe) is False
    verdict = probe_ask(probe)
    assert verdict.outcome == "delivery_unavailable" and verdict.exit_code is None
    assert len(probe.calls) == 2  # one probe each; the ask was never started


def test_old_cli_without_capabilities_command_never_falls_back_to_a_plain_ask(fake):
    verdict = fake(0, out="approved\n", FAKE_CAPS="", FAKE_CAPS_CODE=1)
    assert verdict.outcome == "delivery_unavailable"
    assert fake.asks() == []


def test_capability_without_the_marker_never_asks_even_if_ask_would_approve(fake):
    caps_json = caps("something-else")
    verdict = fake(0, out="approved\n", FAKE_CAPS=caps_json)
    assert verdict.outcome == "delivery_unavailable"
    assert fake.asks() == []


def test_probe_runs_before_every_ask(fake):
    fake(0, out="approved\n")
    fake(1, err="denied: rejected in Telegram\n")
    assert [c[0] for c in fake.calls()] == ["capabilities", "ask", "capabilities", "ask"]


def test_owner_only_flag_is_always_present_and_never_part_of_the_question(fake):
    fake(0, out="approved\n")
    ask = fake.asks()[0]
    assert ask[:2] == ["ask", "--owner-only"] and QUESTION in ask and "--owner-only" not in QUESTION
    assert ask.count("--owner-only") == 1


def test_no_external_approved_flag_or_other_sender_escape_in_argv(fake):
    fake(0, out="approved\n")
    joined = " ".join(fake.asks()[0])
    assert "approved" not in joined.replace(QUESTION, "") and "sender" not in joined


@pytest.mark.parametrize("question", ["--owner-only", "-x", "--help"])
def test_dash_question_is_refused_before_any_probe_or_ask(question):
    probe = Probe(caps("ask-owner-only-v1"))
    with pytest.raises(ValueError):
        ask_owner(question, 30, run=probe, popen=NoAsk())
    assert probe.calls == []


def common_args(**kw):
    args = {
        "request_id": "11111111-2222-4333-8444-555555555555",
        "account": "glide.vc",
        "chat": "https://chatgpt.com/c/abc-123",
        "chat_title": "Canvas work",
        "turn": "turn-77",
        "action": "continue",
        "plan_digest": "ab" * 32,
        "expires_at": 1792254000 + 900,
        "reason": "",
    }
    args.update(kw)
    return args


def test_question_shows_every_binding_field():
    text = build_question(**common_args(reason="long answer, task may be unfinished"))
    for needle in (
        "11111111",
        "glide.vc",
        "Canvas work",
        "abc-123",
        "turn-77",
        "continue",
        "sha256:" + "ab" * 8,
        "\u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u0434\u043e",
        "task may be unfinished",
    ):
        assert needle in text, needle
    assert "ab" * 32 not in text  # a short digest, not the whole value


def test_question_is_bounded_and_control_free():
    text = build_question(
        **common_args(chat_title="\x1b[31mevil\u202e\ntitle" + "z" * 500, reason="r\n" * 400)
    )
    assert len(text) <= 1800
    assert "\x1b" not in text and "\u202e" not in text
    assert not text.startswith("-") and "--timeout" not in text


def test_question_has_no_secret_shaped_fields(monkeypatch):
    monkeypatch.setenv("TG_BOT_TOKEN", "123:SECRETTOKEN")
    assert "SECRET" not in build_question(**common_args())


def test_connector_question_names_scope_not_chat():
    text = build_question(
        **common_args(
            account=None,
            chat=None,
            chat_title="",
            turn=None,
            action="connector-install",
        )
    )
    assert "connector-install" in text and "\u0427\u0430\u0442:" not in text
    assert "\u0410\u043a\u043a\u0430\u0443\u043d\u0442:" not in text


def write_env(directory, body):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".env").write_text(body)
    return directory


def test_owner_discovery_reads_only_the_chat_id_and_never_returns_it(tmp_path):
    cfg = write_env(tmp_path / "a", "TG_BOT_TOKEN=999:TOPSECRET\nTG_CHAT_ID=424242\nOTHER=1\n")
    ref = discover_owner(cfg)
    assert ref and "424242" not in ref and "TOPSECRET" not in ref
    assert ref == discover_owner(cfg)
    assert discover_owner(write_env(tmp_path / "b", "TG_CHAT_ID=424243\n")) != ref


def test_owner_discovery_missing_or_malformed_is_none(tmp_path):
    assert discover_owner(tmp_path / "nothing") is None
    assert discover_owner(write_env(tmp_path / "c", "TG_BOT_TOKEN=1:a\n")) is None


def test_owner_discovery_rejects_nonnumeric_ids(tmp_path):
    assert discover_owner(write_env(tmp_path / "d", "TG_CHAT_ID=not-a-number\n")) is None


def test_verdict_is_a_plain_value():
    assert OwnerVerdict("approved", 0, "").outcome == "approved"


RU_CONTINUE = "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c?"
RU_NOTHING_HAPPENS = "\u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043f\u0440\u043e\u0438\u0437\u043e\u0439\u0434\u0451\u0442"


def test_continue_question_is_russian_named_and_explicit():
    text = build_question(**common_args())
    assert RU_CONTINUE in text
    assert "glide.vc" in text and "Canvas work" in text and "turn-77" in text
    assert "go" in text  # the exact message that will be sent
    assert RU_NOTHING_HAPPENS in text  # decline/cancel does nothing
    assert "\u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u0434\u043e" in text  # expiry
    assert "--help" not in text and not text.startswith("-")


def test_every_action_has_its_own_russian_question_and_effect():
    from pmlib.chatgpt_control.schema import ACTIONS

    seen = set()
    for action in (*ACTIONS, "connector-install"):
        text = build_question(**common_args(action=action))
        assert RU_NOTHING_HAPPENS in text and any(ord(c) > 0x400 for c in text)
        seen.add(text.split("\n\n", 1)[1].split("\n", 1)[0])
    assert len(seen) == len(ACTIONS) + 1


def test_poller_note_is_not_repeated_in_english():
    from pmlib.chatgpt_control.policy import PROPOSAL_NOTE

    assert PROPOSAL_NOTE not in build_question(**common_args(reason=PROPOSAL_NOTE))


def test_source_has_no_cyrillic_literals():
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent / "pmlib"
    for path in root.rglob("*.py"):
        assert re.search("[\u0400-\u04ff]", path.read_text(encoding="utf-8")) is None, path


@pytest.mark.parametrize("value", ["-100123456", "-1", "0", "00", "-0", "+5", "1.5", "12abc"])
def test_owner_discovery_rejects_nonpositive_group_and_odd_ids(tmp_path, value):
    assert discover_owner(write_env(tmp_path / "x", f"TG_CHAT_ID={value}\n")) is None


def test_owner_discovery_accepts_a_positive_private_id_quoted_or_not(tmp_path):
    bare = discover_owner(write_env(tmp_path / "p", "TG_CHAT_ID=5153477378\n"))
    quoted = discover_owner(write_env(tmp_path / "q", 'TG_CHAT_ID="5153477378"\n'))
    assert bare and bare == quoted


def _script(tmp_path, body):
    path = tmp_path / "tg-ctl"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return (str(path),)


def test_default_probe_stops_reading_an_endless_stdout(tmp_path):
    prefix = _script(
        tmp_path,
        "import sys\nchunk = b'x' * 65536\nwhile True:\n    sys.stdout.buffer.write(chunk)\n",
    )
    started = time.monotonic()
    assert tg_transport.owner_only_supported(prefix) is False
    assert time.monotonic() - started < tg_transport.CAPABILITY_TIMEOUT


def test_default_probe_accepts_a_real_capabilities_answer(tmp_path):
    prefix = _script(
        tmp_path, "import sys\nsys.stdout.write(" + repr(caps("ask-owner-only-v1")) + ")\n"
    )
    assert tg_transport.owner_only_supported(prefix) is True
