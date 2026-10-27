"""Which Guardian account a browser session really is: recorded once, verified on every audit.

Accessed via: ``audit_collector`` (turns a reader's opaque identity hash into the evidence's
``observed_account``) and ``pm chatgpt audit bind`` (the explicit owner action that records it).

Invariants: a binding is the SHA-256 identity hash of the logged-in session (computed inside the
page by ``audit_browser``; never an email, id or token) stored privately per account. Nothing is
ever trusted by default: an account with no binding, a session whose hash matches no binding, or no
identity at all is ``None`` (the decision layer then answers ``account-missing``); a session that
matches ANOTHER account's binding is reported as that account (``account-mismatch``). One session
cannot be bound to two accounts. Binding is an owner decision recorded by an explicit command and
re-checked on every run, so a swapped login is noticed instead of silently audited as the old one.
"""

from __future__ import annotations

import hmac
import re
from pathlib import Path

from . import audit_files
from .audit_reader import ReaderIdentity

FILE = "bindings.json"
SCHEMA = 1
MAX_BYTES = 8192
_ACCOUNT = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
_HASH = re.compile(r"[0-9a-f]{64}")


class BindingError(ValueError):
    """The account id or identity hash is not acceptable for a binding."""


class AccountBindings:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, str]:
        data = audit_files.read_json(self.path, max_bytes=MAX_BYTES)
        if not isinstance(data, dict) or data.get("v") != SCHEMA:
            return {}
        accounts = data.get("accounts")
        if not isinstance(accounts, dict):
            return {}
        return {
            k: v
            for k, v in accounts.items()
            if isinstance(k, str)
            and _ACCOUNT.fullmatch(k)
            and isinstance(v, str)
            and _HASH.fullmatch(v)
        }

    def bound(self, account: str) -> str | None:
        return self._load().get(account)

    def record(self, account: str, identity: ReaderIdentity) -> None:
        if not isinstance(account, str) or _ACCOUNT.fullmatch(account) is None or account == "all":
            raise BindingError("account must be a short lower-case account id")
        if not isinstance(identity.account_hash, str) or not _HASH.fullmatch(identity.account_hash):
            raise BindingError("identity must be a 64-character lower-case SHA-256 hex digest")
        with audit_files.file_lock(self.path):
            accounts = self._load()
            for other, digest in accounts.items():
                if other != account and hmac.compare_digest(digest, identity.account_hash):
                    raise BindingError(f"this browser session is already bound to {other}")
            accounts[account] = identity.account_hash
            audit_files.write_json(
                self.path, {"v": SCHEMA, "accounts": accounts}, max_bytes=MAX_BYTES
            )

    def observed_account(self, identity: ReaderIdentity | None, requested: str) -> str | None:
        """The bound account ``identity`` belongs to, or ``None`` when nothing proves it.

        ``requested`` must itself be bound: asking for an account nobody ever bound is ``None``,
        so a missing binding can never be satisfied by whatever session happens to be open.
        """
        if identity is None or self.bound(requested) is None:
            return None
        for account, digest in self._load().items():
            if hmac.compare_digest(digest, identity.account_hash):
                return account
        return None
