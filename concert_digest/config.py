"""Settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Brussels")
WATCH_TAG = "[Concert Digest]"


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; existing env vars win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass
class Settings:
    home: Path
    concertje_url: str
    oauth_port: int
    search_tool: str
    search_arg: str
    lookback_days: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    imap_host: str
    mail_from: str
    mail_to: str
    watch_allowed_from: list[str] = field(default_factory=list)
    spotify_client_id: str = ""
    spotify_client_secret: str = ""

    @property
    def watch_address(self) -> str:
        """Where the 'Watch' mailto buttons send their request."""
        return self.mail_from or self.mail_to


def load_settings() -> Settings:
    load_dotenv(Path.cwd() / ".env")
    home = Path(_env("CONCERT_DIGEST_HOME") or Path.home() / ".concert-digest").expanduser()
    home.mkdir(parents=True, exist_ok=True)
    smtp_user = _env("SMTP_USER")
    mail_to = _env("MAIL_TO") or smtp_user
    allowed = [a.strip().lower() for a in _env("WATCH_ALLOWED_FROM", mail_to).split(",") if a.strip()]
    return Settings(
        home=home,
        concertje_url=_env("CONCERTJE_MCP_URL", "https://ai.concertje.be/mcp"),
        oauth_port=int(_env("CONCERTJE_OAUTH_PORT", "8765")),
        search_tool=_env("CONCERTJE_SEARCH_TOOL"),
        search_arg=_env("CONCERTJE_SEARCH_ARG"),
        lookback_days=int(_env("LOOKBACK_DAYS", "21")),
        smtp_host=_env("SMTP_HOST", "smtp.gmail.com"),
        smtp_port=int(_env("SMTP_PORT", "465")),
        smtp_user=smtp_user,
        smtp_password=_env("SMTP_PASSWORD"),
        imap_host=_env("IMAP_HOST", "imap.gmail.com"),
        mail_from=_env("MAIL_FROM") or smtp_user,
        mail_to=mail_to,
        watch_allowed_from=allowed,
        spotify_client_id=_env("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=_env("SPOTIFY_CLIENT_SECRET"),
    )
