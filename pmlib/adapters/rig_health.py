"""The rig-health adapter — turn `rig status` into a first-class health signal.

Research #255 found that `rig status` can FAIL on global-config schema skew (an installed rig
rejecting a newer key like ``harness.kinds``), and that a health check must treat a "tool/config
mismatch" as a FIRST-CLASS state rather than a silent failure. ``interpret_rig_status`` (PURE)
classifies a run into ok / config-drift / error; ``check_rig_health`` (EFFECTFUL) runs `rig status`.
"""

from __future__ import annotations

from dataclasses import dataclass

# Substrings that signal a config/SCHEMA mismatch — rig couldn't even run (the research #255
# concern: an installed rig rejecting a newer global-config key like ``harness.kinds``). Kept
# specific to a key-rejection message; only consulted on a NON-ZERO exit (see interpret_rig_status).
_DRIFT_MARKERS = ("unknown key", "reject", "unexpected key", "unknown field",
                  "unrecognized key", "not a valid key", "invalid config key")

# Substrings that prove rig RAN and produced a normal status report. A non-zero exit alongside
# these is rig's ordinary "disk drift detected" signal, NOT a tool-health failure — pm can still
# trust rig, so this is ``ok`` (disk drift is a separate concern from config/schema health).
_RAN_OK_MARKERS = ("config layers", "areas rig manages", "in sync")


@dataclass(frozen=True)
class HealthStatus:
    """A rig-health verdict. ``status`` is ok | config-drift | error."""

    status: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail}


def interpret_rig_status(returncode: int, stdout: str, stderr: str) -> HealthStatus:
    """Classify a `rig status` run: ok | config-drift | error.

    A zero exit is always ``ok`` (a config/schema skew makes rig FAIL to run, so it never exits 0).
    On a NON-ZERO exit: a schema-skew marker means ``config-drift`` (the tool/config mismatch); a
    normal status report present means rig merely reported disk drift, still ``ok``; otherwise it is
    a genuine ``error``. Requiring a non-zero exit keeps a healthy report that happens to contain a
    marker word from being misclassified.
    """
    if returncode == 0:
        return HealthStatus("ok", (stdout or "").strip()[:200])
    blob = f"{stdout}\n{stderr}".lower()
    detail = (stderr or stdout or "").strip()[:300]
    if any(marker in blob for marker in _DRIFT_MARKERS):
        return HealthStatus("config-drift", detail)
    if any(marker in blob for marker in _RAN_OK_MARKERS):
        return HealthStatus("ok", "rig ran and reported disk drift (non-zero exit is its signal)")
    return HealthStatus("error", detail)


def check_rig_health(repo_dir: str = "") -> HealthStatus:
    """Run ``rig status`` and interpret it. Effectful and lazy.

    Raises ``RuntimeError`` if ``rig`` is missing (so the CLI renders a structured error). A
    non-zero exit is NOT an exception here — it is the very signal (config-drift) we classify.
    """
    import subprocess

    cmd = ["rig", "status"]
    if repo_dir:
        cmd += ["-C", repo_dir]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RuntimeError("`rig` CLI not found on PATH — install rig-cli to check health") from exc
    return interpret_rig_status(proc.returncode, proc.stdout, proc.stderr)
