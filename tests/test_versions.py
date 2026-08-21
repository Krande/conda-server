"""Conda version ordering.

The bug these guard: the package page listed versions in upload order,
and the obvious fix — sorting the strings — is wrong in exactly the way
that produced the original garble ("0.10.0" < "0.9.0" as text).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from conda_server.versions import compare_versions, sort_versions, version_key, version_ranks


@dataclass
class _Artifact:
    version: str
    build: str = "h0"
    build_number: int = 0
    subdir: str = "linux-64"


def _versions(*specs: str) -> list[_Artifact]:
    return [_Artifact(version=v) for v in specs]


def _order(*specs: str) -> list[str]:
    return [a.version for a in sort_versions(_versions(*specs))]


def test_ten_sorts_after_nine():
    """The headline case: lexicographic order gets this backwards."""
    assert compare_versions("0.9.0", "0.10.0") == -1
    assert _order("0.10.0", "0.9.0", "0.2.0") == ["0.10.0", "0.9.0", "0.2.0"]


def test_patch_and_shorter_minor_order():
    assert compare_versions("2.1", "2.5.3") == -1
    assert compare_versions("2.5.3", "2.5.4") == -1
    assert _order("2.5.3", "2.1", "2.5.4") == ["2.5.4", "2.5.3", "2.1"]


def test_trailing_zero_segment_is_insignificant():
    """conda treats 2.31 and 2.31.0 as the same version, not adjacent ones."""
    assert compare_versions("2.31", "2.31.0") == 0


def test_epoch_beats_plain_version():
    assert compare_versions("1!1.0", "2.0") == 1


def test_dev_precedes_and_post_follows_the_release():
    assert compare_versions("1.0.dev1", "1.0") == -1
    assert compare_versions("1.0", "1.0.post1") == -1


def test_sort_is_newest_first():
    assert _order("1.0", "0.1", "10.0", "2.0")[0] == "10.0"


def test_equal_version_orders_by_build_number_descending():
    """Same version, different rebuilds — the later rebuild wins."""
    artifacts = [
        _Artifact(version="1.0", build="h_0", build_number=0),
        _Artifact(version="1.0", build="h_2", build_number=2),
        _Artifact(version="1.0", build="h_1", build_number=1),
    ]
    assert [a.build_number for a in sort_versions(artifacts)] == [2, 1, 0]


def test_equal_version_and_build_number_orders_by_subdir_then_build():
    """Deterministic row order regardless of what the DB hands back."""
    artifacts = [
        _Artifact(version="1.0", build="hb", subdir="win-64"),
        _Artifact(version="1.0", build="ha", subdir="win-64"),
        _Artifact(version="1.0", build="hz", subdir="linux-64"),
    ]
    ordered = [(a.subdir, a.build) for a in sort_versions(artifacts)]
    assert ordered == [("linux-64", "hz"), ("win-64", "ha"), ("win-64", "hb")]


def test_unparseable_version_sorts_last_without_raising():
    ordered = _order("1.0", "not a version", "2.0")
    assert ordered[:2] == ["2.0", "1.0"]
    assert ordered[-1] == "not a version"


def test_version_key_usable_with_sorted():
    assert sorted(["0.10.0", "0.9.0", "0.2.0"], key=version_key) == ["0.2.0", "0.9.0", "0.10.0"]


def test_version_ranks_are_dense_and_shared_across_builds():
    artifacts = [
        _Artifact(version="2.0", build="h_1", build_number=1),
        _Artifact(version="2.0", build="h_0", build_number=0),
        _Artifact(version="1.0"),
    ]
    ordered = sort_versions(artifacts)
    assert version_ranks(ordered) == [0, 0, 1]


def test_version_ranks_treat_equivalent_spellings_as_one_rank():
    ordered = sort_versions(_versions("2.31.0", "2.31", "1.0"))
    assert version_ranks(ordered) == [0, 0, 1]


@pytest.mark.parametrize("raw", ["", "   ", "..", "1.0-broken-*"])
def test_pathological_strings_do_not_raise(raw):
    assert compare_versions(raw, "1.0") in (-1, 0, 1)
