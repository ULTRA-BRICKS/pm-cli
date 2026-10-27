"""Redaction of credential-looking text before it reaches evidence, the model or any report.

Accessed via: ``audit_native.build_evidence`` (request and final text, the only text the audit
ever holds) so a pasted token cannot be persisted, sent to the local model or put in a Telegram
message by any later stage.

Invariants: pure, idempotent, and linear time on hostile input (every pattern is a single bounded
character class run, no nested quantifiers). It removes well-known credential shapes (API keys,
cloud and chat-service tokens, JWTs, bearer tokens, private-key blocks, URL userinfo) and the VALUE
of ``name: value`` / ``name=value`` pairs whose name says it is a secret. It never alters ordinary
prose, commit hashes or UUIDs. It is defence in depth, not a promise to find every secret: the
audit's real rule is that conversation text is never persisted or notified at all.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]{0,40}-----|\Z)", re.DOTALL
)
_TOKENS = re.compile(
    r"sk-[A-Za-z0-9_-]{20,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}"
    r"|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}"
    r"|AIza[0-9A-Za-z_-]{35}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r"|\b\d{8,10}:[A-Za-z0-9_-]{35}\b"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")
_USERINFO = re.compile(r"(?i)(\bhttps?://)[^\s/@:]{1,128}:[^\s/@]{1,256}@")
_PAIR = re.compile(
    r"(?i)(\b(?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|access[_-]?key|auth(?:orization)?)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;]{6,}"
)


def redact(text: str) -> str:
    """``text`` with credential-looking material replaced by ``[REDACTED]``."""
    cleaned = _PEM.sub(REDACTED, text)
    cleaned = _TOKENS.sub(REDACTED, cleaned)
    cleaned = _BEARER.sub(REDACTED, cleaned)
    cleaned = _USERINFO.sub(lambda m: m.group(1) + REDACTED + "@", cleaned)
    return _PAIR.sub(lambda m: m.group(1) + REDACTED, cleaned)
