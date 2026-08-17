from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

StorageBackend = Literal["local", "s3", "azure", "gcs"]


class DatabaseSettings(BaseSettings):
    url: str = "sqlite+aiosqlite:///./conda-server.db"
    echo: bool = False
    pool_size: int = 5
    max_overflow: int = 10


class StorageSettings(BaseSettings):
    backend: StorageBackend = "local"
    url: str = "./data"
    region: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = None
    endpoint: str | None = None
    presign_ttl_seconds: int = 900


class OIDCSettings(BaseSettings):
    issuer: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    scopes: list[str] = Field(default_factory=lambda: ["openid", "email", "profile"])


class AuthSettings(BaseSettings):
    session_secret: str = "change-me-in-production"
    session_https_only: bool = True
    oidc: OIDCSettings = Field(default_factory=OIDCSettings)
    initial_admins: list[str] = Field(default_factory=list)


class LoggingSettings(BaseSettings):
    level: str = "INFO"
    format: Literal["json", "console"] = "json"


class UploadSettings(BaseSettings):
    """Size caps for the admin upload endpoint.

    Defaults target "a conda package with reasonable prebuilt binaries".
    PyTorch-sized archives (~2 GiB) cross the per-file cap deliberately
    — if you need those, raise the limit explicitly rather than letting
    an unbounded upload fill Garage by accident.
    """

    # Per-file cap. Rejected with a per-file "error" in the response.
    max_file_bytes: int = 1 * 1024 * 1024 * 1024  # 1 GiB
    # Whole-request cap. Rejected with 413 before the first file spools.
    max_total_bytes: int = 4 * 1024 * 1024 * 1024  # 4 GiB


class CleanupSettings(BaseSettings):
    """Periodic in-process maintenance sweeps.

    Runs as a background asyncio task in the API pod (not a separate
    worker). Intentionally only touches things the app itself created
    — orphan S3 multipart uploads belong to the bucket and should be
    handled with a bucket lifecycle rule, not an app-level cron.
    """

    # How long terminal ImportJob rows live before they're swept.
    # 0 disables the sweep (rows accumulate forever — useful for audit
    # builds that prefer to keep the full history).
    import_job_ttl_days: int = 30
    # Sweep cadence. The sweep is cheap (single DELETE statement), so
    # an hourly cycle is fine even if there's nothing to do.
    interval_seconds: int = 3600


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CONDA_SERVER_",
        env_nested_delimiter="__",
        toml_file=("conda-server.toml",),
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8000
    base_url: str = "http://localhost:8000"

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    cleanup: CleanupSettings = Field(default_factory=CleanupSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None


def resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()
