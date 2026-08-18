"""semantic-release wrappers.

Two entry points: a best-effort ``--noop --print`` used by the PR-review comment,
and the real ``version`` invocation used on merge. Both take an injectable runner
so tests never shell out.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Sequence

from .labels import BumpDecision

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+")


def _isolated_env() -> dict[str, str]:
    """Env with the Actions output handshake removed.

    On a release branch ``semantic-release version`` writes released/version/tag
    into GITHUB_OUTPUT, and its final ``tag=`` line has no trailing newline —
    which corrupts any output we append afterwards. Hiding GITHUB_OUTPUT (and the
    GITHUB_ACTIONS flag) from the child keeps it out of our step's outputs.
    """
    return {k: v for k, v in os.environ.items() if k not in ("GITHUB_OUTPUT", "GITHUB_ACTIONS")}


def _default_noop_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=_isolated_env())


def next_version_noop(
    config_file: str, flag: str | None, runner: Runner = _default_noop_runner
) -> str | None:
    """What version would semantic-release cut right now? None if none/unknown."""
    cmd = ["semantic-release", "-c", config_file, "--noop", "version", "--print"]
    if flag:
        cmd.append(flag)
    try:
        res = runner(cmd)
    except Exception:
        return None
    out = (res.stdout or "").strip()
    if not out:
        return None
    last = out.splitlines()[-1].strip()
    return last if _VERSION_RE.match(last) else None


def version_line_for(
    decision: BumpDecision, config_file: str, runner: Runner = _default_noop_runner
) -> str:
    """The informational '* … next version …' bullet for the PR comment."""
    if decision.multiple or not decision.release:
        return " * ✅ Skipping release (release-skip)"
    ver = next_version_noop(config_file, decision.flag, runner)
    if ver:
        return f' * ✅ Calculated next version: "{ver}"'
    return " * ℹ️ No release will be issued for these commits"


def _default_release_runner(cmd: Sequence[str]) -> subprocess.CompletedProcess:
    # Inherit the full env on purpose: the real release needs GH_TOKEN and the
    # SSH-authenticated `origin` remote to push the tag. Stream output.
    return subprocess.run(cmd)


def run_release(
    config_file: str, flag: str | None, runner: Runner = _default_release_runner
) -> int:
    """Actually bump the version, tag, push, and cut the GitHub Release.

    Returns the subprocess return code.
    """
    cmd = ["semantic-release", "-c", config_file, "version", "--changelog", "--vcs-release"]
    if flag:
        cmd.append(flag)
    res = runner(cmd)
    return getattr(res, "returncode", 0)
