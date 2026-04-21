"""Environment-variable-driven application configuration for mailwatch.

Loads settings from the process environment (and optionally from a local
``.env`` file) using :mod:`pydantic-settings`. Credentials are wrapped in
:class:`~pydantic.SecretStr` so they do not leak through ``repr()`` or
``str()`` — the inadvertent-logging vector.

Consumers should obtain a single cached :class:`Settings` instance via
:func:`get_settings` (suitable for ``fastapi.Depends(get_settings)``).
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Validated application configuration.

    All fields map 1:1 to the names in ``.env.example``. The env loader is
    case-sensitive so only ``MAILER_ID`` (not ``mailer_id``) is accepted.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # USPS Mailer Identity
    MAILER_ID: int = Field(..., description="6 or 9 digit Mailer ID from USPS BCG")
    SRV_TYPE: int = Field(40, ge=0, le=999, description="IMb Service Type")
    BARCODE_ID: int = Field(0, ge=0, le=99, description="2-digit barcode ID")

    # USPS IV-MTR (BSG OAuth)
    BSG_USERNAME: str
    BSG_PASSWORD: SecretStr

    # USPS developer API
    USPS_NEWAPI_CUSTOMER_ID: str
    USPS_NEWAPI_CUSTOMER_SECRET: SecretStr

    # Session
    SESSION_KEY: SecretStr = Field(..., min_length=32)

    # Storage
    DB_PATH: Path = Field(Path("./mailwatch.db"))

    # Rate limiting
    RATE_LIMIT_PER_HOUR: int = Field(50, ge=1, le=10000)

    # IV-MTR pull-poll daemon (read by `python -m mailwatch.poll`)
    POLL_LOOKBACK_DAYS: int = Field(14, ge=1, le=365)

    # IV-MTR push feed source IP allowlist.
    # ``NoDecode`` disables pydantic-settings' default JSON-decoding for list
    # fields so the string form from env (``"a,b,c"``) reaches the
    # ``mode="before"`` validator below intact.
    USPS_FEED_CIDRS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["56.0.0.0/8"])

    @field_validator("MAILER_ID")
    @classmethod
    def _validate_mailer_id_width(cls, v: int) -> int:
        """USPS Mailer IDs are exactly 6 or 9 decimal digits."""
        digits = len(str(v))
        if digits not in (6, 9):
            raise ValueError(f"MAILER_ID must be 6 or 9 digits, got {digits} digits ({v!r})")
        return v

    @field_validator("USPS_FEED_CIDRS", mode="before")
    @classmethod
    def _parse_cidrs(cls, v: Any) -> Any:
        """Accept a comma-separated string (env-var form) or a real list."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("DB_PATH")
    @classmethod
    def _resolve_db_path(cls, v: Path) -> Path:
        """Resolve relative paths at validation time to a stable absolute path."""
        return v if v.is_absolute() else Path.cwd() / v


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide :class:`Settings` singleton.

    Cached so repeated ``Depends(get_settings)`` calls in FastAPI routes
    share one validated instance. Tests that mutate the environment should
    call ``get_settings.cache_clear()`` before and after.
    """
    return Settings()
