"""Small git helpers used by the flows (injectable runner for tests)."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[[Sequence[str]], object]


def seed_title_commit(title: str, runner: Runner = subprocess.run) -> None:
    """Create an empty commit carrying the PR title.

    The PR-review version calc runs on the base ref, where the PR's own commits
    aren't present yet. Seeding the title as a conventional commit lets
    semantic-release preview what the merge would release.
    """
    runner(["git", "config", "user.email", "pr-review-bot@users.noreply.github.com"])
    runner(["git", "config", "user.name", "pr-review-bot"])
    runner(["git", "commit", "--allow-empty", "-m", title])
