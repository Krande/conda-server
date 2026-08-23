"""Read a conda archive's ``info/about.json``.

``info/index.json`` — the only archive member this server used to read —
is the repodata record: name, version, build, subdir, depends. The
human-facing metadata a package page wants (documentation URL, homepage,
repository, summary, long description) lives in a *different* member,
``info/about.json``, which never reaches repodata.json and therefore
never reaches the database via the indexer's normal path.

Two things make this more than a one-liner:

* **There is no rattler helper.** ``rattler.IndexJson`` has
  ``from_package_archive``; ``rattler.AboutJson`` has only ``from_str`` /
  ``from_path`` / ``from_package_directory``, none of which take an
  archive. So the member has to be pulled out here, once per format.
* **The two archive formats differ.** A ``.conda`` is a zip whose
  metadata lives in a single ``info-*.tar.zst`` member — cheap, because
  the zip central directory lets us seek straight to a few KB and skip
  the payload member entirely. A ``.tar.bz2`` is one solid bz2 stream, so
  reaching ``info/about.json`` means decompressing until we hit it; we
  stop the moment we do.

``about.json`` is optional and routinely partial — plenty of recipes set
``home`` and nothing else, and archives built by older tooling omit the
member entirely. Every failure mode here (member absent, unreadable
archive, malformed JSON, field missing) resolves to "no metadata", never
to an exception, because the callers are an upload handler and a channel
indexer where a missing docs link must not fail the operation.

There are two ways in. ``read_package_about`` takes a path and is what
the upload handler uses, because the upload is already a file on disk.
``read_conda_about_ranged`` takes a callable that returns byte ranges of
an object in storage, and is what the indexer uses: it never brings the
archive down at all, which matters because a container's ephemeral disk
is a small, shared, hard limit and an indexing pass touches every newly
published artifact. That path is ``.conda``-only by construction — see
its docstring.
"""

from __future__ import annotations

import errno
import io
import os
import tarfile
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rattler
import zstandard

from conda_server.logging import get_logger

log = get_logger(__name__)

_ABOUT_MEMBER = "info/about.json"

#: Long descriptions are occasionally a whole README. The column, the
#: JSON response and the page's lead paragraph all want a paragraph, not
#: a document, so the stored value is clipped. Summaries are short by
#: convention but clipped too, defensively.
MAX_DESCRIPTION_CHARS = 8192
MAX_SUMMARY_CHARS = 2048

#: Guard on the info tarball inside a ``.conda``. A legitimate one is a
#: few KB; anything past this is a malformed or hostile archive and we
#: decline rather than keep decompressing it.
_MAX_INFO_TAR_BYTES = 32 * 1024 * 1024

#: How much of a ``.conda``'s tail the first ranged read pulls. It has to
#: cover the End of Central Directory record, whatever Zip64 records sit
#: in front of it, and the whole central directory. A conda archive has
#: three members, so its directory is a few hundred bytes and this is
#: already generous; the window grows on a miss rather than being sized
#: for the worst case, because the worst case (a 64 KiB archive comment)
#: does not happen and paying for it on every read would undo the saving.
_ZIP_TAIL_WINDOW = 64 * 1024

#: Ceiling on that growth. Past this the file is not laid out like
#: anything conda-build produces, and reading more of it is a worse deal
#: than going without its metadata.
_ZIP_TAIL_WINDOW_MAX = 1024 * 1024

#: Slack added when fetching the info member, to cover the local file
#: header in front of the data: 30 fixed bytes, the file name, and an
#: extra field whose length is stored in that header and need not match
#: the central directory's copy — so it cannot be computed in advance.
_LOCAL_HEADER_SLACK = 8 * 1024

#: A callable returning ``(start, length)`` of an object as bytes.
#: ``conda_server.storage.Storage.get_range`` adapted to one key.
RangeReader = Callable[[int, int], Awaitable[bytes]]


class ArchiveFetchError(Exception):
    """The archive's bytes could not be read out of storage.

    Kept distinct from every other failure in this module because the
    consequences differ: a malformed archive is a fact about the archive
    and is remembered as "no metadata", whereas a failed fetch is
    transient and must be retried on a later pass. A ``RangeReader``
    raises this to say which one happened.
    """


@dataclass(frozen=True)
class PackageAbout:
    """The subset of ``about.json`` this server stores and renders.

    ``home`` is the project homepage and ``dev_url`` the source
    repository — conda keeps them apart and so does the package page, so
    a project whose docs, site and repo live on three different hosts
    links to all three rather than picking one.

    All fields are optional. ``empty()`` is what an archive with no
    ``about.json`` produces, and it is a perfectly normal outcome.
    """

    doc_url: str | None = None
    home: str | None = None
    dev_url: str | None = None
    summary: str | None = None
    description: str | None = None

    @classmethod
    def empty(cls) -> PackageAbout:
        return cls()

    def is_empty(self) -> bool:
        """True when the archive supplied nothing worth displaying.

        Used to decide whether a version can act as the package's
        metadata source; see ``conda_server.api.packages``.
        """
        return not any((self.doc_url, self.home, self.dev_url, self.summary, self.description))


def read_package_about(archive_path: str | Path) -> PackageAbout:
    """Extract ``info/about.json`` from a ``.conda`` / ``.tar.bz2`` archive.

    Returns an empty ``PackageAbout`` when the member is absent, the
    archive is unreadable, or the JSON does not parse. Never raises —
    callers treat "no metadata" and "could not read metadata" the same
    way, and neither is a reason to reject a package.
    """
    path = Path(archive_path)
    try:
        if path.name.endswith(".conda"):
            raw = _about_bytes_from_conda(path)
        elif path.name.endswith(".tar.bz2"):
            raw = _about_bytes_from_tar_bz2(path)
        else:
            return PackageAbout.empty()
    except Exception as exc:  # unreadable archive — not fatal, just no links
        log.debug("about.read_failed", path=path.name, error=str(exc))
        return PackageAbout.empty()

    if raw is None:
        return PackageAbout.empty()
    return parse_about_json(raw)


async def read_conda_about_ranged(read_range: RangeReader, size: int) -> PackageAbout:
    """Extract ``info/about.json`` from a ``.conda`` using byte ranges only.

    ``read_range(start, length)`` returns that slice of the object;
    ``size`` is the object's full length, which the caller already knows
    from a ``head``. Nothing is written to disk and nothing larger than
    the two windows below is held in memory, so the cost is flat in the
    size of the package: a 300 MB artifact and a 300 KB one both cost
    roughly 64 KB plus the info member.

    **This works for ``.conda`` and cannot work for ``.tar.bz2``.** A
    ``.conda`` is a zip, and a zip records where each member lives in a
    central directory at the *tail* — read the tail, learn the offset,
    read the member. A ``.tar.bz2`` is one solid bz2 stream with no index
    at all: the only way to reach a member is to decompress everything in
    front of it, which a ranged read cannot shortcut. Callers keep the
    spooling path for that format; see ``conda_server.indexer``.

    Raises ``ArchiveFetchError`` — and only that — when the bytes could
    not be read, so the caller can tell a transient storage problem from
    an archive that genuinely has no usable metadata. Every other
    failure, including a file that is not a zip at all, resolves to an
    empty result like the rest of this module.
    """
    if size <= 0:
        return PackageAbout.empty()
    try:
        raw = await _about_bytes_from_conda_ranges(read_range, size)
    except ArchiveFetchError:
        raise
    except Exception as exc:
        log.debug("about.ranged_read_failed", size=size, error=str(exc))
        return PackageAbout.empty()

    if raw is None:
        return PackageAbout.empty()
    return parse_about_json(raw)


def parse_about_json(raw: bytes) -> PackageAbout:
    """Map raw ``about.json`` bytes onto ``PackageAbout``.

    Parsing goes through ``rattler.AboutJson`` rather than a bare
    ``json.loads`` so the URL fields get the same validation and
    normalisation the rest of the conda ecosystem applies: a recipe that
    wrote ``home: example.com`` (no scheme) yields *nothing* rather than
    an href the browser would resolve as a relative path. That is exactly
    the "must not render a dead link" behaviour the page needs, and it is
    free here.

    rattler models the three URL fields as *lists* — a recipe may name
    several homepages — while the page has room for one link each, so we
    take the first and drop the rest.
    """
    try:
        about = rattler.AboutJson.from_str(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        log.debug("about.parse_failed", error=str(exc))
        return PackageAbout.empty()

    return PackageAbout(
        doc_url=_first_url(getattr(about, "doc_url", None)),
        home=_first_url(getattr(about, "home", None)),
        dev_url=_first_url(getattr(about, "dev_url", None)),
        summary=_clip(getattr(about, "summary", None), MAX_SUMMARY_CHARS),
        description=_clip(getattr(about, "description", None), MAX_DESCRIPTION_CHARS),
    )


def _first_url(value: object) -> str | None:
    """First entry of a rattler URL list, as a plain string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, list | tuple):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return None
    text = str(value).strip()
    return text or None


def _clip(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _about_bytes_from_conda(path: Path) -> bytes | None:
    """Pull ``info/about.json`` out of a ``.conda`` (zip of tar.zst parts).

    Only the ``info-*.tar.zst`` member is touched. The payload member
    (``pkg-*.tar.zst``), which is the entire package and can be hundreds
    of megabytes, is never read — the zip central directory means we seek
    past it.
    """
    with zipfile.ZipFile(path) as zf:
        info_member = _info_member_name(zf)
        if info_member is None:
            return None
        return _about_from_zip_member(zf, info_member)


def _info_member_name(zf: zipfile.ZipFile) -> str | None:
    """Name of the ``info-*.tar.zst`` member, or None if there isn't one."""
    return next(
        (n for n in zf.namelist() if n.startswith("info-") and n.endswith(".tar.zst")),
        None,
    )


def _about_from_zip_member(zf: zipfile.ZipFile, member: str) -> bytes | None:
    """Decompress one ``info-*.tar.zst`` member and read about.json out of it."""
    decompressor = zstandard.ZstdDecompressor()
    # Streaming mode ("r|"): the info tarball is small and read once,
    # front to back, so there is no reason to materialise it for
    # random access.
    with (
        zf.open(member) as compressed,
        decompressor.stream_reader(compressed) as reader,
        tarfile.open(fileobj=reader, mode="r|") as tf,
    ):
        return _about_from_tar(tf, limit=_MAX_INFO_TAR_BYTES)


async def _about_bytes_from_conda_ranges(read_range: RangeReader, size: int) -> bytes | None:
    """Two ranged reads: the zip's tail, then the info member it points at.

    The first read has to be a guess, because how far back the central
    directory starts is only knowable once the End of Central Directory
    record at the very end has been parsed. Guessing 64 KiB and widening
    on a miss costs one extra read in a case that does not arise in
    practice, whereas guessing the format's maximum would cost 64 KiB of
    waste on every single archive.
    """
    window = min(size, _ZIP_TAIL_WINDOW)
    while True:
        tail_start = size - window
        tail = await _fetch(read_range, tail_start, window)
        try:
            entry = _locate_info_member(size, [(tail_start, tail)])
            break
        except _MissingWindow:
            if window >= min(size, _ZIP_TAIL_WINDOW_MAX):
                raise
            window = min(size, window * 4)

    if entry is None:
        return None

    # The central directory gives the offset of the member's local header
    # and its compressed size, but not the header's own length, so the
    # fetch is bounded rather than exact. Clamped to the object because a
    # backend may refuse a range that starts past the end.
    member_start = entry.header_offset
    member_length = min(
        size - member_start,
        len(entry.filename.encode("utf-8")) + entry.compress_size + _LOCAL_HEADER_SLACK,
    )
    member = await _fetch(read_range, member_start, member_length)

    windows = [(tail_start, tail), (member_start, member)]
    with zipfile.ZipFile(_WindowedReader(size, windows)) as zf:
        return _about_from_zip_member(zf, entry.filename)


async def _fetch(read_range: RangeReader, start: int, length: int) -> bytes:
    """Range-read, mapping any storage failure onto ``ArchiveFetchError``.

    Wrapped here rather than left to each caller so that a failure to
    fetch can never be mistaken for a failure to parse — the two are
    remembered differently.
    """
    try:
        return await read_range(start, length)
    except ArchiveFetchError:
        raise
    except Exception as exc:
        raise ArchiveFetchError(str(exc)) from exc


def _locate_info_member(size: int, windows: Sequence[tuple[int, bytes]]) -> zipfile.ZipInfo | None:
    """Find the info member's directory entry from the fetched tail alone.

    Parsing goes through ``zipfile`` rather than reading the End of
    Central Directory by hand: it already handles archive comments, Zip64
    and the rest, and it only touches bytes the tail window holds — a
    read outside them raises ``_MissingWindow`` instead of quietly
    returning short data.
    """
    with zipfile.ZipFile(_WindowedReader(size, windows)) as zf:
        name = _info_member_name(zf)
        return zf.getinfo(name) if name is not None else None


class _MissingWindow(Exception):
    """A read landed outside the byte ranges that were fetched."""


class _WindowedReader(io.RawIOBase):
    """A seekable read-only file over a few prefetched slices of an object.

    ``zipfile`` wants random access and the entire point here is to fetch
    kilobytes rather than megabytes, so the object is presented at its
    true length while only the fetched ranges hold data. A read anywhere
    else raises ``_MissingWindow``, which the caller answers by widening
    the fetch — the alternative, returning short or zero-filled data,
    would surface as a corrupt-archive error and hide the real cause.

    Seeking matches a real file rather than being lenient about it: a
    seek before position zero raises ``OSError``, because that is what
    ``zipfile`` catches when it probes a file too short to hold the
    archive comment it is looking for.
    """

    def __init__(self, size: int, windows: Sequence[tuple[int, bytes]]) -> None:
        self._size = size
        self._windows = sorted(windows)
        self._pos = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = self._pos + offset
        elif whence == os.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence!r}")
        if target < 0:
            raise OSError(errno.EINVAL, "negative seek position")
        self._pos = target
        return target

    def readinto(self, buffer: Any) -> int:
        want = min(len(buffer), max(self._size - self._pos, 0))
        filled = 0
        while filled < want:
            chunk = self._window_slice(self._pos + filled, want - filled)
            buffer[filled : filled + len(chunk)] = chunk
            filled += len(chunk)
        self._pos += filled
        return filled

    def _window_slice(self, start: int, length: int) -> bytes:
        for offset, data in self._windows:
            if offset <= start < offset + len(data):
                end = min(start + length, offset + len(data))
                return data[start - offset : end - offset]
        raise _MissingWindow(start)


def _about_bytes_from_tar_bz2(path: Path) -> bytes | None:
    """Pull ``info/about.json`` out of a legacy ``.tar.bz2`` archive.

    bz2 is a solid stream with no index, so this decompresses forward
    until the member turns up. ``_about_from_tar`` stops there, which in
    practice is early: conda-build writes ``info/`` entries first.
    """
    with tarfile.open(path, mode="r|bz2") as tf:
        return _about_from_tar(tf, limit=None)


def _about_from_tar(tf: tarfile.TarFile, *, limit: int | None) -> bytes | None:
    """Return the ``info/about.json`` member's bytes, or None.

    Iterates rather than calling ``getmember`` because both callers open
    the tar in streaming mode, where the member index does not exist
    until it has been read past.
    """
    consumed = 0
    for member in tf:
        if limit is not None:
            consumed += max(member.size, 0)
            if consumed > limit:
                raise ValueError("info tarball is implausibly large")
        if member.name.lstrip("./") != _ABOUT_MEMBER or not member.isfile():
            continue
        extracted = tf.extractfile(member)
        if extracted is None:
            return None
        return extracted.read()
    return None
