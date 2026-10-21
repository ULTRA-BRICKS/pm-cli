"""Stable error vocabulary for the control API envelope (``{ok:false,error:{code,message}}``)."""

from __future__ import annotations

CODES = (
    "invalid_request",  # envelope is not {method, params?}
    "invalid_params",  # a parameter is missing, mistyped or out of range
    "unknown_key",  # a key the method does not define
    "forbidden_key",  # an authority-bearing key (approved, sender, url, ...) was supplied
    "unknown_method",
    "not_found",  # request id, account chat or connector is unknown
    "account_unavailable",  # fresh telemetry for the addressed account could not be read
    "refused",  # policy refuses the request against current evidence
    "busy",  # too many unresolved requests are already queued
    "unauthorized",  # HTTP only: missing or wrong bearer token
    "payload_too_large",
    "internal_error",
)


class ApiError(Exception):
    """A fail-closed API refusal; ``message`` is safe to show (it never carries secrets)."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message
