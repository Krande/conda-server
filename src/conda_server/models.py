from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conda_server.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    username: Mapped[str | None] = mapped_column(String(255), index=True)
    role: Mapped[str] = mapped_column(String(32), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    api_keys: Mapped[list[ApiKey]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="api_keys")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(String(1024))
    private: Mapped[bool] = mapped_column(Boolean, default=False)
    storage_prefix: Mapped[str] = mapped_column(String(512))
    repodata_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Mirror / proxy support. When set, requests for files not in local
    # storage are fetched from this upstream URL (no trailing slash), with
    # packages cached forever and repodata cached for mirror_cache_seconds.
    mirror_url: Mapped[str | None] = mapped_column(String(512))
    mirror_cache_seconds: Mapped[int] = mapped_column(Integer, default=900)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    packages: Mapped[list[Package]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )
    members: Mapped[list[ChannelMember]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelMember(Base):
    """Per-channel ACL grant.

    Either ``user_id`` or ``group_name`` is set, never both — the CHECK
    constraint enforces it. Group grants are schema-reserved but not
    consulted by the current auth layer; wiring them up requires
    Authentik group claims in the OIDC token, which is a follow-up.
    """

    __tablename__ = "channel_members"
    __table_args__ = (
        UniqueConstraint("channel_id", "user_id", name="uq_channel_member_user"),
        UniqueConstraint("channel_id", "group_name", name="uq_channel_member_group"),
        CheckConstraint(
            "(user_id IS NULL) <> (group_name IS NULL)",
            name="ck_channel_member_principal_xor",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    group_name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16))  # reader | writer | owner
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    channel: Mapped[Channel] = relationship(back_populates="members")
    user: Mapped[User | None] = relationship()


class AuditLog(Base):
    """Append-only record of administrative actions.

    ``actor_id`` is nullable so system-initiated actions (scheduled jobs,
    future automation) can still land. ``actor_email`` is a denormalized
    snapshot — if the user row is later deleted, the audit trail still
    points at a human-readable identity. ``channel_name`` is the same
    kind of snapshot so deleting a channel doesn't break history.

    Metadata is a JSON blob for action-specific context (uploaded
    filename + size, removed member's email, etc.). Schema here is kept
    deliberately flexible — this table is for ops read-only review, not
    for application logic to join against.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    actor_email: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64), index=True)
    channel_name: Mapped[str | None] = mapped_column(String(128), index=True)
    target: Mapped[str | None] = mapped_column(String(512))
    # Arbitrary JSON payload — per-action details.
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )


class Package(Base):
    __tablename__ = "packages"
    __table_args__ = (UniqueConstraint("channel_id", "name", name="uq_package_channel_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str | None] = mapped_column(String(2048))

    channel: Mapped[Channel] = relationship(back_populates="packages")
    versions: Mapped[list[PackageVersion]] = relationship(
        back_populates="package", cascade="all, delete-orphan"
    )


class PackageVersion(Base):
    __tablename__ = "package_versions"
    __table_args__ = (
        UniqueConstraint(
            "package_id",
            "version",
            "build",
            "subdir",
            name="uq_packageversion_unique",
        ),
        # The about-metadata backfill's driving query is "versions in this
        # channel nobody has inspected yet". Without an index that walks
        # every row in the table on every batch and every progress count.
        # Indexing only the un-inspected rows keeps it proportional to the
        # work remaining rather than to the size of the channel, and the
        # index shrinks to nothing once a channel is fully backfilled.
        Index(
            "ix_package_versions_about_pending",
            "package_id",
            postgresql_where=text("about_fetched_at IS NULL"),
            sqlite_where=text("about_fetched_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(
        ForeignKey("packages.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[str] = mapped_column(String(64), index=True)
    build: Mapped[str] = mapped_column(String(128))
    build_number: Mapped[int] = mapped_column(Integer, default=0)
    subdir: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(512), index=True)
    sha256: Mapped[str | None] = mapped_column(String(64))
    md5: Mapped[str | None] = mapped_column(String(32))
    size: Mapped[int | None] = mapped_column(BigInteger)
    depends: Mapped[list[str]] = mapped_column(JSON, default=list)
    constrains: Mapped[list[str]] = mapped_column(JSON, default=list)
    package_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Origin of the bytes: the full upstream URL we pulled from when the
    # version landed via the import-from-upstream flow. Null for plain
    # admin uploads (those have no canonical upstream) and for mirror-
    # cached files (mirror channels don't materialise PackageVersion
    # rows). Plain string, no FK — we don't model "upstream channels"
    # as first-class entities.
    imported_from: Mapped[str | None] = mapped_column(String(1024))
    # --- info/about.json --------------------------------------------------
    # Human-facing metadata, read out of the archive rather than out of
    # repodata (repodata is built from info/index.json, which carries none
    # of it). These live on the *version* because that is where the data
    # is: every artifact ships its own about.json and a project can move
    # its docs between releases. The package page collapses them to one
    # value per package at read time; see conda_server.api.packages.
    # Every field is nullable — about.json is optional in a conda archive
    # and frequently partial.
    doc_url: Mapped[str | None] = mapped_column(String(1024))
    home: Mapped[str | None] = mapped_column(String(1024))
    dev_url: Mapped[str | None] = mapped_column(String(1024))
    summary: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    # When extraction was last *attempted* for this row — set whether or
    # not anything was found. Distinguishes "this archive has no
    # about.json" from "nobody has looked yet", which is what stops the
    # backfill command from re-downloading metadata-less archives on every
    # run.
    about_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    package: Mapped[Package] = relationship(back_populates="versions")


class ImportJob(Base):
    """Per-import progress + result so the UI can poll instead of holding
    a multi-minute HTTP request open. The `import_packages` endpoint
    returns a job id immediately; the actual fetch + store loop runs as
    a FastAPI background task and updates the row. The frontend polls
    the GET endpoint until status is terminal.

    Lifecycle: pending → running → (completed | failed). Rows aren't
    pruned automatically — the table is small (one row per import) and
    operators may want to look back. Add a TTL sweep later if it grows.
    """

    __tablename__ = "import_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    # SET NULL so deleting a user doesn't drop the audit-relevant row.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    upstream_url: Mapped[str] = mapped_column(String(512))
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending | running | completed | failed
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    written_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    current_filename: Mapped[str | None] = mapped_column(String(512))
    # Per-item outcome list, populated as the loop runs. Same shape as
    # the old synchronous endpoint's `results` payload so the UI can
    # render it once the job lands.
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaintenanceJob(Base):
    """A long-running housekeeping pass, tracked so the UI can poll it.

    Distinct from ``ImportJob``, which models one specific operation and
    requires an ``upstream_url``. This one is deliberately generic: the
    work is described by ``kind``, and the counters are the ones any
    "walk a set of rows and do something to each" sweep needs. Adding a
    second kind should mean a new runner and no schema change.

    Kinds in use:

    * ``about_backfill`` — open archives already in storage to read the
      ``info/about.json`` metadata for versions indexed before that
      metadata was captured.

    Lifecycle: pending → running → (completed | failed), same as
    ``ImportJob``, including the startup sweep that fails rows stranded
    by a restart. Runners are in-process asyncio tasks, not durable
    workers, so a row left ``running`` after a restart is a lie the
    lifespan hook corrects.
    """

    __tablename__ = "maintenance_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    # Every kind so far is per-channel. Nullable so a future
    # server-wide sweep can use the same table without a migration.
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), index=True
    )
    # SET NULL so deleting a user doesn't drop the audit-relevant row.
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", index=True
    )  # pending | running | completed | failed
    # How many rows the run intends to touch, learned when the runner
    # counts them. Zero until then, so the UI shows a spinner rather
    # than a bogus 0/0 bar.
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    # Subset of completed_count that yielded something worth showing.
    # For about_backfill most archives legitimately carry nothing, so
    # "600 read, 140 with metadata" is the honest summary and "600
    # completed" on its own reads like a much better result than it is.
    with_metadata_count: Mapped[int] = mapped_column(Integer, default=0)
    current_target: Mapped[str | None] = mapped_column(String(512))
    error: Mapped[str | None] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
