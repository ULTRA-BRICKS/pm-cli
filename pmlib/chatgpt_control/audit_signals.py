"""Deterministic signals in a final answer and in the request that it answers (pure, no I/O).

Accessed via: ``audit_native`` while it builds evidence. The signals are structural-style evidence
for ``audit_decision``. ``permission_expired``, ``pending_approval``, ``login_required`` and
``human_input_required`` are the ONLY things that can make a task ``blocked``: a named permission,
approval, login or physical step the assistant says it is waiting for. ``unfinished_cue`` (an
announced or admitted unfinished promise) makes the task ``incomplete`` before any model score is
read, and ``doubt_cue`` only withdraws the right to be ``done`` (the verdict abstains).

Only the closing paragraph (the last ``TAIL_CHARS`` characters, fenced code, block quotes and
quoted/inline-code spans removed) is read for the waiting and promise signals, and only STRONG
phrases count, so a word such as "left", "remaining" or "TODO" in a finished answer can never create
a signal. A sentence that says the problem was resolved ("renewed the expired permission") or that
is an optional offer ("if you want I can ...") never counts. Quoted or injected text cannot create
a blocker. Request-side checks (``detect_request``) read only the user's own latest request: whether
it demands execution, deployment/live verification or verification, wants a plan as the deliverable,
or says to stop and hand off. Every final-text signal pushes away from ``done``; none can produce
it, so injected text can only make the verdict more cautious. The Russian phrases are written as
``\\u`` escapes so the source stays ASCII.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TAIL_CHARS = 700
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_QUOTE_LINE = re.compile(r"^[ \t]*>.*$", re.MULTILINE)
_QUOTED_SPAN = re.compile(
    r"\"[^\"\n]{0,300}\"|\u201c[^\u201d\n]{0,300}\u201d|\u00ab[^\u00bb\n]{0,300}\u00bb"
    r"|`[^`\n]{0,200}`"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_FLAGS = re.IGNORECASE


def _rx(*patterns: str, flags: int = _FLAGS) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), flags)


_EXPIRED = _rx(
    r"\b(?:permission|approval|authori[sz]ation|grant|consent)s?\b[^.\n]{0,40}\bexpired\b",
    r"\bexpired\b[^.\n]{0,30}\b(?:permission|approval|authori[sz]ation|grant|consent)s?\b",
    r"\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438[^.\n]{0,40}"
    r"\u0438\u0441\u0442[\u0435\u0451]\u043a",
    r"\u0438\u0441\u0442[\u0435\u0451]\u043a[^.\n]{0,30}"
    r"\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438",
)
_APPROVAL = _rx(
    r"\bwaiting for your (?:approval|confirmation|permission)\b",
    r"\b(?:needs|requires) your (?:approval|confirmation|permission)\b",
    r"\bawaiting (?:your )?(?:approval|confirmation|permission)\b",
    r"\bonce you (?:approve|confirm|grant)\b",
    r"\bplease (?:approve|confirm) (?:it |this )?(?:to|so)\b",
    r"\u043e\u0436\u0438\u0434\u0430\u0435\u0442(?:\u0441\u044f)? "
    r"(?:\u0432\u0430\u0448\u0435\u0433\u043e )?"
    r"(?:\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438"
    r"|\u043e\u0434\u043e\u0431\u0440\u0435\u043d\u0438|\u0440\u0430\u0437\u0440\u0435\u0448\u0435\u043d\u0438)",
)
_HUMAN = _rx(
    r"\b(?:plug|insert|connect|attach)\b[^.\n]{0,50}\b(?:usb|dongle|cable|drive|stick|sd card)\b",
    r"\bphysically (?:move|plug|press|connect|insert|replace|reboot|access|be present)\b",
    r"\bphysical (?:access|action|input)\b",
    r"\bpress the (?:physical |hardware )?button\b",
    r"\bscan the qr\b",
    r"\benter the (?:one-time )?(?:code|otp|2fa)\b",
    r"\bapprove (?:it )?on your phone\b",
    r"(?:\u0432\u0441\u0442\u0430\u0432\u044c\u0442\u0435|\u043f\u043e\u0434\u043a\u043b\u044e"
    r"\u0447\u0438\u0442\u0435)[^.\n]{0,40}(?:usb|\u0444\u043b\u0435\u0448|\u0434\u043e\u043d\u0433\u043b)",
)
_LOGIN = _rx(
    r"\byou (?:need|have|must|should) (?:to )?(?:log ?in|sign ?in|re-?authenticate|re-?authori[sz]e)\b",
    r"\bplease (?:log ?in|sign ?in|re-?authenticate|re-?authori[sz]e)\b",
    r"\b(?:log ?in|sign ?in|login) (?:is )?(?:required|pending|needed)\b",
    r"\bnot (?:currently )?(?:logged|signed) in\b",
    r"\bsession (?:has )?(?:expired|timed out)[^.\n]{0,40}\b(?:log|sign|re-?auth)",
    r"\u0432\u043e\u0439\u0434\u0438\u0442\u0435 \u0432",
    r"\u043d\u0443\u0436\u043d\u043e \u0432\u043e\u0439\u0442\u0438",
    r"\u0430\u0432\u0442\u043e\u0440\u0438\u0437\u0443\u0439\u0442\u0435\u0441\u044c",
)
# A sentence that reports the problem as fixed is not a wait: "renewed the expired permission".
_RESOLVED = _rx(
    r"\b(?:renewed|refreshed|re-?granted|fixed|resolved|rotated|replaced|restored|no longer)\b",
    r"\b(?:already|have|has) (?:applied|approved|granted)\b",
    r"\b(?:will|would|do|does|did) not need\b",
    r"\bno need\b",
    r"\bwon't need\b",
)
_OFFER = _rx(
    r"\bif you\b",
    r"\bshould you\b",
    r"\bwhen you(?:'re| are)? ready\b",
    r"\blet me know\b",
    r"\bwant me to\b",
    r"\bfeel free\b",
    r"\bI can\b",
    r"\bI could\b",
    r"\bhappy to\b",
    r"\bwould you like\b",
    r"\bjust (?:say|tell|ask)\b",
    r"\u0435\u0441\u043b\u0438 \u0445\u043e\u0442\u0438\u0442\u0435",
    r"\u043c\u043e\u0433\u0443 \u0442\u0430\u043a\u0436\u0435",
    r"\u0434\u0430\u0439\u0442\u0435 \u0437\u043d\u0430\u0442\u044c",
)
_CONTINUATION = _rx(
    r"\bI(?:'ll| will) (?:now )?(?:continue|keep going|keep working|proceed)\b",
    r"\bto be continued\b",
    r"\bcontinuing in (?:the )?next\b",
    r"\b(?:still|currently) (?:working on|in progress)\b",
    r"\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0443",
    r"\u0432 \u0441\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u043c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0438",
    r"\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0435\u043d\u0438\u0435 \u0441\u043b\u0435\u0434\u0443\u0435\u0442",
)
_ACTIONS = (
    r"start|begin|implement|install|deploy|verify|test|run|apply|fix|build|write|create|add|"
    r"update|flash|migrate|configure"
)
_NEXT_STEP = _rx(
    r"\bnext,? I(?:'ll| will)\b",
    rf"\bI(?:'ll| will) (?:now |then |next |first )?(?:{_ACTIONS})\b",
)
# Work the assistant says it has (not) carried out, versus checks it says it has (not) made.
_DONE_PARTICIPLES = (
    r"installed|deployed|released|merged|applied|configured|flashed|implemented|done|finished|"
    r"completed|started|fixed|resolved|rotated|converted|migrated|updated|written|created"
)
_CHECK_PARTICIPLES = r"verified|tested|run|compared|checked|validated|confirmed|live"
_PARTICIPLES = f"{_DONE_PARTICIPLES}|{_CHECK_PARTICIPLES}"
_NOT_YET = _rx(
    rf"\bnot yet (?:been )?(?:{_PARTICIPLES})\b",
    rf"\b(?:haven't|have not|hasn't|has not) yet (?:been )?(?:{_PARTICIPLES})\b",
    r"\bstill (?:need|needs|have|has) to\b",
    rf"\bstill not (?:{_PARTICIPLES})\b",
    rf"\b(?:yet to be|remains? to be) (?:{_PARTICIPLES})\b",
    # Perfect-tense admissions about work (not checks): "has not been installed", "have not fixed".
    rf"\b(?:haven't|have not|hasn't|has not) (?:been )?(?:{_DONE_PARTICIPLES})\b",
    r"\bI (?:did not|didn't|could not|couldn't|can't|cannot) (?:yet )?(?:fix|implement|install"
    r"|deploy|finish|complete|convert|migrate|rotate|apply|resolve|configure|flash)\b",
    r"\b(?:are|is|remains?|remain) (?:still )?pending\b",
    r"\bnot (?:finished|completed|done)\b",
    r"\u043f\u043e\u043a\u0430 \u043d\u0435 (?:\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u043b\u0435\u043d"
    r"|\u043f\u0440\u043e\u0432\u0435\u0440\u0435\u043d|\u0440\u0430\u0437\u0432\u0435\u0440\u043d\u0443\u0442"
    r"|\u043f\u0440\u043e\u0442\u0435\u0441\u0442\u0438\u0440\u043e\u0432\u0430\u043d)",
)
# Counts only when the request itself demanded the verification or the deployment.
_NOT_DONE_BARE = _rx(
    r"\b(?:haven't|have not|hasn't|has not|didn't|did not|wasn't|was not|were not|weren't) "
    rf"(?:been )?(?:{_CHECK_PARTICIPLES}|installed|deployed)\b",
    rf"\bnot (?:been )?(?:{_CHECK_PARTICIPLES}|installed|deployed)\b",
    r"\b(?:untested|unverified|undeployed|uninstalled)\b",
)
_PLAN = _rx(
    r"\bhere(?:'s| is) (?:the|my|a|an) (?:proposed |suggested |step-by-step |implementation "
    r"|detailed )?plan\b",
    r"^\s*(?:#+\s*)?(?:\*\*)?(?:proposed |implementation |step-by-step |suggested )?plan\b",
    r"\bproposed (?:approach|steps|plan)\b",
    r"\bI(?:'d| would) (?:suggest|propose|recommend) (?:the following|doing|we)\b",
    r"\bsteps? to (?:take|follow)\b",
    r"\bhere(?:'s| is) (?:how|what) (?:I|we) (?:would|will|could|can|propose)\b",
    flags=_FLAGS | re.MULTILINE,
)
_COMPLETION = _rx(
    r"\bI(?:'ve| have)\b",
    r"\b(?:done|completed|implemented|installed|deployed|fixed|created|added|updated|configured|"
    r"merged|applied|verified|passed|passing|released|flashed|migrated|ran|tested)\b",
)
_DEPLOY_EVIDENCE = _rx(
    r"\b(?:installed|deployed|released|rolled out|shipped|flashed|restarted|is live|went live)\b",
    r"\bin production\b",
    r"\b(?:http|status|returned|got) 200\b",
    r"\b200 ok\b",
    r"\bhealth ?check (?:passed|returned|ok|is ok|succeeded)\b",
    r"\b(?:verified|confirmed) (?:live|in production|on the device)\b",
    r"\blive[- ]verified\b",
)
_EXEC = _rx(
    r"\b(?:implement|install|deploy|flash|fix|set ?up|configure|migrate|patch|apply|build|release"
    r"|ship|refactor|verify|restart|upgrade)\b",
    r"\brun (?:the|a|it|this)\b",
)
_DEPLOY = _rx(
    r"\b(?:install|deploy|release|ship|roll ?out|go live|flash)\b",
    r"\blive[- ]verif",
    r"\b(?:in|to|on) prod(?:uction)?\b",
    r"\brestart the service\b",
)
_VERIFY = _rx(
    r"\b(?:verify|verified|test|tests|tested|install|deploy|flash|confirm|ensure|make sure)\b",
)
_PLAN_DELIVERABLE = _rx(r"\b(?:plan|proposal|outline|roadmap|strategy|brainstorm|ideas?)\b")
_WRAP = _rx(
    r"\bwrap(?:ping)? (?:it |this |things |everything )?up\b",
    r"\bcall it a day\b",
    r"\bpark (?:it|this)\b",
    r"\bpause (?:here|work)\b",
    r"\bstop (?:here|work(?:ing)?|now)\b",
    r"\bthat'?s enough\b",
    r"\bsign off\b",
    r"\bclose (?:it |this )?out\b",
    r"\u0441\u0432\u0435\u0440\u043d\u0438",
    r"\u0437\u0430\u0432\u0435\u0440\u0448\u0438 \u0440\u0430\u0431\u043e\u0442",
    r"\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0441\u044c",
)
_HANDOFF = _rx(
    r"\bhand[- ]?off\b",
    r"\bhand (?:it|this|things) over\b",
    r"\bsummary\b|\bsummari[sz]e\b",
    r"\bstatus (?:notes?|summary)\b",
    r"\bnotes? for (?:the )?next\b",
    r"\u0441\u0432\u043e\u0434\u043a",
    r"\u0440\u0435\u0437\u044e\u043c\u0435",
)


@dataclass(frozen=True)
class Signals:
    pending_approval: bool
    permission_expired: bool
    human_input_required: bool
    login_required: bool
    continuation_announced: bool
    unfinished_cue: (
        str  # "" | continuation-announced | next-step-promised | not-yet-done | plan-only
    )
    doubt_cue: str  # "" | plan-like | deployment-unevidenced


@dataclass(frozen=True)
class RequestSignals:
    stop_intent: bool
    exec_demand: bool
    deploy_demand: bool
    verify_demand: bool
    plan_deliverable: bool


def closing_paragraph(text: str) -> str:
    """The last ``TAIL_CHARS`` characters outside fenced code."""
    return _FENCE.sub(" ", text)[-TAIL_CHARS:]


def _closing_sentences(text: str) -> list[str]:
    """Sentences of the closing paragraph with quotes, block quotes and inline code removed."""
    tail = _QUOTED_SPAN.sub(" ", _QUOTE_LINE.sub(" ", closing_paragraph(text)))
    return [s for s in _SENTENCE.split(tail) if s.strip()]


def _waits(sentences: list[str], pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(s) and not _RESOLVED.search(s) for s in sentences)


def _promises(sentences: list[str], pattern: re.Pattern[str]) -> bool:
    return any(pattern.search(s) and not _OFFER.search(s) for s in sentences)


def detect_request(request_text: str) -> RequestSignals:
    text = request_text if isinstance(request_text, str) else ""
    return RequestSignals(
        stop_intent=_WRAP.search(text) is not None and _HANDOFF.search(text) is not None,
        exec_demand=_EXEC.search(text) is not None,
        deploy_demand=_DEPLOY.search(text) is not None,
        verify_demand=_VERIFY.search(text) is not None,
        plan_deliverable=_PLAN_DELIVERABLE.search(text) is not None,
    )


def _unfinished_cue(sentences: list[str], whole: str, request: RequestSignals) -> tuple[str, str]:
    """(unfinished cue, doubt cue); the cue only ever argues AGAINST ``done``."""
    if _promises(sentences, _CONTINUATION):
        return "continuation-announced", ""
    if _promises(sentences, _NEXT_STEP):
        return "next-step-promised", ""
    if _promises(sentences, _NOT_YET):
        return "not-yet-done", ""
    if (request.verify_demand or request.deploy_demand) and _promises(sentences, _NOT_DONE_BARE):
        return "not-yet-done", ""
    plan = _PLAN.search(whole) is not None
    if plan and request.exec_demand and not request.plan_deliverable:
        if _COMPLETION.search(whole) is None:
            return "plan-only", ""
        return "", "plan-like"
    if plan and request.exec_demand:
        return "", "plan-like"
    if request.deploy_demand and _DEPLOY_EVIDENCE.search(whole) is None:
        return "", "deployment-unevidenced"
    return "", ""


def detect(final_text: str, request_text: str = "") -> Signals:
    text = final_text if isinstance(final_text, str) else ""
    whole = _FENCE.sub(" ", text)
    sentences = _closing_sentences(text)
    cue, doubt = _unfinished_cue(sentences, whole, detect_request(request_text))
    return Signals(
        pending_approval=_waits(sentences, _APPROVAL),
        permission_expired=_waits(sentences, _EXPIRED),
        human_input_required=_waits(sentences, _HUMAN),
        login_required=_waits(sentences, _LOGIN),
        continuation_announced=bool(cue),
        unfinished_cue=cue,
        doubt_cue=doubt,
    )


# What a conversation with no CURRENT final answer carries: nothing to read, nothing asserted.
EMPTY = Signals(False, False, False, False, False, "", "")
