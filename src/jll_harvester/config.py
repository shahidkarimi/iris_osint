"""Runtime configuration.

Every value can be supplied through the environment (prefix ``IRIS_``) or a ``.env`` file.
No credentials live in source code; ``IRIS_DATABASE_URL`` is the only secret-bearing setting.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    pass


class GeocoderMode(StrEnum):
    """Which geocoding chain to use."""

    NOMINATIM = "nominatim"  # Nominatim first, offline gazetteer as fallback
    OFFLINE = "offline"  # bundled GeoNames gazetteer only (city/district precision)
    NONE = "none"  # never resolve coordinates


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="IRIS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity ---------------------------------------------------------------------------
    task_id: str = "IRIS-SIG-45"
    source_system: str = "jll_de_newsroom"

    # --- database -------------------------------------------------------------------------
    # e.g. postgresql://user:password@host:5432/dbname — required for any command touching the DB
    database_url: SecretStr | None = None

    # --- source ---------------------------------------------------------------------------
    # The endpoint named in the brief. It now answers 301 → jll.com/de-de/insights/market-dynamics,
    # which is checked on every run and recorded in the run ledger.
    brief_endpoint: str = "https://www.jll.de/de/trends-and-insights/research"
    base_url: str = "https://www.jll.com"
    robots_url: str = "https://www.jll.com/robots.txt"
    sitemap_url: str = "https://www.jll.com/de-de/sitemap.xml"
    # Path prefixes (inside the de-de sitemap) that make up the "research & transactions newsroom".
    section_prefixes: list[str] = Field(
        default_factory=lambda: ["/de-de/newsroom/", "/de-de/insights/market-dynamics/"]
    )

    # --- politeness / HTTP ----------------------------------------------------------------
    user_agent: str = "IRIS-SIG-45-Harvester/1.0 (research prototype; respects robots.txt)"
    request_delay_seconds: float = Field(default=1.5, ge=0.0)
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    max_retries: int = Field(default=4, ge=0, le=10)
    backoff_base_seconds: float = Field(default=2.0, ge=0.0)
    max_documents_per_run: int = Field(default=0, ge=0, description="0 = unlimited")
    raw_snapshot_dir: Path | None = Path("var/raw")

    # --- domain filtering -----------------------------------------------------------------
    big_box_min_sqm: int = Field(default=10_000, ge=1)
    min_classification_confidence: float = Field(default=0.5, ge=0.0, le=1.0)

    # --- geocoding ------------------------------------------------------------------------
    geocoder: GeocoderMode = GeocoderMode.NOMINATIM
    nominatim_url: str = "https://nominatim.openstreetmap.org"
    # Nominatim's usage policy asks for a contact address; supply it through the environment.
    nominatim_email: str | None = None
    nominatim_delay_seconds: float = Field(default=1.1, ge=1.0)

    # --- web ------------------------------------------------------------------------------
    web_basic_auth_user: str | None = None
    web_basic_auth_password: SecretStr | None = None
    map_style_url: str = "https://tiles.openfreemap.org/styles/positron"
    map_style_url_dark: str = "https://tiles.openfreemap.org/styles/dark"
    default_locale: str = "de"
    # git commit the image was built from (baked in by CI); /healthz reports it so deploys can be verified
    revision: str = "dev"

    @field_validator("section_prefixes", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [p.strip() for p in value.split(",") if p.strip()]
        return value

    @property
    def dsn(self) -> str:
        if self.database_url is None:
            raise ConfigError("IRIS_DATABASE_URL is not set (see .env.example)")
        return self.database_url.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
