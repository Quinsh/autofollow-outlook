from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

DEFAULT_CONFIG_TEXT = """# Autofollow configuration
# Keep this file private if you add personal addresses or custom paths.

[google]
credentials_path = "credentials.json"
token_path = ".autofollow/token.json"
# Least-privilege scopes needed by this tool: send email and read thread metadata.
scopes = [
  "https://www.googleapis.com/auth/gmail.send",
  "https://www.googleapis.com/auth/gmail.readonly"
]

[storage]
database_path = ".autofollow/autofollow.sqlite3"

[followup]
default_delay_days = 5
default_max_followups = 1
default_template_path = "templates/followup.txt"

[email]
# Optional: add aliases you send from so reply detection does not mistake your own
# follow-ups for replies. Example: own_addresses = ["alias@example.com"]
own_addresses = []

[inbox]
# Timezone used for daily views and interpreting relative deadlines.
timezone = "UTC"
initial_lookback_days = 30

[llm]
# auto uses OpenAI when OPENAI_API_KEY is set, otherwise Ollama.
provider = "auto"
openai_model = "gpt-4.1-mini"
model = "qwen3.5:9b"
host = "http://localhost:11434"
"""

DEFAULT_FOLLOWUP_TEMPLATE = """Hi {name},

Just wanted to follow up on my previous email below.

Best,
{sender_name}
"""


@dataclass(frozen=True)
class Config:
    config_path: Path
    credentials_path: Path
    token_path: Path
    database_path: Path
    default_delay_days: int = 5
    default_max_followups: int = 1
    default_template_path: Path = Path("templates/followup.txt")
    own_addresses: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=lambda: list(DEFAULT_SCOPES))
    timezone: str = "UTC"
    initial_lookback_days: int = 30
    llm_model: str = "qwen3.5:9b"
    llm_host: str = "http://localhost:11434"
    llm_provider: str = "auto"
    openai_model: str = "gpt-4.1-mini"
    mail_provider: str = "gmail"
    microsoft_client_id: str = ""
    microsoft_tenant_id: str = "common"
    display_name: str = ""
    signature: str = ""

    @property
    def mail_provider_name(self) -> str:
        return "Outlook" if self.mail_provider == "outlook" else "Gmail"

    @property
    def mail_configured(self) -> bool:
        return bool(self.microsoft_client_id) if self.mail_provider == "outlook" else self.credentials_path.exists()


def _resolve(base: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return (base / path).resolve()


def load_config(config_path: str | Path | None = None) -> Config:
    path = Path(config_path or "config.toml").expanduser().resolve()
    base = path.parent

    data: dict = {}
    if path.exists():
        with path.open("rb") as f:
            data = tomllib.load(f)

    def table(name: str) -> dict:
        value = data.get(name, {})
        if not isinstance(value, dict):
            raise ValueError(f"Configuration [{name}] must be a table")
        return value

    def string(section: dict, key: str, default: str) -> str:
        value = section.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Configuration {key} must be a nonempty string")
        return value.strip()

    def positive_integer(section: dict, key: str, default: int) -> int:
        value = section.get(key, default)
        if type(value) is not int or value < 1:
            raise ValueError(f"Configuration {key} must be a positive integer")
        return value

    def strings(section: dict, key: str, default: list[str]) -> list[str]:
        value = section.get(key, default)
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"Configuration {key} must be an array of nonempty strings")
        return [item.strip() for item in value]

    microsoft = table("microsoft")
    google = table("google")
    storage = table("storage")
    followup = table("followup")
    email = table("email")
    inbox = table("inbox")
    llm = table("llm")
    mail_provider = string(email, "provider", "gmail")
    if mail_provider not in {"gmail", "outlook"}:
        raise ValueError("Configuration email.provider must be gmail or outlook")
    def optional_string(section, key):
        value = section.get(key, "")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError(f"Configuration {key} must be text")
        return value.strip()
    client_id = optional_string(microsoft, "client_id")
    tenant_id = string(microsoft, "tenant_id", "common")
    if client_id:
        try:
            UUID(client_id)
        except ValueError as exc:
            raise ValueError("microsoft.client_id must be the Application (client) ID UUID") from exc
    if tenant_id not in {"common", "organizations", "consumers"}:
        try:
            UUID(tenant_id)
        except ValueError as exc:
            raise ValueError("microsoft.tenant_id must be common, organizations, consumers, or a tenant UUID") from exc
    timezone = string(inbox, "timezone", "UTC")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown inbox timezone: {timezone}") from exc
    host = string(llm, "host", "http://localhost:11434").rstrip("/")
    parsed_host = urlsplit(host)
    if parsed_host.scheme not in {"http", "https"} or not parsed_host.hostname:
        raise ValueError("Configuration llm.host must be an HTTP or HTTPS URL")
    if parsed_host.query or parsed_host.fragment or parsed_host.username or parsed_host.password:
        raise ValueError("Configuration llm.host cannot include credentials, a query, or a fragment")
    try:
        parsed_host.port
    except ValueError as exc:
        raise ValueError("Configuration llm.host has an invalid port") from exc
    provider = string(llm, "provider", "auto")
    if provider not in {"auto", "ollama", "openai"}:
        raise ValueError("Configuration llm.provider must be auto, ollama, or openai")
    scopes = strings(google, "scopes", DEFAULT_SCOPES)
    if not scopes:
        raise ValueError("Configuration google.scopes must not be empty")

    return Config(
        config_path=path,
        mail_provider=mail_provider,
        microsoft_client_id=client_id,
        microsoft_tenant_id=tenant_id,
        display_name=optional_string(email, "display_name"),
        signature=optional_string(email, "signature"),
        credentials_path=_resolve(base, string(google, "credentials_path", "credentials.json")),
        token_path=_resolve(base, string(microsoft, "token_path", ".autofollow/outlook-token-cache.json")
                            if mail_provider == "outlook" else string(google, "token_path", ".autofollow/token.json")),
        database_path=_resolve(base, string(storage, "database_path", ".autofollow/autofollow.sqlite3")),
        default_delay_days=positive_integer(followup, "default_delay_days", 5),
        default_max_followups=positive_integer(followup, "default_max_followups", 1),
        default_template_path=_resolve(base, string(followup, "default_template_path", "templates/followup.txt")),
        own_addresses=[address.lower() for address in strings(email, "own_addresses", [])],
        scopes=scopes,
        timezone=timezone,
        initial_lookback_days=positive_integer(inbox, "initial_lookback_days", 30),
        llm_model=string(llm, "model", "qwen3.5:9b"),
        llm_host=host,
        llm_provider=provider,
        openai_model=string(llm, "openai_model", "gpt-4.1-mini"),
    )


def initialize_project(config_path: str | Path | None = None, overwrite: bool = False, provider: str = "gmail") -> Config:
    path = Path(config_path or "config.toml").expanduser().resolve()
    if overwrite or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        if provider not in {"gmail", "outlook"}:
            raise ValueError("Mail provider must be gmail or outlook")
        path.write_text(OUTLOOK_CONFIG_TEXT if provider == "outlook" else DEFAULT_CONFIG_TEXT, encoding="utf-8")

    # Resolve paths from the configuration actually on disk after a reset.
    # --overwrite also repairs an invalid old configuration.
    cfg = load_config(path)
    cfg.database_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.token_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.default_template_path.parent.mkdir(parents=True, exist_ok=True)
    if overwrite or not cfg.default_template_path.exists():
        cfg.default_template_path.write_text(DEFAULT_FOLLOWUP_TEMPLATE, encoding="utf-8")
    return cfg


OUTLOOK_CONFIG_TEXT = """# Outlook / Microsoft 365 setup: see OUTLOOK_AND_NEW_USER_SETUP.md
[email]
provider = "outlook"
own_addresses = []
display_name = ""
signature = ""

[microsoft]
# Application (client) ID from Microsoft Entra App registrations.
client_id = ""
tenant_id = "common"
token_path = ".autofollow/outlook-token-cache.json"

[storage]
database_path = ".autofollow/outlook.sqlite3"

[followup]
default_delay_days = 5
default_max_followups = 1
default_template_path = "templates/followup.txt"

[inbox]
timezone = "UTC"
initial_lookback_days = 30

[llm]
provider = "auto"
openai_model = "gpt-4.1-mini"
model = "qwen3.5:9b"
host = "http://localhost:11434"
"""
