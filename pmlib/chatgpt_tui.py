"""Keyboard-driven ChatGPT Guardian monitor with confirmed per-account actions.

Refreshing is always read-only. Every durable change goes through ``pm chatgpt`` (the
existing validated control path) after an explicit y/n naming account, chat and action.
Curses is imported only on entry; everything above ``run_tui`` is pure and testable.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from .chatgpt_monitor import ACCOUNTS, CHAT_URL

EVENT_KIND = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
CHAT_LABEL_WIDTH = 12
UNRECOGNIZED = "(unrecognized)"
ZONE = ZoneInfo("Europe/Belgrade")
ACTION_TIMEOUT = 45  # Guardian control waits up to 30 s; service start/stop up to 20 s.
LABELS = {
    "watch": "watch",
    "unwatch": "unwatch",
    "pause": "pause Guardian (stop --chat)",
    "resume": "resume (resume --chat)",
    "service-start": "start service",
    "service-stop": "stop service",
}
MENU = (
    ("w", "watch"),
    ("u", "unwatch"),
    ("p", "pause"),
    ("R", "resume"),
    ("S", "service-start"),
    ("X", "service-stop"),
)
STALE_AFTER = 180  # seconds a source snapshot may age before it no longer counts as fresh
# These need fresh evidence of the target account; pause and service-stop stay available stale.
FRESH_ONLY = ("watch", "unwatch", "resume", "service-start")


def clean(value) -> str:
    text = str(value if value is not None else "")
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf", "Cs"))


def text_width(text):
    return sum(
        0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
        for c in text
    )


def fit(value, width):
    out = ""
    used = 0
    for ch in clean(value):
        size = text_width(ch)
        if used + size > max(0, width):
            break
        out += ch
        used += size
    return out


def stamp(value, short=False):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "-"
    try:
        return dt.datetime.fromtimestamp(value, ZONE).strftime(
            "%H:%M:%S" if short else "%d %b %H:%M:%S"
        )
    except (ValueError, OSError, OverflowError):
        return "-"


def age(value, now):
    return (
        f"{max(0, int(now - value))}s"
        if isinstance(value, (int, float)) and math.isfinite(value)
        else "unknown"
    )


@dataclass(frozen=True)
class Action:
    account: str
    kind: str
    chat: str | None = None


@dataclass(frozen=True)
class ActionResult:
    outcome: str  # success | pending | uncertain | failed
    message: str


def _accounts(data):
    if "accounts" in data:
        return {a.get("workspace"): a for a in data["accounts"]}
    workspace = data.get("workspace")
    return {
        workspace: {
            "ok": True,
            "stale": data.get("stale", True),
            "generated_at": data.get("generated_at"),
        }
    }


def _is_stale(generated, flagged, now):
    """One freshness predicate for render and planner: flag, missing/non-finite time, or age."""
    if flagged or isinstance(generated, bool) or not isinstance(generated, (int, float)):
        return True
    return not math.isfinite(generated) or now - generated > STALE_AFTER


def plan_action(data, row, kind, now=None):
    """Validate one action against the displayed data: ``(Action, None)`` or ``(None, reason)``."""
    now = time.time() if now is None else now
    data = data or {}
    if kind not in LABELS:
        return None, "unknown action"
    if not isinstance(row, dict):
        return None, "no row selected (A focuses an account for service actions)"
    account = row.get("account", data.get("workspace"))
    if account not in ACCOUNTS:
        return None, f"account {clean(account)!r} is not one supported account"
    if kind.startswith("service-"):
        if kind in FRESH_ONLY:
            refusal = _fresh_refusal(data, account, now)
            if refusal:
                return None, refusal
        return Action(account, kind), None
    chat = row.get("chat")
    if not isinstance(chat, str) or CHAT_URL.fullmatch(chat) is None:
        return None, "selected row has no exact permanent chat URL"
    matches = [
        r
        for r in data.get("sessions", [])
        if r.get("account", data.get("workspace")) == account and r.get("chat") == chat
    ]
    if not matches:
        return None, f"chat is not in {account}'s current session list"
    if len(matches) > 1:
        return None, f"ambiguous: {account} lists this chat {len(matches)} times"
    if kind in FRESH_ONLY:
        refusal = _fresh_refusal(data, account, now)
        if refusal:
            return None, refusal
        if matches[0].get("session_stale"):
            return None, f"{account} data is stale; refresh before changing this chat"
    return Action(account, kind, chat), None


def _fresh_refusal(data, account, now):
    state = _accounts(data).get(account)
    if not state or not state.get("ok", False):
        return f"{account} telemetry is unavailable; state is unknown"
    if _is_stale(state.get("generated_at"), state.get("stale", True), now):
        return f"{account} data is stale; refresh before changing this chat or service"
    return None


def revalidate(data, action, now=None):
    """Re-plan a confirmed action against newer data using only its frozen target.

    The row is built from the action itself, never from the current selection, so a list
    refresh that reorders rows cannot retarget the confirmed action.
    """
    row = {"account": action.account}
    if action.chat is not None:
        row["chat"] = action.chat
    return plan_action(data, row, action.kind, now)


def _argv(action):
    if action.kind in ("watch", "unwatch"):
        return [action.kind, action.chat]
    if action.kind in ("pause", "resume"):
        return ["stop" if action.kind == "pause" else "resume", "--chat", action.chat]
    return ["service", action.kind.removeprefix("service-")]


SUCCESS = {
    "watch": "watch recorded for this chat; this does not enable recovery sends",
    "unwatch": "unwatch recorded for this chat",
    "pause": "Guardian pause recorded for this chat; use resume to lift it",
    "resume": "resume applied to this chat only; global, quota and uncertain blocks are not cleared",
    "service-start": "service start accepted; not all chats are necessarily covered, "
    "check each chat's observed state",
    "service-stop": "service stop accepted; recovery stops for this account only. The observer "
    "(monitoring) is a separate service and is NOT stopped, so observed state may continue",
}


def run_action(action, runner=subprocess.run):
    """Run one confirmed action via ``pm chatgpt``: argv list, no shell, bounded, never retried."""
    if action.account not in ACCOUNTS or action.kind not in LABELS:
        return ActionResult("failed", "refused: unsupported account or action")
    if not action.kind.startswith("service-") and (
        not isinstance(action.chat, str) or CHAT_URL.fullmatch(action.chat) is None
    ):
        return ActionResult("failed", "refused: not an exact permanent chat URL")
    command = [
        sys.executable,
        "-m",
        "pmlib.cli",
        "chatgpt",
        "--workspace",
        action.account,
        *_argv(action),
        "--apply",
    ]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(
        os.environ, PYTHONPATH=os.pathsep.join(filter(None, (root, os.environ.get("PYTHONPATH"))))
    )
    try:
        result = runner(
            command,
            capture_output=True,
            text=True,
            timeout=ACTION_TIMEOUT,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return ActionResult(
            "uncertain",
            f"{LABELS[action.kind]} timed out; the change may still apply. "
            "Not retried: check observed state first",
        )
    except OSError as exc:
        return ActionResult("failed", f"cannot run pm: {type(exc).__name__}")
    code = result.returncode
    if code == 0:
        return ActionResult("success", SUCCESS[action.kind])
    if code == 3:
        return ActionResult(
            "pending", f"{LABELS[action.kind]} requested; state still changing. Not retried"
        )
    if code == 124:
        return ActionResult(
            "uncertain",
            f"{LABELS[action.kind]} timed out; the change may still apply. "
            "Not retried: check observed state first",
        )
    detail = clean(result.stderr or "").strip().splitlines()
    return ActionResult(
        "failed",
        f"{LABELS[action.kind]} failed (exit {code}): {detail[-1][:160] if detail else '-'}",
    )


def describe(action):
    if action.chat is None:
        return (
            f"{LABELS[action.kind]} for account [{action.account}] (whole service, all its chats)"
        )
    return f"{LABELS[action.kind]} on [{action.account}] {action.chat}"


@dataclass
class ViewState:
    pane: str = "sessions"
    selected: int = 0
    query: str = ""
    editing: bool = False
    details: bool = False
    filter: str = "all"
    paused: bool = False
    error: str = ""
    focus: str = ""
    menu: bool = False
    confirm: Action | None = None
    notice: str = ""
    actions: bool = True
    pending: bool = False

    def _propose(self, kind, data, now=None):
        rows = visible_rows(data or {}, self)
        if rows:
            row = rows[min(self.selected, len(rows) - 1)]
        else:
            row = {"account": self.focus} if self.focus else None
        if kind in FRESH_ONLY and self.error:
            action, refusal = None, "latest refresh failed; data may be outdated, refresh first"
        else:
            action, refusal = plan_action(data, row, kind, now)
        if action is None:
            self.notice = "Refused: " + refusal
        else:
            self.confirm = action
            self.notice = ""

    def key(self, key, count, data=None, now=None):
        if self.confirm is not None:
            action, self.confirm = self.confirm, None
            if key == "y":
                current, refusal = revalidate(data, action, now)
                if action.kind in FRESH_ONLY and self.error:
                    current, refusal = None, "latest refresh failed; data may be outdated"
                if current != action:
                    self.notice = "Refused: " + (refusal or "target changed")
                    return None
                return ("action", action)
            self.notice = "Cancelled; nothing changed"
            return None
        if self.menu:
            self.menu = False
            kind = dict(MENU).get(key)
            if kind:
                self._propose(kind, data, now)
            else:
                self.notice = "Cancelled; nothing changed"
            return None
        if self.editing:
            if key in ("\n", "\r"):
                self.editing = False
            elif key == "\x1b":
                self.editing = False
                self.query = ""
            elif key in ("\x7f", "\b", "BACKSPACE"):
                self.query = self.query[:-1]
            elif isinstance(key, str) and len(key) == 1 and key.isprintable():
                self.query += key
            self.selected = 0
            return None
        if key == "q":
            if self.pending:
                self.notice = "Action still running; wait for its result before quitting"
                return None
            return "quit"
        if key == "r":
            return "refresh"
        if key == "a":
            if self.actions:
                self.menu = True
                self.notice = ""
            else:
                self.notice = "Actions are disabled: this view uses a custom --state (read-only)"
        elif key == "A":
            order = ("", *ACCOUNTS)
            self.focus = order[(order.index(self.focus) + 1) % len(order)]
            self.selected = 0
        elif key == " ":
            self.paused = not self.paused
        elif key == "\t":
            self.pane = "events" if self.pane == "sessions" else "sessions"
            self.selected = 0
            self.details = False
        elif key == "/":
            self.editing = True
            self.query = ""
            self.selected = 0
        elif key == "f":
            choices = ("all", "watched", "problems")
            self.filter = choices[(choices.index(self.filter) + 1) % len(choices)]
            self.selected = 0
        elif key in ("j", "DOWN", "PAGEDOWN"):
            self.selected = min(max(0, count - 1), self.selected + (10 if key == "PAGEDOWN" else 1))
        elif key in ("k", "UP", "PAGEUP"):
            self.selected = max(0, self.selected - (10 if key == "PAGEUP" else 1))
        elif key in ("\n", "\r"):
            self.details = not self.details
        elif key == "\x1b":
            self.details = False
            self.query = ""
            self.notice = ""
        return None


def visible_rows(snapshot, state):
    rows = list(snapshot.get("sessions" if state.pane == "sessions" else "events", []))
    if state.focus and "accounts" in snapshot:
        rows = [r for r in rows if r.get("account") == state.focus]
    if state.pane == "sessions":
        if state.filter == "watched":
            rows = [r for r in rows if r.get("watched")]
        elif state.filter == "problems":
            rows = [
                r
                for r in rows
                if r.get("no_visible_progress")
                or r.get("status") == "Thinking failed"
                or r.get("reason") not in ("", None, "not-enrolled")
            ]
    if state.query:
        rows = [
            r
            for r in rows
            if state.query.casefold() in clean(" ".join(map(str, r.values()))).casefold()
        ]
    return rows


def _account_line(account, now):
    name = clean(account.get("workspace"))
    if not account.get("ok"):
        return f"{name}: UNAVAILABLE {clean(account.get('error'))}"
    s = account.get("summary") or {}
    generated = account.get("generated_at")
    stale = _is_stale(generated, account.get("stale", True), now)
    return (
        f"{name}: service {clean((account.get('service') or {}).get('active', 'unknown'))}"
        f" | source {stamp(generated, True)} ({age(generated, now)}) {'STALE' if stale else 'fresh'}"
        f" | watched {s.get('enrolled', '?')} present {s.get('present', '?')}"
        f" running {s.get('running', '?')} blocked {s.get('blocked', '?')}"
        f" | 24h sent {s.get('sent_24h', '?')} cont {s.get('resumed_24h', '?')}"
        f" done {s.get('completed_24h', '?')}"
    )


def _modal(state, snapshot):
    if state.confirm is not None:
        lines = [f"CONFIRM {describe(state.confirm)}  y/n (any other key cancels)"]
        if state.confirm.kind == "resume":
            lines.append("Only this chat; global/quota/uncertain blocks are not cleared.")
        return lines
    if state.menu:
        rows = visible_rows(snapshot, state)
        row = rows[min(state.selected, len(rows) - 1)] if rows else {}
        target = f"[{row.get('account', snapshot.get('workspace', '?'))}] {row.get('chat', '-')}"
        return [
            "ACTIONS " + " | ".join(f"{key} {LABELS[kind]}" for key, kind in MENU) + " | Esc close",
            f"Target: {target} (service actions affect that whole account)",
        ]
    return []


def event_kind_label(kind):
    """Event kind as a plain identifier; free text could imitate another column or account."""
    if isinstance(kind, str) and EVENT_KIND.fullmatch(kind):
        return kind
    return "" if kind == "" else UNRECOGNIZED


def event_chat_label(chat):
    """Short chat id for the Events pane; only a recognised chat URL is ever echoed.

    Service-level events carry an empty (or absent) chat; anything else (wrong type, hostile
    text) is shown as a fixed placeholder so it can neither crash rendering nor imitate an
    account label.
    """
    if chat == "":
        return "-"
    if isinstance(chat, str) and CHAT_URL.fullmatch(chat):
        return chat.rstrip("/").rsplit("/", 1)[-1][:CHAT_LABEL_WIDTH]
    return UNRECOGNIZED


def render_lines(snapshot, state, width, height, now=None):
    now = time.time() if now is None else now
    snapshot = snapshot or {}
    combined = "accounts" in snapshot
    if combined:
        lines = [f"ChatGPT Guardian | all accounts | {stamp(now)} Europe/Belgrade"]
        lines += [_account_line(a, now) for a in snapshot["accounts"]]
        lines.append("coverage: dedicated browser per account, not account-wide")
    else:
        s = snapshot.get("summary", {})
        generated = snapshot.get("generated_at")
        stale = _is_stale(generated, snapshot.get("stale", True), now)
        lines = [
            f"ChatGPT Guardian | {snapshot.get('workspace', '')} | {stamp(now)} Europe/Belgrade",
            f"watched {s.get('enrolled', '?')} | present {s.get('present', '?')} | available {s.get('available', '?')} | running {s.get('running', '?')} | blocked {s.get('blocked', '?')}",
            f"24h: sent {s.get('sent_24h', '?')} | continued {s.get('resumed_24h', '?')} | completed {s.get('completed_24h', '?')}",
            f"Source {stamp(generated)} ({age(generated, now)}) {'STALE' if stale else 'fresh'} | Guardian heartbeat {age(snapshot.get('heartbeat_at'), now)} | {snapshot.get('connection', 'unknown')}",
            f"Service {snapshot.get('service', {}).get('active', 'unknown')} | coverage: dedicated browser, not account-wide",
        ]
    lines[1:1] = _modal(state, snapshot)
    if state.error:
        lines.append("UNAVAILABLE: " + state.error + " | last snapshot retained")
    if state.notice:
        lines.append("Last action: " + state.notice)
    if state.paused:
        lines.append("Display updates paused; Guardian is NOT paused")
    if combined:
        limitations = [
            f"[{a['workspace']}] {m}"
            for a in snapshot["accounts"]
            for m in (a.get("limitations") or [])
        ]
    else:
        limitations = snapshot.get("limitations", [])
    for limitation in limitations[:2]:
        lines.append("LIMITATION: " + str(limitation))
    lines.append(
        f"[{state.pane}] filter={state.filter} account={state.focus or 'all'} search={'/' if state.editing else ''}{state.query}"
        if combined
        else f"[{state.pane}] filter={state.filter} search={'/' if state.editing else ''}{state.query}"
    )
    rows = visible_rows(snapshot, state)
    selected = min(state.selected, max(0, len(rows) - 1))
    if state.details and rows:
        row = rows[selected]
        keys = (
            (
                *(("account",) if combined else ()),
                "title",
                "chat",
                "watched",
                "present",
                "status",
                "reason",
                *(k for k in ("error_detail", "session_stale") if k in row),
                "last_checked_at",
                "next_check_at",
                "backoff_seconds",
                "last_go_at",
                "last_go_state",
                "continuation",
                "continuation_turn",
                "continuation_observed_at",
                "completion_observed_at",
                "last_progress_at",
                "model",
            )
            if state.pane == "sessions"
            else tuple(row)
        )
        for key in keys:
            value = row.get(key)
            if key.endswith("_at") or key == "at":
                value = stamp(value)
            lines.append(f"{key}: {value if value is not None else '-'}")
    elif state.pane == "sessions":
        lines.append("  W  Session / status / reason")
        lines.append("     Last go (dispatch) | continuation evidence | checked -> next check")
        capacity = max(1, (height - len(lines) - 2) // 2)
        start = max(0, selected - capacity + 1)
        for i, row in enumerate(rows[start : start + capacity], start):
            mark = ">" if i == selected else " "
            title = fit(row.get("title", ""), 48)
            label = f"[{clean(row.get('account'))}] " if combined else ""
            lines.append(
                f"{mark} {'Y' if row.get('watched') else '-'}  {label}{title} [{row.get('status', 'unknown')}] {row.get('reason', '')}"
            )
            lines.append(
                f"     {stamp(row.get('last_go_at'), True)} {row.get('last_go_state') or '-'} | {row.get('continuation', 'unknown')} | {stamp(row.get('last_checked_at'), True)} -> {stamp(row.get('next_check_at'), True)}"
            )
    else:
        lines.append("  Observed at | event | session (baseline = history, not a new send)")
        capacity = max(1, height - len(lines) - 2)
        start = max(0, selected - capacity + 1)
        for i, event in enumerate(rows[start : start + capacity], start):
            label = f"[{clean(event.get('account'))}] " if combined else ""
            lines.append(
                f"{'>' if i == selected else ' '} {stamp(event.get('at'), True)} | {event_kind_label(event.get('kind', ''))} | {label}{event_chat_label(event.get('chat', ''))} {'baseline' if event.get('baseline') else ''}"
            )
    if not rows:
        lines.append("No matching rows")
    footer = "a actions | Tab panes | j/k select | Enter details | / search | f filter | A account | r refresh (read-only) | Space pause display | q quit"
    lines = lines[: max(0, height - 1)] + [footer]
    return [fit(line, max(0, width - 1)) for line in lines[:height]]


def run_tui(fetch, *, interval=5, initial=None, act=run_action):
    """Curses loop. ``act=None`` makes the view read-only; fetch and act run on worker threads."""
    import curses

    state = ViewState(actions=act is not None)
    results = queue.Queue(maxsize=1)
    outcomes = queue.Queue(maxsize=1)
    worker = None
    actor = None
    running = None
    snapshot = initial
    next_refresh = 0.0
    recheck = False

    def load():
        try:
            results.put((fetch(), ""))
        except (ValueError, OSError, RuntimeError, TypeError) as exc:
            results.put((None, clean(str(exc))))

    def perform(action):
        try:
            outcomes.put(act(action))
        except Exception as exc:  # noqa: BLE001 - any failure must surface as a result
            outcomes.put(ActionResult("failed", f"action crashed: {type(exc).__name__}"))

    def screen(stdscr):
        nonlocal worker, actor, running, snapshot, next_refresh, recheck
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(150)
        keys = {
            curses.KEY_UP: "UP",
            curses.KEY_DOWN: "DOWN",
            curses.KEY_PPAGE: "PAGEUP",
            curses.KEY_NPAGE: "PAGEDOWN",
            curses.KEY_BACKSPACE: "BACKSPACE",
            curses.KEY_ENTER: "\n",
        }
        while True:
            try:
                fresh, error = results.get_nowait()
                worker = None
                state.error = error
                if fresh is not None:
                    snapshot = fresh
                # A read that started before an action finished may predate its effect.
                next_refresh = 0.0 if recheck else time.monotonic() + interval
                recheck = False
            except queue.Empty:
                pass
            try:
                outcome = outcomes.get_nowait()
                actor = None
                state.pending = False
                state.notice = f"{outcome.outcome}: {clean(outcome.message)}"
                stdscr.clearok(True)  # repaint whole screen so the result is never half-diffed
                state.paused = False
                if worker is None:
                    next_refresh = 0.0
                else:
                    recheck = True
            except queue.Empty:
                pass
            if not state.paused and worker is None and time.monotonic() >= next_refresh:
                worker = threading.Thread(target=load, name="guardian-monitor-read", daemon=True)
                worker.start()
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            for y, line in enumerate(render_lines(snapshot, state, w, h)):
                try:
                    stdscr.addstr(
                        y, 0, line, curses.A_REVERSE if line.startswith(">") else curses.A_NORMAL
                    )
                except curses.error:
                    pass
            stdscr.refresh()
            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            action = state.key(
                keys.get(key, key),
                len(visible_rows(snapshot or {}, state)),
                snapshot,
                time.time(),
            )
            if action == "quit":
                return 0
            if action == "refresh":
                next_refresh = 0
                state.paused = False
            elif isinstance(action, tuple):
                if actor is not None:
                    state.notice = "Another action is still running; wait for its result"
                else:
                    state.notice = f"running: {describe(action[1])} (not retried on timeout)"
                    running = action[1]
                    state.pending = True
                    actor = threading.Thread(
                        target=perform, args=(action[1],), name="guardian-action", daemon=True
                    )
                    actor.start()

    try:
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(25)
        return curses.wrapper(screen)
    except KeyboardInterrupt:
        if state.pending and running is not None:
            print(
                f"interrupted while running: {describe(running)}; its outcome is UNKNOWN "
                "(it was not retried): check observed state before repeating it",
                file=sys.stderr,
            )
        return 130
