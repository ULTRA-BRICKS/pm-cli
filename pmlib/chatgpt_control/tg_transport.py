"""Telegram transport: the single route from a request to an owner approval.

Accessed via: ``worker`` only. The API, HTTP and MCP front ends can queue requests but have no
path to this module.

The contract with ``tg-ctl`` (private-owner approval protocol v1). Before ANY ask the trusted
tg-ctl is probed with ``capabilities --json`` and must exit 0 with a bounded JSON object
``{"schema_version": 1, "capabilities": [..., "ask-owner-only-v1"]}``; otherwise the ask is
never started and there is no legacy fallback (delivery_unavailable). The ask is exactly
``tg-ctl ask --owner-only QUESTION --timeout SEC`` (tg-cli ``runGenericAsk``): it posts an
Approve/Reject card to the private owner chat, exits 0 and prints ``approved`` (optionally
``approved: <detail>``) on stdout ONLY for an Approve tap from the owner; every other outcome
exits 1 with a ``denied: ...`` line on stderr. This module accepts an approval only for exit 0
AND a first stdout line of ``approved`` / ``approved: ...``; anything else, including exit 0
with other output, a signal exit or a timeout of our own wait, is a non-approval. QUESTION never
begins with ``-``, so tg-ctl cannot mistake it for an option. No approved/sender flag is passed.

Nothing here knows or stores a Telegram token or user id. Owner discovery reads only
``TG_CHAT_ID`` from tg-cli's own config and exposes a one-way reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .policy import PROPOSAL_NOTE
from .schema import clean_text

GRACE_SECONDS = 15
DETAIL_LIMIT = 300
QUESTION_LIMIT = 1800
MAX_TIMEOUT = 900
_APPROVED = re.compile(r"approved(?:: .*)?")
_CHAT_ID = re.compile(r"TG_CHAT_ID\s*=\s*['\"]?(\d{1,20})['\"]?\s*")
CAPABILITY = "ask-owner-only-v1"
CAPABILITY_TIMEOUT = 10
CAPABILITY_MAX_BYTES = 65536

# Owner-facing text is Russian; source stays ASCII (\u escapes) under the English-only repo rule.
_ASK = {
    "continue": ("\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c?"),
    "watch": (
        "\u0412\u0437\u044f\u0442\u044c \u044d\u0442\u043e\u0442 \u0447\u0430\u0442 \u043f\u043e\u0434 \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u0435 Guardian?"
    ),
    "unwatch": (
        "\u0421\u043d\u044f\u0442\u044c \u044d\u0442\u043e\u0442 \u0447\u0430\u0442 \u0441 \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u044f Guardian?"
    ),
    "pause": (
        "\u041f\u0440\u0438\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 Guardian \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0447\u0430\u0442\u0430?"
    ),
    "resume": (
        "\u0412\u043e\u0437\u043e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 Guardian \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0447\u0430\u0442\u0430?"
    ),
    "service-start": (
        "\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u0441\u043b\u0443\u0436\u0431\u0443 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f Guardian \u044d\u0442\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430?"
    ),
    "service-stop": (
        "\u041e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u0441\u043b\u0443\u0436\u0431\u0443 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f Guardian \u044d\u0442\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430?"
    ),
    "connector-install": (
        "\u0423\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u044b\u0439 \u043a\u043e\u043d\u043d\u0435\u043a\u0442\u043e\u0440 guardian \u0434\u043b\u044f ChatGPT?"
    ),
}

_EFFECT = {
    "continue": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: Guardian \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442 \u0432 \u044d\u0442\u043e\u0442 \u0447\u0430\u0442, \u043a \u044d\u0442\u043e\u043c\u0443 \u0445\u043e\u0434\u0443, \u043e\u0434\u043d\u043e \u0444\u0438\u043a\u0441\u0438\u0440\u043e\u0432\u0430\u043d\u043d\u043e\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 go. \u0411\u043e\u043b\u044c\u0448\u0435 \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u044f\u0435\u0442\u0441\u044f."
    ),
    "watch": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u0442\u043e\u043b\u044c\u043a\u043e \u0437\u0430\u043f\u0438\u0441\u044c \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u044f \u0437\u0430 \u044d\u0442\u0438\u043c \u0447\u0430\u0442\u043e\u043c; \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0430 \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u044f \u043d\u0435 \u0432\u043a\u043b\u044e\u0447\u0430\u0435\u0442\u0441\u044f."
    ),
    "unwatch": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u043d\u0430\u0431\u043b\u044e\u0434\u0435\u043d\u0438\u0435 \u0437\u0430 \u044d\u0442\u0438\u043c \u0447\u0430\u0442\u043e\u043c \u0431\u0443\u0434\u0435\u0442 \u0441\u043d\u044f\u0442\u043e."
    ),
    "pause": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 Guardian \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0447\u0430\u0442\u0430 \u0431\u0443\u0434\u0435\u0442 \u043f\u0440\u0438\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u043e."
    ),
    "resume": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u0432\u043e\u0441\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0438\u0435 \u0434\u043b\u044f \u044d\u0442\u043e\u0433\u043e \u0447\u0430\u0442\u0430 \u0432\u043e\u0437\u043e\u0431\u043d\u043e\u0432\u0438\u0442\u0441\u044f; \u043e\u0431\u0449\u0438\u0435 \u0431\u043b\u043e\u043a\u0438\u0440\u043e\u0432\u043a\u0438, \u043a\u0432\u043e\u0442\u0430 \u0438 \u043d\u0435\u043e\u043f\u0440\u0435\u0434\u0435\u043b\u0451\u043d\u043d\u044b\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043a\u0438 \u043e\u0441\u0442\u0430\u044e\u0442\u0441\u044f \u0432 \u0441\u0438\u043b\u0435."
    ),
    "service-start": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u0431\u0443\u0434\u0435\u0442 \u0437\u0430\u043f\u0443\u0449\u0435\u043d\u0430 \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0430\u044f \u0441\u043b\u0443\u0436\u0431\u0430 Guardian \u044d\u0442\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430."
    ),
    "service-stop": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u043f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u044c\u0441\u043a\u0430\u044f \u0441\u043b\u0443\u0436\u0431\u0430 Guardian \u044d\u0442\u043e\u0433\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442\u0430 \u0431\u0443\u0434\u0435\u0442 \u043e\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d\u0430."
    ),
    "connector-install": (
        "\u0415\u0441\u043b\u0438 \u043e\u0434\u043e\u0431\u0440\u0438\u0442\u044c: \u0432 \u043a\u0430\u0442\u0430\u043b\u043e\u0433 \u0441\u043e\u0441\u0442\u043e\u044f\u043d\u0438\u044f pm \u0431\u0443\u0434\u0443\u0442 \u0437\u0430\u043f\u0438\u0441\u0430\u043d\u044b \u043b\u043e\u043a\u0430\u043b\u044c\u043d\u044b\u0439 \u0437\u0430\u043f\u0443\u0441\u043a MCP, \u043c\u0430\u043d\u0438\u0444\u0435\u0441\u0442 \u0438 \u0437\u0430\u043c\u0435\u0442\u043a\u0430. \u0417\u0430\u0433\u0440\u0443\u0437\u043e\u043a \u0438 \u0441\u0435\u0442\u0435\u0432\u043e\u0433\u043e \u0434\u043e\u0441\u0442\u0443\u043f\u0430 \u043d\u0435\u0442; \u043f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u043a ChatGPT \u043e\u0441\u0442\u0430\u0451\u0442\u0441\u044f \u0437\u0430 \u0432\u0430\u043c\u0438."
    ),
}

_HEAD = "\u0417\u0430\u043f\u0440\u043e\u0441 \u043d\u0430 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435"
_ACCOUNT = "\u0410\u043a\u043a\u0430\u0443\u043d\u0442"
_CHAT = "\u0427\u0430\u0442"
_TURN = "\u0425\u043e\u0434"
_CANCEL = "\u0415\u0441\u043b\u0438 \u043e\u0442\u043a\u043b\u043e\u043d\u0438\u0442\u044c, \u043d\u0435 \u043e\u0442\u0432\u0435\u0442\u0438\u0442\u044c \u0438\u043b\u0438 \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c, \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043f\u0440\u043e\u0438\u0437\u043e\u0439\u0434\u0451\u0442 \u0438 \u043d\u0438\u0447\u0435\u0433\u043e \u043d\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u0441\u044f."
_UNFINISHED = "\u0413\u043e\u0442\u043e\u0432\u044b\u0439 \u043e\u0442\u0432\u0435\u0442 \u043d\u0435 \u0432\u0441\u0435\u0433\u0434\u0430 \u0437\u043d\u0430\u0447\u0438\u0442, \u0447\u0442\u043e \u0437\u0430\u0434\u0430\u0447\u0430 \u0437\u0430\u0432\u0435\u0440\u0448\u0435\u043d\u0430: \u043f\u0440\u043e\u0432\u0435\u0440\u044c\u0442\u0435 \u043f\u043e\u0441\u043b\u0435\u0434\u043d\u0438\u0439 \u043e\u0442\u0432\u0435\u0442."
_EXPIRES = "\u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u0434\u043e"
_PLAN = "\u041f\u043b\u0430\u043d"
_NOTE = "\u041f\u0440\u0438\u043c\u0435\u0447\u0430\u043d\u0438\u0435"
_FILES = "\u0424\u0430\u0439\u043b\u044b \u043f\u043b\u0430\u043d\u0430"


@dataclass(frozen=True)
class OwnerVerdict:
    # approved | rejected | timed_out | delivery_unavailable | uncertain
    outcome: str
    exit_code: int | None
    detail: str


def _classify(code: int, out: str, err: str) -> OwnerVerdict:
    out_lines = out.splitlines()
    first_out = out_lines[0] if out_lines else ""
    denial = next((ln for ln in err.splitlines() if ln.startswith("denied:")), "")
    detail = clean_text(denial or err.strip() or first_out, DETAIL_LIMIT)
    if code == 0:
        if _APPROVED.fullmatch(first_out):
            return OwnerVerdict("approved", 0, "")
        return OwnerVerdict("uncertain", 0, "exit 0 without an 'approved' line: " + detail)
    if code == 1:
        if denial.startswith(
            ("denied: daemon unreachable", "denied: no bot credentials configured")
        ):
            return OwnerVerdict("delivery_unavailable", 1, detail)
        if denial.startswith("denied: timeout after "):
            return OwnerVerdict("timed_out", 1, detail)
        if denial.startswith(("denied: rejected in Telegram", "denied: rejected or not answered")):
            return OwnerVerdict("rejected", 1, detail)
    return OwnerVerdict("uncertain", code, detail)


def _stop(child: subprocess.Popen) -> None:
    """Ask tg-ctl to withdraw its card (it does so on SIGTERM); never force-kill it."""
    try:
        child.terminate()
        child.wait(timeout=GRACE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _run_bounded(
    argv: list[str],
    *,
    stdin,
    timeout: float,
    env: dict[str, str] | None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` keeping at most ``CAPABILITY_MAX_BYTES + 1`` bytes of stdout.

    The capability-probe child is killed if it outgrows the cap or deadline, so this probe
    never buffers unbounded output. Oversize output is returned (one byte past the cap)
    for the caller to reject; a deadline raises ``subprocess.TimeoutExpired``.
    """
    deadline = time.monotonic() + timeout
    child = subprocess.Popen(
        argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env
    )
    chunks: list[bytes] = []
    size = 0
    try:
        fd = child.stdout.fileno()
        while size <= CAPABILITY_MAX_BYTES:
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            if not select.select([fd], [], [], left)[0]:
                continue
            chunk = os.read(fd, CAPABILITY_MAX_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > CAPABILITY_MAX_BYTES:
            child.kill()
        code = child.wait(timeout=max(deadline - time.monotonic(), 0.001))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()
    return subprocess.CompletedProcess(argv, code, b"".join(chunks).decode("utf-8", "replace"), "")


def owner_only_supported(
    argv_prefix: tuple[str, ...] = ("tg-ctl",),
    *,
    env: dict[str, str] | None = None,
    run=_run_bounded,
) -> bool:
    """True only when the trusted tg-ctl advertises ``ask-owner-only-v1``; any doubt is False."""
    try:
        done = run(
            [*argv_prefix, "capabilities", "--json"],
            stdin=subprocess.DEVNULL,
            timeout=CAPABILITY_TIMEOUT,
            env=env,
        )
        out = done.stdout
        if done.returncode != 0 or not isinstance(out, str):
            return False
        if len(out.encode("utf-8", "replace")) > CAPABILITY_MAX_BYTES:
            return False
        data = json.loads(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    version = data.get("schema_version")
    if type(version) is not int or version != 1:
        return False
    caps = data.get("capabilities")
    return isinstance(caps, list) and CAPABILITY in caps


def ask_owner(
    question: str,
    timeout_s: int,
    *,
    argv_prefix: tuple[str, ...] = ("tg-ctl",),
    env: dict[str, str] | None = None,
    popen=subprocess.Popen,
    run=_run_bounded,
) -> OwnerVerdict:
    """Block on ``tg-ctl ask --owner-only`` for at most ``timeout_s`` seconds (plus a grace).

    Fails closed: without the capability marker no ask is started and there is no fallback.
    """
    if not 1 <= timeout_s <= MAX_TIMEOUT or question.startswith("-"):
        raise ValueError("refusing an unsafe tg-ctl ask invocation")
    if not owner_only_supported(argv_prefix, env=env, run=run):
        return OwnerVerdict(
            "delivery_unavailable", None, "tg-ctl does not advertise ask-owner-only-v1"
        )
    argv = [*argv_prefix, "ask", "--owner-only", question, "--timeout", str(timeout_s)]
    try:
        child = popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as exc:  # never started, so no card exists
        return OwnerVerdict(
            "delivery_unavailable", None, f"tg-ctl could not start: {type(exc).__name__}"
        )
    try:
        out, err = child.communicate(timeout=timeout_s + GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _stop(child)
        return OwnerVerdict(
            "uncertain", None, "wait for the owner answer timed out; card state unknown"
        )
    except BaseException:
        _stop(child)
        raise
    return _classify(child.returncode, out or "", err or "")


def discover_owner(config_dir: Path | None = None) -> str | None:
    """A one-way reference to the owner chat configured for tg-cli, or None when unconfigured.

    Only ``TG_CHAT_ID`` is read from ``.env`` (never the bot token); the id itself is not
    returned, so it cannot leak into a receipt.
    """
    if config_dir is None:
        config_dir = Path(os.environ.get("TG_CTL_CONFIG_DIR") or Path.home() / ".config" / "tg-cli")
    try:
        with open(Path(config_dir) / ".env", encoding="utf-8") as handle:
            for _, line in zip(range(200), handle, strict=False):
                match = _CHAT_ID.fullmatch(line.strip())
                if match and int(match.group(1)) > 0:
                    return hashlib.sha256(b"pm-owner:" + match.group(1).encode()).hexdigest()[:12]
    except (OSError, UnicodeDecodeError):
        return None
    return None


def build_question(
    *,
    request_id: str,
    account: str | None,
    chat: str | None,
    chat_title: str,
    turn: str | None,
    action: str,
    plan_digest: str,
    expires_at: int,
    reason: str,
    scope_digest: str | None = None,
) -> str:
    """The owner-facing Russian card: every binding field the owner is approving, bounded.

    Names the account and chat, asks the action's own question (``Continue?`` for a continuation),
    states the exact effect and expiry, and says that declining or ignoring changes nothing. The
    text is fixed per action, so what the owner reads is what the plan digest binds.
    """
    from ..chatgpt_tui import stamp

    lines = [f"{_HEAD} {request_id[:8]} ({action})"]
    if account:
        lines.append(f"{_ACCOUNT}: {clean_text(account, 40)}")
    if chat:
        tail = clean_text(chat.rstrip("/").rsplit("/", 1)[-1], 40)
        title = clean_text(chat_title, 60)
        lines.append(f"{_CHAT}: {title + ' ' if title else ''}({tail})")
    if turn:
        lines.append(f"{_TURN}: {clean_text(turn, 40)}")
    lines.append("")
    lines.append(_ASK[action])
    lines.append(_EFFECT[action])
    lines.append(_CANCEL)
    if action == "continue":
        lines.append(_UNFINISHED)
    lines.append(f"{_EXPIRES} {stamp(expires_at)} (Europe/Belgrade).")
    lines.append(f"{_PLAN}: sha256:{plan_digest[:16]}")
    if scope_digest:
        lines.append(f"{_FILES}: sha256:{scope_digest[:16]}")
    if reason and reason != PROPOSAL_NOTE:
        lines.append(f"{_NOTE}: {clean_text(reason, 120)}")
    return "\n".join(lines)[:QUESTION_LIMIT]
