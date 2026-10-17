"""Settings read from the environment: the sign-in names before and after the vexouLuna rename."""

from __future__ import annotations

from pathlib import Path

import pytest

from doomtp_bot.config import Settings

AUTH_VARS = ("URL", "INTERNAL_URL", "CLIENT_ID", "CLIENT_SECRET", "CLIENT_SECRET_FILE")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SIGNIN_PROVIDER", raising=False)
    for name in AUTH_VARS:
        monkeypatch.delenv(f"VEX_AUTH_{name}", raising=False)
        monkeypatch.delenv(f"VEXOULZ_AUTH_{name}", raising=False)


def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]  # pydantic-settings' init-only option


@pytest.mark.parametrize("value", ["vex", "vexoulz"])
def test_either_provider_name_means_the_shared_sign_in(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("SIGNIN_PROVIDER", value)
    assert settings().signin_provider == "vex"


def test_twitch_is_the_default() -> None:
    assert settings().signin_provider == "twitch"


@pytest.mark.parametrize("prefix", ["VEX_AUTH_", "VEXOULZ_AUTH_"])
def test_either_env_prefix_is_read(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    monkeypatch.setenv(f"{prefix}URL", "https://auth.example.net")
    monkeypatch.setenv(f"{prefix}INTERNAL_URL", "http://auth:8000")
    monkeypatch.setenv(f"{prefix}CLIENT_ID", "dtp-test")
    monkeypatch.setenv(f"{prefix}CLIENT_SECRET", "s3cret")
    s = settings()
    assert s.vex_auth_url == "https://auth.example.net"
    assert s.vex_auth_internal_url == "http://auth:8000"
    assert s.vex_auth_client_id == "dtp-test"
    assert s.vex_auth_secret() == "s3cret"


def test_the_new_name_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VEX_AUTH_URL", "https://new.example.net")
    monkeypatch.setenv("VEXOULZ_AUTH_URL", "https://old.example.net")
    assert settings().vex_auth_url == "https://new.example.net"


def test_the_secret_file_under_either_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("VEXOULZ_AUTH_CLIENT_SECRET_FILE", str(secret))
    assert settings().vex_auth_secret() == "from-file"


def test_an_unknown_provider_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIGNIN_PROVIDER", "github")
    with pytest.raises(ValueError):
        settings()
