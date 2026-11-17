"""Process settings read from environment variables (and an optional `.env` file).

Secrets never live in code or in the adapter YAML files; they are injected here.
Settings are split by concern so tools that need only one part (the migration runner,
the `jira-check` command) do not require the rest.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AfterValidator, AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _strip_trailing_slash(url: AnyHttpUrl) -> str:
    return str(url).rstrip("/")


BaseUrl = Annotated[AnyHttpUrl, AfterValidator(_strip_trailing_slash)]


class _EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class DatabaseSettings(_EnvSettings):
    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )


class JiraSettings(_EnvSettings):
    jira_base_url: BaseUrl = Field(description="e.g. https://your-domain.atlassian.net")
    jira_email: str = Field(description="Email of the Atlassian account that owns the API token")
    jira_api_token: SecretStr
    jira_config_path: Path = Path("config/jira.yaml")


class GoogleSettings(_EnvSettings):
    google_service_account_file: Path = Field(
        default=Path("secrets/google-service-account.json"),
        description="JSON key of the Google service account the sheets are shared with",
    )


def _valid_poll_interval(seconds: int) -> int:
    # arq cron schedules by second/minute sets, so the interval must divide a minute or an hour.
    if (seconds < 60 and 60 % seconds == 0) or (seconds % 60 == 0 and 60 % (seconds // 60) == 0):
        return seconds
    raise ValueError("must divide 60 seconds (e.g. 15, 30) or 60 minutes (e.g. 60, 300, 600)")


class Settings(DatabaseSettings, JiraSettings, GoogleSettings):
    redis_url: str = Field(description="Redis URL used by arq, e.g. redis://redis:6379/0")
    slack_config_path: Path = Path("config/slack.yaml")
    log_level: str = "INFO"

    public_base_url: BaseUrl = Field(
        description="Public URL of this service; detail-page links written to the sheet use it",
    )
    sheets_webhook_secret: SecretStr = Field(
        min_length=16, description="Shared secret the Apps Script trigger sends"
    )
    sheets_poll_interval_seconds: Annotated[
        int, Field(ge=10), AfterValidator(_valid_poll_interval)
    ] = 60
    push_max_attempts: int = Field(default=8, ge=1, description="Ticket push attempts per job")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
