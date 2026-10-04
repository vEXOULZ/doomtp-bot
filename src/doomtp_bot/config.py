"""Settings from environment, .env and Docker secrets (architecture §3.7)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit, urlunsplit

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from doomtp_bot.api.sessions import LOCAL_NETWORKS
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
    # The public pages can be opened by chat at PUBLIC_BASE_URL (a reverse proxy, a tunnel). Off by
    # default, because the web UI is LAN-only until then; on, `!explain` links to its full report.
    public_web_ui: bool = False
    # The public web site (doomtp-web), the same for every channel. When set, `!help` ends with a link to
    # the channel's command page there. Unset, `!help` names no page.
    web_site_url: str | None = None
    # Comma-separated addresses (or "*") of reverse proxies whose X-Forwarded-For/-Proto uvicorn believes.
    # The default trusts only a proxy on the same host; anything else would let a client pick its address.
    web_forwarded_allow_ips: str = "127.0.0.1"

    default_prefix: str = DEFAULT_PREFIX

    # The admin password: the web site's way in that doesn't need Twitch. Read from the environment (.env)
    # or a file. Without one, the password login is off rather than open.
    admin_password: SecretStr | None = None
    admin_password_file: Path | None = None
    # Where the password login is taken from: comma-separated addresses or CIDRs, or `*` for anywhere.
    # Default: this host and the private ranges.
    admin_password_networks: str = LOCAL_NETWORKS
    # Who signs moderators in to the web admin (ADR-0023): Twitch directly, or vexoulz-auth (the sign-in
    # shared by the vexoulz sites), which then needs its URL and this bot's client registration there.
    signin_provider: Literal["twitch", "vexoulz"] = "twitch"
    vexoulz_auth_url: str | None = None  # as browsers reach it
    vexoulz_auth_internal_url: str | None = None  # as the bot reaches it; unset = vexoulz_auth_url
    vexoulz_auth_client_id: str = "dtp"
    vexoulz_auth_client_secret: SecretStr | None = None
    vexoulz_auth_client_secret_file: Path | None = None
    ivr_logs_url: str = "https://logs.ivr.fi"  # where chat history is backfilled from (ADR-0008)

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"

    @property
    def bot_owner_ids(self) -> frozenset[str]:
        return frozenset(p.strip() for p in self.bot_owner_ids_csv.split(",") if p.strip())

    def client_secret(self) -> str | None:
        """TWITCH_CLIENT_SECRET, or the contents of TWITCH_CLIENT_SECRET_FILE."""
        return _secret(self.twitch_client_secret, self.twitch_client_secret_file)

    def vexoulz_auth_secret(self) -> str | None:
        """VEXOULZ_AUTH_CLIENT_SECRET, or the contents of VEXOULZ_AUTH_CLIENT_SECRET_FILE."""
        return _secret(self.vexoulz_auth_client_secret, self.vexoulz_auth_client_secret_file)

    def admin_password_value(self) -> str | None:
        """ADMIN_PASSWORD, or the contents of ADMIN_PASSWORD_FILE."""
        return _secret(self.admin_password, self.admin_password_file)

    def database_dsn(self) -> str:
        """DATABASE_URL with DATABASE_PASSWORD (or the contents of DATABASE_PASSWORD_FILE) spliced in.

        Returned rather than stored so the password never sits on the Settings object, where a repr in
        a log line or a traceback would print it.
        """
        password = _secret(self.database_password, self.database_password_file)
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


def _secret(value: SecretStr | None, file: Path | None) -> str | None:
    """A secret given inline, else read from its file; blank counts as not set."""
    if value is not None:
        return value.get_secret_value() or None
    if file and file.is_file():
        return file.read_text(encoding="utf-8").strip() or None
    return None
