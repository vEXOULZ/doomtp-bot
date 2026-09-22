"""Settings from environment, .env and Docker secrets (architecture §3.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from doomtp_bot.lang.parser import DEFAULT_PREFIX


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    twitch_client_id: str | None = None
    twitch_client_secret: SecretStr | None = None
    twitch_client_secret_file: Path | None = None
    # Expected Twitch user ID of the bot account; /auth/callback rejects tokens for any other account.
    twitch_bot_id: str | None = None

    # Comma-separated Twitch user IDs; parsed by `bot_owner_ids`.
    bot_owner_ids_csv: str = Field(default="", validation_alias="BOT_OWNER_IDS")

    data_dir: Path = Path("./data")

    # Postgres (ADR-0014). One database, schemas `bot` and `chatlog`. The password is kept out of the
    # URL so it can come from a Docker secret, the same way the Twitch and admin secrets do.
    database_url: str = "postgresql://doomtp@postgres:5432/doomtp"
    database_password: SecretStr | None = None
    database_password_file: Path | None = None

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    public_base_url: str = "http://localhost:8080"

    default_prefix: str = DEFAULT_PREFIX

    # Admin UI: the password is read from a file (a Docker secret) or the environment. Without one,
    # /admin is disabled rather than open.
    admin_password: SecretStr | None = None
    admin_password_file: Path | None = None
    history_provider_url: str = "https://recent-messages.robotty.de/api/v2"

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    @property
    def bot_owner_ids(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.bot_owner_ids_csv.split(",") if p.strip())

    def client_secret(self) -> str | None:
        """TWITCH_CLIENT_SECRET, or the contents of TWITCH_CLIENT_SECRET_FILE."""
        if self.twitch_client_secret is not None:
            return self.twitch_client_secret.get_secret_value()
        if self.twitch_client_secret_file and self.twitch_client_secret_file.is_file():
            return self.twitch_client_secret_file.read_text(encoding="utf-8").strip() or None
        return None

    def admin_password_value(self) -> str | None:
        """ADMIN_PASSWORD, or the contents of ADMIN_PASSWORD_FILE."""
        if self.admin_password is not None:
            return self.admin_password.get_secret_value() or None
        if self.admin_password_file and self.admin_password_file.is_file():
            return self.admin_password_file.read_text(encoding="utf-8").strip() or None
        return None

    def database_dsn(self) -> str:
        """DATABASE_URL with DATABASE_PASSWORD (or the contents of DATABASE_PASSWORD_FILE) spliced in.

        Returned rather than stored so the password never sits on the Settings object, where a repr in
        a log line or a traceback would print it.
        """
        password = None
        if self.database_password is not None:
            password = self.database_password.get_secret_value() or None
        elif self.database_password_file and self.database_password_file.is_file():
            password = self.database_password_file.read_text(encoding="utf-8").strip() or None
        if password is None:
            return self.database_url
        parts = urlsplit(self.database_url)
        if parts.password:  # already carries one; don't quietly override what the operator wrote
            return self.database_url
        userinfo = quote(parts.username or "", safe="") + ":" + quote(password, safe="")
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        return urlunsplit(parts._replace(netloc=f"{userinfo}@{host}"))

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".lock"
