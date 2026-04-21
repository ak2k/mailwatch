"""Tests for :mod:`mailwatch.config`.

These tests drive the env-var loader exclusively via ``monkeypatch.setenv`` /
``monkeypatch.delenv``. They never read or write a real ``.env`` file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from mailwatch.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_REQUIRED_ENV: dict[str, str] = {
    "MAILER_ID": "123456789",
    "BSG_USERNAME": "bcg-user",
    "BSG_PASSWORD": "bcg-pass",
    "USPS_NEWAPI_CUSTOMER_ID": "oauth-client-id",
    "USPS_NEWAPI_CUSTOMER_SECRET": "oauth-client-secret",
    "SESSION_KEY": "a" * 32,
}

# Every env var the loader knows about — used to scrub the process env so
# tests are hermetic regardless of what the developer shell exports.
_ALL_ENV_KEYS: tuple[str, ...] = (
    "MAILER_ID",
    "SRV_TYPE",
    "BARCODE_ID",
    "BSG_USERNAME",
    "BSG_PASSWORD",
    "USPS_NEWAPI_CUSTOMER_ID",
    "USPS_NEWAPI_CUSTOMER_SECRET",
    "SESSION_KEY",
    "DB_PATH",
    "RATE_LIMIT_PER_HOUR",
    "USPS_FEED_CIDRS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Strip any real env vars + disable .env file loading for every test."""
    for key in _ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Anchor cwd to an empty tmp dir so stray .env in the repo cannot leak in.
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Any:
    """Ensure ``get_settings`` is not poisoned across tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_required(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set the minimum required env vars, optionally overriding some."""
    env = {**_REQUIRED_ENV, **overrides}
    for k, v in env.items():
        monkeypatch.setenv(k, v)


# ---------------------------------------------------------------------------
# Required-field validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    [
        "MAILER_ID",
        "BSG_USERNAME",
        "BSG_PASSWORD",
        "USPS_NEWAPI_CUSTOMER_ID",
        "USPS_NEWAPI_CUSTOMER_SECRET",
        "SESSION_KEY",
    ],
)
def test_missing_required_raises(monkeypatch: pytest.MonkeyPatch, missing: str) -> None:
    _set_required(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert missing in str(exc_info.value)


# ---------------------------------------------------------------------------
# MAILER_ID digit-width validator
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mid", ["123456", "123456789"])
def test_mailer_id_accepts_6_or_9_digits(monkeypatch: pytest.MonkeyPatch, mid: str) -> None:
    _set_required(monkeypatch, MAILER_ID=mid)
    settings = Settings()
    expected = int(mid)
    assert expected == settings.MAILER_ID


@pytest.mark.parametrize("mid", ["1234567", "12345", "1234567890"])
def test_mailer_id_rejects_other_widths(monkeypatch: pytest.MonkeyPatch, mid: str) -> None:
    _set_required(monkeypatch, MAILER_ID=mid)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "MAILER_ID" in str(exc_info.value)


def test_mailer_id_rejects_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch, MAILER_ID="not-a-number")
    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------------------
# USPS_FEED_CIDRS parser
# ---------------------------------------------------------------------------


def test_feed_cidrs_parses_comma_separated_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("USPS_FEED_CIDRS", "10.0.0.0/8, 192.168.0.0/16 ,56.0.0.0/8")
    settings = Settings()
    assert settings.USPS_FEED_CIDRS == [
        "10.0.0.0/8",
        "192.168.0.0/16",
        "56.0.0.0/8",
    ]


def test_feed_cidrs_accepts_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    # pydantic-settings recognises a JSON list passed as a plain value.
    settings = Settings(USPS_FEED_CIDRS=["10.0.0.0/8", "192.168.0.0/16"])
    assert settings.USPS_FEED_CIDRS == ["10.0.0.0/8", "192.168.0.0/16"]


def test_feed_cidrs_single_value_string(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("USPS_FEED_CIDRS", "56.0.0.0/8")
    settings = Settings()
    assert settings.USPS_FEED_CIDRS == ["56.0.0.0/8"]


# ---------------------------------------------------------------------------
# SESSION_KEY length
# ---------------------------------------------------------------------------


def test_session_key_below_min_length_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch, SESSION_KEY="a" * 31)
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "SESSION_KEY" in str(exc_info.value)


def test_session_key_exact_min_length_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch, SESSION_KEY="x" * 32)
    settings = Settings()
    assert settings.SESSION_KEY.get_secret_value() == "x" * 32


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_defaults_populated_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    settings = Settings()
    assert settings.SRV_TYPE == 40
    assert settings.BARCODE_ID == 0
    assert settings.RATE_LIMIT_PER_HOUR == 50
    assert settings.USPS_FEED_CIDRS == ["56.0.0.0/8"]
    # DB_PATH relative default resolves to cwd-anchored absolute path.
    assert settings.DB_PATH.is_absolute()
    assert settings.DB_PATH.name == "mailwatch.db"


def test_db_path_absolute_value_preserved(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "sub" / "custom.db"
    _set_required(monkeypatch)
    monkeypatch.setenv("DB_PATH", str(target))
    settings = Settings()
    observed = settings.DB_PATH
    assert observed == target


def test_bounds_enforced_on_numeric_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_required(monkeypatch)
    monkeypatch.setenv("RATE_LIMIT_PER_HOUR", "0")
    with pytest.raises(ValidationError):
        Settings()


# ---------------------------------------------------------------------------
# get_settings() caching
# ---------------------------------------------------------------------------


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    first = get_settings()
    second = get_settings()
    assert first is second


def test_get_settings_cache_clear_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_required(monkeypatch)
    first = get_settings()
    get_settings.cache_clear()
    second = get_settings()
    assert first is not second


# ---------------------------------------------------------------------------
# SecretStr leak prevention
# ---------------------------------------------------------------------------


def test_secrets_not_leaked_in_repr_or_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_password = "super-secret-bcg-password-xyz"
    sentinel_client = "super-secret-client-secret-xyz"
    sentinel_session = "s" * 40 + "-session-sentinel"
    _set_required(
        monkeypatch,
        BSG_PASSWORD=sentinel_password,
        USPS_NEWAPI_CUSTOMER_SECRET=sentinel_client,
        SESSION_KEY=sentinel_session,
    )
    settings = Settings()

    rendered = repr(settings) + "\n" + str(settings)
    assert sentinel_password not in rendered
    assert sentinel_client not in rendered
    assert sentinel_session not in rendered

    # The secrets are still retrievable via get_secret_value().
    assert settings.BSG_PASSWORD.get_secret_value() == sentinel_password
    assert settings.USPS_NEWAPI_CUSTOMER_SECRET.get_secret_value() == sentinel_client
    assert settings.SESSION_KEY.get_secret_value() == sentinel_session
    # And the wrapper type is SecretStr, not a raw string.
    assert isinstance(settings.BSG_PASSWORD, SecretStr)
