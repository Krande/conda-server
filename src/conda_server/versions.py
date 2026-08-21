"""Conda version ordering.

Version strings do **not** sort correctly as plain strings: ``"0.10.0"``
sorts before ``"0.9.0"`` lexicographically, which is backwards, and the
real conda spec adds epochs (``1!1.0``), ``.post`` / ``.dev`` suffixes,
and the rule that trailing zero segments are insignificant (``2.31`` and
``2.31.0`` are the *same* version).

Rather than re-derive those rules, this delegates to ``rattler.Version``
— the same rattler the solver and archive reader already depend on, and
the reference implementation of the ordering conda itself uses. All this
module adds is a cache, a total order for strings rattler can't parse,
and the artifact-level tiebreak (build number, subdir, build).
"""

from __future__ import annotations

import functools
from collections.abc import Iterable
from typing import Any, Protocol

import rattler


class _Artifact(Protocol):
    """The subset of a package-version row this module orders by.

    Structural, not nominal: both the ``PackageVersion`` ORM model and
    the mirror listing's filename-derived dataclass satisfy it.
    """

    version: str
    build: str
    build_number: int
    subdir: str


@functools.lru_cache(maxsize=8192)
def _parse(raw: str) -> rattler.Version | None:
    """Parsed version, or None when rattler rejects the string.

    Cached because a channel page re-compares the same few hundred
    strings O(n log n) times, and parsing is the expensive half.
    """
    try:
        return rattler.Version(raw)
    except Exception:
        return None


def compare_versions(a: str, b: str) -> int:
    """Three-way compare two version strings, conda-style.

    Returns -1 / 0 / 1. Note that 0 means *equivalent*, not identical:
    ``compare_versions("2.31", "2.31.0") == 0`` because trailing zero
    segments carry no meaning in conda's ordering.

    Strings rattler can't parse sort below every parseable version and
    fall back to plain string order among themselves, so the result is
    always a deterministic total order — a malformed row in the DB
    degrades one row's placement instead of raising mid-request.
    """
    parsed_a = _parse(a)
    parsed_b = _parse(b)
    if parsed_a is None or parsed_b is None:
        if parsed_a is None and parsed_b is None:
            return (a > b) - (a < b)
        return -1 if parsed_a is None else 1
    if parsed_a == parsed_b:
        return 0
    return -1 if parsed_a < parsed_b else 1


#: ``sorted``/``list.sort`` key that orders version *strings* ascending.
version_key: Any = functools.cmp_to_key(compare_versions)


def sort_versions[ArtifactT: _Artifact](versions: Iterable[ArtifactT]) -> list[ArtifactT]:
    """Order artifacts newest-version-first.

    Full ordering: version descending, then build number descending
    (a rebuild supersedes its predecessor), then subdir and build string
    ascending so the row order is stable across requests instead of
    following whatever the DB happened to return.

    Implemented as a chain of sorts from least to most significant —
    Python's sort is stable, so each pass preserves the previous one's
    ordering within ties. That keeps the mixed asc/desc directions
    readable without a reverse-comparison wrapper.
    """
    ordered = sorted(versions, key=lambda v: (v.subdir, v.build))
    ordered.sort(key=lambda v: v.build_number, reverse=True)
    ordered.sort(key=lambda v: version_key(v.version), reverse=True)
    return ordered


def version_ranks(ordered: Iterable[_Artifact]) -> list[int]:
    """Dense 0-based rank per artifact, given the output of ``sort_versions``.

    Rank 0 is the newest version; artifacts sharing a version (different
    builds or subdirs) share a rank, as do equivalent spellings of the
    same version (``2.31`` / ``2.31.0``).

    This exists so the browser can re-sort the table by version without
    shipping conda's ordering rules to TypeScript: the client sorts on
    this integer and gets the exact ordering the server computed.
    """
    ranks: list[int] = []
    rank = -1
    previous: str | None = None
    for artifact in ordered:
        if previous is None or compare_versions(previous, artifact.version) != 0:
            rank += 1
            previous = artifact.version
        ranks.append(rank)
    return ranks
