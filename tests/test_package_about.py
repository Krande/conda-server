"""Extracting ``info/about.json`` out of a conda archive.

The archives here are built for real — a ``.conda`` is a genuine zip of
zstd-compressed tarballs and a ``.tar.bz2`` a genuine bz2 tarball — rather
than mocked, because the whole point of the module under test is that it
navigates those two container formats correctly. A stub would test
nothing.

``about.json`` is optional in a conda archive and frequently only partly
filled in, so absence and partial fields are first-class cases, not edge
cases: they must produce nulls and keep going.
"""

from __future__ import annotations

import io
import json
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
import zstandard

from conda_server.package_about import (
    MAX_DESCRIPTION_CHARS,
    ArchiveFetchError,
    PackageAbout,
    RangeReader,
    read_conda_about_ranged,
    read_package_about,
)

FULL_ABOUT = {
    "home": "https://example.com/pkg-a",
    "dev_url": "https://example.com/pkg-a/src",
    "doc_url": "https://example.com/docs/pkg-a/",
    "summary": "A small example package.",
    "description": "A longer paragraph about the example package.",
    "license": "MIT",
}

INDEX_JSON = {
    "name": "pkg-a",
    "version": "1.0.0",
    "build": "h0",
    "build_number": 0,
    "subdir": "linux-64",
    "depends": [],
}


def _info_tar_bytes(about: dict | None) -> bytes:
    """A conda ``info/`` tarball, with or without an ``about.json``."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        _add(tf, "info/index.json", json.dumps(INDEX_JSON).encode())
        if about is not None:
            _add(tf, "info/about.json", json.dumps(about).encode())
    return buf.getvalue()


def _add(tf: tarfile.TarFile, name: str, payload: bytes) -> None:
    entry = tarfile.TarInfo(name=name)
    entry.size = len(payload)
    tf.addfile(entry, io.BytesIO(payload))


def make_conda(path: Path, about: dict | None, *, raw_about: bytes | None = None) -> Path:
    """Write a ``.conda``: a zip holding an info part and a payload part.

    ``raw_about`` bypasses JSON encoding so a test can plant bytes that
    do not parse.
    """
    if raw_about is not None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            _add(tf, "info/index.json", json.dumps(INDEX_JSON).encode())
            _add(tf, "info/about.json", raw_about)
        info_tar = buf.getvalue()
    else:
        info_tar = _info_tar_bytes(about)

    compressor = zstandard.ZstdCompressor()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tf:
        _add(tf, "lib/python3.12/site-packages/pkg_a/__init__.py", b"# payload\n")

    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("metadata.json", json.dumps({"conda_pkg_format_version": 2}))
        zf.writestr("info-pkg-a-1.0.0-h0.tar.zst", compressor.compress(info_tar))
        zf.writestr("pkg-pkg-a-1.0.0-h0.tar.zst", compressor.compress(payload.getvalue()))
    return path


def make_tar_bz2(path: Path, about: dict | None) -> Path:
    """Write a legacy ``.tar.bz2`` archive."""
    with tarfile.open(path, mode="w:bz2") as tf:
        _add(tf, "info/index.json", json.dumps(INDEX_JSON).encode())
        if about is not None:
            _add(tf, "info/about.json", json.dumps(about).encode())
        _add(tf, "lib/pkg_a.py", b"# payload\n")
    return path


# --- .conda ------------------------------------------------------------


def test_conda_with_full_about(tmp_path):
    archive = make_conda(tmp_path / "pkg-a-1.0.0-h0.conda", FULL_ABOUT)

    about = read_package_about(archive)

    assert about.doc_url == "https://example.com/docs/pkg-a/"
    assert about.summary == "A small example package."
    assert about.description == "A longer paragraph about the example package."
    # rattler normalises URLs, so compare on the part that matters rather
    # than on an exact string it may add a trailing slash to.
    assert about.home is not None and about.home.startswith("https://example.com/pkg-a")
    assert about.dev_url is not None and about.dev_url.endswith("/src")
    assert not about.is_empty()


def test_conda_without_about_json_yields_empty(tmp_path):
    """An archive whose info/ has no about.json is normal, not an error."""
    archive = make_conda(tmp_path / "pkg-a-1.0.0-h0.conda", None)

    about = read_package_about(archive)

    assert about == PackageAbout.empty()
    assert about.is_empty()


def test_conda_with_partial_about_keeps_what_is_there(tmp_path):
    archive = make_conda(
        tmp_path / "pkg-a-1.0.0-h0.conda",
        {"home": "https://example.com/pkg-a", "summary": "Only these two."},
    )

    about = read_package_about(archive)

    assert about.summary == "Only these two."
    assert about.home is not None
    assert about.doc_url is None
    assert about.dev_url is None
    assert about.description is None
    assert not about.is_empty()


def test_url_without_a_scheme_is_dropped_rather_than_linked(tmp_path):
    """A bare hostname would render as a relative href — worse than nothing."""
    archive = make_conda(tmp_path / "pkg-a-1.0.0-h0.conda", {"home": "example.com/pkg-a"})

    assert read_package_about(archive).home is None


def test_long_description_is_clipped(tmp_path):
    archive = make_conda(
        tmp_path / "pkg-a-1.0.0-h0.conda",
        {"description": "x" * (MAX_DESCRIPTION_CHARS * 2)},
    )

    description = read_package_about(archive).description

    assert description is not None
    assert len(description) == MAX_DESCRIPTION_CHARS


# --- .tar.bz2 ----------------------------------------------------------


def test_tar_bz2_with_about(tmp_path):
    archive = make_tar_bz2(tmp_path / "pkg-a-1.0.0-h0.tar.bz2", FULL_ABOUT)

    about = read_package_about(archive)

    assert about.doc_url == "https://example.com/docs/pkg-a/"
    assert about.summary == "A small example package."


def test_tar_bz2_without_about(tmp_path):
    archive = make_tar_bz2(tmp_path / "pkg-a-1.0.0-h0.tar.bz2", None)

    assert read_package_about(archive).is_empty()


# --- failure modes -----------------------------------------------------


def test_malformed_about_json_does_not_raise(tmp_path):
    archive = make_conda(tmp_path / "pkg-a-1.0.0-h0.conda", None, raw_about=b"{not json")

    assert read_package_about(archive).is_empty()


def test_unreadable_archive_does_not_raise(tmp_path):
    archive = tmp_path / "pkg-a-1.0.0-h0.conda"
    archive.write_bytes(b"this is not a zip file")

    assert read_package_about(archive).is_empty()


def test_missing_file_does_not_raise(tmp_path):
    assert read_package_about(tmp_path / "absent-1.0.0-h0.conda").is_empty()


def test_unknown_extension_is_ignored(tmp_path):
    other = tmp_path / "pkg-a-1.0.0-h0.zip"
    other.write_bytes(b"whatever")

    assert read_package_about(other).is_empty()


# --- reading a .conda through byte ranges ------------------------------
#
# The property under test is not "it produces the same answer" — though
# it must — but "it produces the same answer without moving the archive".
# A package is mostly payload, and reading a few hundred bytes of
# metadata out of its tail used to mean spooling all of it to local disk.
# These tests assert on bytes fetched, because that is the part that was
# wrong.


def _blob_range_reader(blob: bytes) -> tuple[RangeReader, list[int]]:
    """A ``RangeReader`` over an in-memory archive, plus a fetch log."""
    fetched: list[int] = []

    async def read_range(start: int, length: int) -> bytes:
        chunk = blob[start : start + max(length, 0)]
        fetched.append(len(chunk))
        return chunk

    return read_range, fetched


def make_conda_blob(about: dict | None, *, payload_size: int = 0, comment: bytes = b"") -> bytes:
    """A ``.conda`` as bytes, with a payload member of a chosen size.

    The payload is random rather than zeros so it cannot compress down to
    nothing — these tests turn on there being a large member that must
    *not* be fetched.
    """
    compressor = zstandard.ZstdCompressor()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tf:
        _add(tf, "lib/python3.12/site-packages/pkg_a/blob.bin", os.urandom(payload_size))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("metadata.json", json.dumps({"conda_pkg_format_version": 2}))
        zf.writestr("info-pkg-a-1.0.0-h0.tar.zst", compressor.compress(_info_tar_bytes(about)))
        zf.writestr("pkg-pkg-a-1.0.0-h0.tar.zst", compressor.compress(payload.getvalue()))
        if comment:
            zf.comment = comment
    return buf.getvalue()


async def test_ranged_read_matches_the_path_based_read(tmp_path):
    """Same archive, two routes in, one answer."""
    blob = make_conda_blob(FULL_ABOUT)
    archive = tmp_path / "pkg-a-1.0.0-h0.conda"
    archive.write_bytes(blob)
    read_range, _ = _blob_range_reader(blob)

    assert await read_conda_about_ranged(read_range, len(blob)) == read_package_about(archive)


async def test_ranged_read_leaves_the_payload_alone():
    """The regression this path exists for.

    A 4 MiB archive must cost tens of KB to read, not 4 MiB. The bound is
    the tail window plus the info member and does not grow with the
    package, so it is asserted as an absolute number rather than a ratio.
    """
    blob = make_conda_blob(FULL_ABOUT, payload_size=4 * 1024 * 1024)
    read_range, fetched = _blob_range_reader(blob)

    about = await read_conda_about_ranged(read_range, len(blob))

    assert about.doc_url == "https://example.com/docs/pkg-a/"
    assert len(blob) > 4 * 1024 * 1024
    assert sum(fetched) < 128 * 1024, f"fetched {sum(fetched)} bytes of a {len(blob)}-byte archive"


async def test_ranged_read_widens_the_window_for_a_long_archive_comment():
    """A comment can push the central directory outside the first window.

    Zip keeps its index at the tail, so how far back to read is a guess
    until the very last record has been parsed. Getting the guess wrong
    has to cost another read, not the metadata.
    """
    # Bigger than the first tail window, so the comment genuinely pushes
    # the records zipfile needs out of it rather than the window happening
    # to cover the whole file.
    blob = make_conda_blob(FULL_ABOUT, payload_size=128 * 1024, comment=b"c" * 60_000)
    assert len(blob) > 64 * 1024
    read_range, fetched = _blob_range_reader(blob)

    about = await read_conda_about_ranged(read_range, len(blob))

    assert about.doc_url == "https://example.com/docs/pkg-a/"
    assert len(fetched) > 2, "the first tail window should have missed and been retried"


async def test_ranged_read_of_an_archive_without_about_json_is_empty():
    blob = make_conda_blob(None)
    read_range, _ = _blob_range_reader(blob)

    assert (await read_conda_about_ranged(read_range, len(blob))).is_empty()


async def test_ranged_read_of_a_non_zip_is_empty_not_an_error():
    """Same contract as the path-based read: a bad archive is not fatal."""
    blob = b"this is not a zip file" * 100
    read_range, _ = _blob_range_reader(blob)

    assert (await read_conda_about_ranged(read_range, len(blob))).is_empty()


async def test_ranged_read_of_an_empty_object_is_empty():
    read_range, fetched = _blob_range_reader(b"")

    assert (await read_conda_about_ranged(read_range, 0)).is_empty()
    assert fetched == [], "a zero-length object is not worth a request"


async def test_a_failed_fetch_is_distinguishable_from_missing_metadata():
    """The distinction the caller's retry logic is built on.

    "This archive has no about.json" is remembered; "storage would not
    give me the bytes" has to be retried. Collapsing the two would let a
    blip during indexing permanently blank a package's links.
    """

    async def read_range(start: int, length: int) -> bytes:
        raise ArchiveFetchError("storage said no")

    with pytest.raises(ArchiveFetchError):
        await read_conda_about_ranged(read_range, 4096)
