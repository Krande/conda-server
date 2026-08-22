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
"""

from __future__ import annotations

import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

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
        info_member = next(
            (n for n in zf.namelist() if n.startswith("info-") and n.endswith(".tar.zst")),
            None,
        )
        if info_member is None:
            return None
        decompressor = zstandard.ZstdDecompressor()
        # Streaming mode ("r|"): the info tarball is small and read once,
        # front to back, so there is no reason to materialise it for
        # random access.
        with (
            zf.open(info_member) as compressed,
            decompressor.stream_reader(compressed) as reader,
            tarfile.open(fileobj=reader, mode="r|") as tf,
        ):
            return _about_from_tar(tf, limit=_MAX_INFO_TAR_BYTES)


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
