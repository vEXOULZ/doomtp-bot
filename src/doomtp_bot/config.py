"""Settings from environment, .env and Docker secrets (architecture §3.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    web_host: str = "127.0.0.1"
    web_port: int = 8080
    public_base_url: str = "http://localhost:8080"

    default_prefix: str = "!"
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

    @property
    def bot_db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def chatlog_db_path(self) -> Path:
        return self.data_dir / "chatlog.db"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / ".lock"
