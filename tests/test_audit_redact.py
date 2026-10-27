"""Credential-looking text is redacted before it reaches evidence, the model or any report."""

from __future__ import annotations

import time

import pytest

from pmlib.chatgpt_control import audit_redact as redact

# Assembled at runtime so no source line looks like a real credential to a scanner.
OPENAI = "sk-" + "A1b2C3d4" * 5
GITHUB = "ghp_" + "Z9y8X7w6" * 5
SLACK = "xoxb-" + "123456789012-" + "abcdefghijkl"
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
JWT = "eyJhbGciOiJI.eyJzdWIiOiIx" + "MjM0NTY3.SflKxwRJSMeKKF2QT4"
TELEGRAM = "123456789:" + "AAH" + "x" * 32
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY" + "-----"
PEM_END = "-----END " + "PRIVATE KEY" + "-----"
RSA_BEGIN = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"


@pytest.mark.parametrize(
    "secret",
    [OPENAI, GITHUB, SLACK, AWS, JWT, TELEGRAM, "Bearer " + "q" * 30],
)
def test_known_credential_shapes_are_removed(secret):
    text = f"use this key {secret} for the request"
    cleaned = redact.redact(text)
    assert secret not in cleaned and "[REDACTED]" in cleaned
    assert cleaned.startswith("use this key ") and cleaned.endswith(" for the request")


@pytest.mark.parametrize(
    "name,separator,value",
    [
        ("pass" + "word", ": ", "hunter2" + "hunter2"),
        ("API_" + "KEY", "=", "abcd1234" + "efgh5678"),
        ("to" + "ken", " = '", "abc-def" + "-ghi-jkl'"),
        ('"sec' + 'ret"', ': "', "s3cr3t" + '-value-long"'),
    ],
)
def test_key_value_credentials_keep_the_name_and_lose_the_value(name, separator, value):
    cleaned = redact.redact(name + separator + value)
    assert name in cleaned and "[REDACTED]" in cleaned
    assert value.strip("'\"") not in cleaned


def test_private_key_blocks_are_removed_entirely():
    block = f"{PEM_BEGIN}\nMIIBVgIBADANBg\nkqhkiG9w0BAQEFAA\n{PEM_END}"
    cleaned = redact.redact(f"before\n{block}\nafter")
    assert "MIIBVg" not in cleaned and "kqhkiG9w" not in cleaned
    assert cleaned.startswith("before") and cleaned.endswith("after")


def test_an_unterminated_private_key_block_is_removed_to_the_end():
    cleaned = redact.redact(f"intro\n{RSA_BEGIN}\nMIIEpAIBAAKCAQEA\nmore")
    assert "MIIEpAIB" not in cleaned and "more" not in cleaned and cleaned.startswith("intro")


def test_url_userinfo_is_removed_but_the_host_stays():
    userinfo = "deploy:" + "Pa55w0rd" + "Pa55"
    cleaned = redact.redact(f"clone https://{userinfo}@git.example.test/repo.git now")
    assert "Pa55w0rd" not in cleaned and "git.example.test/repo.git" in cleaned


@pytest.mark.parametrize(
    "text",
    [
        "Summarize the three options and recommend one.",
        "commit 0123456789abcdef0123456789abcdef01234567 fixed it",
        "conversation 11111111-2222-3333-4444-555555555555 is archived",
        "The token bucket algorithm refills at a fixed rate.",
        "Password policy: rotate every 90 days.",
        "set the secret santa budget to 20 dollars",
    ],
)
def test_ordinary_text_is_untouched(text):
    assert redact.redact(text) == text


def test_redaction_is_idempotent_and_total_over_empty_text():
    once = redact.redact(f"key {OPENAI} and " + "pass" + "word=correct-horse-battery")
    assert redact.redact(once) == once
    assert redact.redact("") == ""


def test_pathological_input_is_linear_time():
    started = time.monotonic()
    redact.redact("pass" + "word=" + "a" * 200000)
    redact.redact("sk-" + "-" * 200000)
    redact.redact(PEM_BEGIN + "x" * 200000)
    redact.redact("https://" + "u" * 200000 + "@")
    assert time.monotonic() - started < 2.0
