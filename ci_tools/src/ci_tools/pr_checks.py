"""Conventional-commit PR-hygiene checks — pure, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

VALID_TITLE_PREFIXES: tuple[str, ...] = ("fix: ", "fix!: ", "feat: ", "feat!: ", "chore: ")


def title_ok(title: str) -> bool:
    """True if the PR title starts with an allowed conventional-commit prefix."""
    return title.startswith(VALID_TITLE_PREFIXES)


@dataclass(frozen=True)
class PrChecks:
    title_ok: bool
    label_ok: bool  # exactly one release-* label
    has_source_key: bool

    @property
    def ok(self) -> bool:
        # SOURCE_KEY is advisory only — it never fails the check.
        return self.title_ok and self.label_ok
