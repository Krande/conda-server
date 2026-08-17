"""Read-only package listing for mirror channels.

Mirror channels don't write rows to the Package / PackageVersion tables —
upstream is the source of truth and we'd have to keep 100k+ rows in sync
per channel to mirror conda-forge-scale indexes. Instead, the listing is
derived from filenames already in object storage: ``<name>-<version>-
<build>.{conda,tar.bz2}`` is self-describing, and that's exactly the set
of packages that have passed through this server.

This deliberately does **not** read ``repodata.json`` — that file is
~400 MB for conda-forge linux-64 and ``json.loads`` on it allocates
multiple GB of Python objects, which OOM'd the pod the first time a user
opened the channel page after any meaningful mirror usage. Detail fields
we don't get from the filename (depends, size, sha256) simply aren't
shown for mirror channels; they live in the upstream repodata and the
client will fetch whatever it needs directly anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from conda_server.models import Channel
from conda_server.storage import Storage

_KNOWN_SUBDIRS = (
    "noarch",
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
)


@dataclass
class _PackageVersion:
    version: str
    build: str
    build_number: int
    subdir: str
    filename: str
    size: int | None = None
    sha256: str | None = None


@dataclass
class _Package:
    name: str
    description: str | None = None
    versions: list[_PackageVersion] = field(default_factory=list)


def parse_conda_filename(filename: str) -> tuple[str, str, str] | None:
    """Split ``<name>-<version>-<build>.<ext>`` into its three components.

    Returns None for anything that isn't a recognizable conda artifact.
    Shared with the upload validator in the channels API.
    """
    if filename.endswith(".conda"):
        stem = filename[:-6]
    elif filename.endswith(".tar.bz2"):
        stem = filename[:-8]
    else:
        return None
    # Split off build then version from the right — names may contain
    # hyphens (e.g. "libxml-python"), but versions and builds never do.
    parts = stem.rsplit("-", 2)
    if len(parts) != 3:
        return None
    name, version, build = parts
    if not (name and version and build):
        return None
    return name, version, build


def _build_number_from(build: str) -> int:
    """Last underscore-separated integer in a build string, else 0."""
    tail = build.rsplit("_", 1)[-1]
    return int(tail) if tail.isdigit() else 0


async def cached_packages(storage: Storage, channel: Channel) -> list[_Package]:
    """List cached packages grouped by name, parsed from storage filenames.

    No repodata read, no JSON parsing — just the object listing. Memory is
    bounded by the count of cached filenames (a few bytes each).
    """
    prefix = channel.storage_prefix.strip("/")
    merged: dict[str, _Package] = {}

    async for meta in storage.list(f"{prefix}/"):
        rel = meta.key[len(prefix) :].lstrip("/")
        parts = rel.split("/", 1)
        if len(parts) != 2:
            continue
        subdir, filename = parts
        if subdir not in _KNOWN_SUBDIRS:
            continue
        parsed = parse_conda_filename(filename)
        if parsed is None:
            continue
        name, version, build = parsed
        pkg = merged.setdefault(name, _Package(name=name))
        pkg.versions.append(
            _PackageVersion(
                version=version,
                build=build,
                build_number=_build_number_from(build),
                subdir=subdir,
                filename=filename,
                size=meta.size,
            )
        )

    for pkg in merged.values():
        pkg.versions.sort(key=lambda v: (v.version, v.build, v.subdir))

    return sorted(merged.values(), key=lambda p: p.name)


async def cached_package(storage: Storage, channel: Channel, name: str) -> _Package | None:
    for pkg in await cached_packages(storage, channel):
        if pkg.name == name:
            return pkg
    return None
