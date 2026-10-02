"""The Forgejo adapter — recognize a Forgejo PR link and read its state + CI conclusion.

``parse_forgejo_pr_url`` (PURE) recognizes a pull-request link on a known Forgejo host
(``https://git.hyperide.ai/<owner>/<repo>/pulls/<N>``). ``parse_forgejo_pr_facts`` (PURE) turns
the Forgejo API's PR object and combined commit status into the same reconcile fact
``{pr_url, pr_state, ci}`` that ``adapters/github.py`` builds from ``gh pr view``.
``fetch_forgejo_pr`` (EFFECTFUL) reads both from the Forgejo REST API.

Why a separate adapter: ``gh`` cannot read a Forgejo PR, and Forgejo spells the PR path
``/pulls/N`` where GitHub spells it ``/pull/N``. The repos and their PRs moved to Forgejo, so a
ticket's PR attachment is now usually a Forgejo link; without this, PR discovery matched nothing
and those items never reconciled.

Only hosts in ``FORGEJO_HOSTS`` count. An arbitrary ``https://<any host>/<a>/<b>/pulls/<n>`` link
on a ticket is not trusted as a forge the reconciler should query.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# Forgejo instances whose PR links pm reconciles. Lowercase host names.
FORGEJO_HOSTS: frozenset[str] = frozenset({"git.hyperide.ai"})

# Owner and repo names are restricted to Forgejo's own name charset, so a crafted link cannot
# smuggle `..` or an encoded path segment into the API URL built from it.
_NAME = r"[A-Za-z0-9_.-]+"
_FORGEJO_PR_URL_RE = re.compile(
    rf"^(https://([^/\s]+)/({_NAME})/({_NAME})/pulls/(\d+))/?(?:[?#].*)?$"
)

_TIMEOUT_SECONDS = 30  # a hung API call must not stall reconcile for every other PR


def parse_forgejo_pr_url(url: str) -> tuple[str, str, str, str, int] | None:
    """``(canonical_url, host, owner, repo, number)`` for a Forgejo PR link, else ``None``.

    The canonical url drops a trailing slash, query string and fragment. Sub-pages such as
    ``/pulls/1/files`` are not the PR itself and do not match.
    """
    m = _FORGEJO_PR_URL_RE.match(url or "")
    if not m:
        return None
    host, owner, repo = m.group(2).lower(), m.group(3), m.group(4)
    if host not in FORGEJO_HOSTS or owner in (".", "..") or repo in (".", ".."):
        return None
    number = int(m.group(5))
    # Rebuilt from the lowered host, so links differing only in host case share one url.
    return f"https://{host}/{owner}/{repo}/pulls/{number}", host, owner, repo, number


def is_forgejo_pr_url(url: str) -> bool:
    return parse_forgejo_pr_url(url) is not None


def parse_forgejo_pr_facts(pr_url: str, pr: dict, status: dict | None) -> dict:
    """A reconcile fact ``{pr_url, pr_state, ci}`` from Forgejo's PR object + combined status.

    ``status`` is ``GET /repos/{owner}/{repo}/commits/{sha}/status``; ``None`` means it could
    not be read, which is reported as ``pending`` (never a false green).
    """
    raw_state = str(pr.get("state") or "").lower()
    if pr.get("merged") is True:
        pr_state = "merged"
    else:
        # An unrecognized state is surfaced as-is rather than masked as "open".
        pr_state = raw_state or "open"
    return {"pr_url": pr_url, "pr_state": pr_state, "ci": _ci_conclusion(status)}


def _ci_conclusion(status: object) -> str:
    """Forgejo's combined commit status -> success | failure | pending | none."""
    if not isinstance(status, dict):  # unreadable (None) or malformed: never a false green
        return "pending"
    statuses = status.get("statuses")
    if statuses is None or statuses == []:  # no CI reported on the head commit at all
        return "none"
    if not isinstance(statuses, list) or not all(isinstance(x, dict) for x in statuses):
        return "pending"  # malformed: never a false green
    states = [str(x.get("status") or "").lower() for x in statuses]
    if any(st in ("failure", "error") for st in states):
        return "failure"
    combined = str(status.get("state") or "").lower()
    if combined == "success" and all(st == "success" for st in states):
        return "success"
    return "pending"  # pending, warning, blank or anything new: not a success


def _auth_headers(host: str, env: dict[str, str] | None = None) -> dict[str, str]:
    """``Authorization`` for ``host`` from ``$FORGEJO_TOKEN``, only when ``$FORGEJO_URL`` names
    that same host (task-cli's rule), so a token for one forge is never sent to another."""
    env = os.environ if env is None else env
    token = (env.get("FORGEJO_TOKEN") or "").strip()
    url_host = (urllib.parse.urlsplit(env.get("FORGEJO_URL") or "").hostname or "").lower()
    if token and url_host == host:
        return {"Authorization": f"token {token}"}
    return {}


Opener = Callable[..., Any]


def _get_json(url: str, host: str, opener: Opener) -> Any:
    """GET ``url`` as JSON, or ``None`` on any network, HTTP or decode error (best-effort)."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Unredirected: urllib copies ordinary headers onto a redirect, whatever its host, so a
    # token set there would follow a 3xx off the forge. These stay on this request only.
    for name, value in _auth_headers(host).items():
        req.add_unredirected_header(name, value)
    try:
        with opener(req, timeout=_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8") or "null")
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
        return None


def fetch_forgejo_pr(pr_url: str, opener: Opener | None = None) -> dict | None:
    """Read one Forgejo PR -> a reconcile fact, or ``None`` if the PR can't be read."""
    parsed = parse_forgejo_pr_url(pr_url)
    if parsed is None:
        return None
    canonical, host, owner, repo, number = parsed
    opener = opener or urllib.request.urlopen
    api = f"https://{host}/api/v1/repos/{owner}/{repo}"
    pr = _get_json(f"{api}/pulls/{number}", host, opener)
    if not isinstance(pr, dict):
        return None
    head = pr.get("head")
    sha = str(head.get("sha") or "") if isinstance(head, dict) else ""  # malformed: no CI read
    status = None
    if re.fullmatch(r"[0-9a-f]{7,64}", sha):
        got = _get_json(f"{api}/commits/{sha}/status", host, opener)
        status = got if isinstance(got, dict) else None
    # The fact carries the canonical url, so one PR is one evidence uri however it was linked.
    return parse_forgejo_pr_facts(canonical, pr, status)
