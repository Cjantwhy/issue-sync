"""Process settings read from environment variables (and an optional `.env` file).

Secrets never live in code or in the adapter YAML files; they are injected here.
Settings are split by concern so tools that need only one part (the migration runner,
the `jira-check` command) do not require the rest.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, AnyHttpUrl, Field, SecretStr, model_validator
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
    """Two ways to authenticate:

    - basic: an Atlassian account's email + API token, calling the site directly.
    - oauth: a service account's OAuth 2.0 client credentials, calling the
      api.atlassian.com gateway for `jira_cloud_id` (service accounts have no email).
    """

    jira_base_url: BaseUrl = Field(description="Site URL, e.g. https://your-domain.atlassian.net")
    jira_auth: Literal["basic", "oauth"] = "basic"
    jira_email: str | None = None
    jira_api_token: SecretStr | None = None
    jira_client_id: str | None = None
    jira_client_secret: SecretStr | None = None
    jira_cloud_id: str | None = None
    jira_config_path: Path = Path("config/jira.yaml")

    @model_validator(mode="after")
    def _credentials_for_auth_mode(self) -> "JiraSettings":
        required = {
            "basic": ("jira_email", "jira_api_token"),
            "oauth": ("jira_client_id", "jira_client_secret", "jira_cloud_id"),
        }[self.jira_auth]
        missing = []
        for name in required:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not value:
                missing.append(name.upper())
        if missing:
            raise ValueError(f"JIRA_AUTH={self.jira_auth} requires {', '.join(missing)}")
        return self


class GoogleSettings(_EnvSettings):
    google_service_account_file: Path = Field(
        default=Path("secrets/google-service-account.json"),
        description="JSON key of the Google service account the sheets are shared with",
    )


class LlmSettings(_EnvSettings):
    """Any OpenAI-compatible chat completions API (DeepSeek, OpenAI, OpenRouter, ...)."""

    llm_base_url: BaseUrl = Field(description="e.g. https://api.deepseek.com")
    llm_model: str = Field(min_length=1, description="e.g. deepseek-flash")
    llm_api_key: SecretStr = Field(min_length=1)
    # json_schema lets the API enforce the schema; json_object is supported more widely.
    llm_response_format: Literal["json_object", "json_schema"] = "json_object"
    # Provider-specific request fields, as JSON, e.g. {"thinking": {"type": "disabled"}}.
    llm_extra_body: dict[str, Any] = Field(default_factory=dict)
    llm_timeout_seconds: float = Field(default=60.0, gt=0)
    # Classification attempts before a ticket is created without an AI judgement.
    llm_max_attempts: int = Field(default=3, ge=1)
    classifier_config_path: Path = Path("config/classifier.yaml")


class SlackSettings(_EnvSettings):
    slack_bot_token: SecretStr = Field(
        min_length=1, description="Bot token (xoxb-...) of a Slack app with chat:write"
    )


def _divides_an_hour(minutes: int) -> int:
    if 1 <= minutes <= 60 and 60 % minutes == 0:
        return minutes
    raise ValueError("must divide 60 (e.g. 5, 10, 15, 30, 60)")


def _valid_poll_interval(seconds: int) -> int:
    # arq cron schedules by second/minute sets, so the interval must divide a minute or an hour.
    if (seconds < 60 and 60 % seconds == 0) or (seconds % 60 == 0 and 60 % (seconds // 60) == 0):
        return seconds
    raise ValueError("must divide 60 seconds (e.g. 15, 30) or 60 minutes (e.g. 60, 300, 600)")


class Settings(DatabaseSettings, JiraSettings, GoogleSettings, LlmSettings, SlackSettings):
    redis_url: str = Field(description="Redis URL used by arq, e.g. redis://redis:6379/0")
    slack_config_path: Path = Path("config/slack.yaml")
    assignment_config_path: Path = Path("config/assignment.yaml")
    log_level: str = "INFO"

    public_base_url: BaseUrl = Field(
        description="Public URL of this service; detail-page links written to the sheet use it",
    )
    sheets_webhook_secret: SecretStr = Field(
        min_length=16, description="Shared secret the Apps Script trigger sends"
    )
    jira_webhook_secret: SecretStr = Field(
        min_length=16, description="Secret of the Jira admin webhook; Jira signs bodies with it"
    )
    # Interactive API docs (/docs, /openapi.json). Off by default: they describe every
    # endpoint to anyone who can reach the api.
    enable_api_docs: bool = False
    sheets_poll_interval_seconds: Annotated[
        int, Field(ge=10), AfterValidator(_valid_poll_interval)
    ] = 60
    push_max_attempts: int = Field(default=8, ge=1, description="Ticket push attempts per job")
    # Reconciliation (missed webhooks, deleted rows, stuck pushes); must divide 60.
    reconcile_interval_minutes: Annotated[int, AfterValidator(_divides_an_hour)] = 15
    # Pushes or notifications stuck longer than this are re-queued or reported.
    reconcile_stale_minutes: int = Field(default=30, ge=5)

    @property
    def push_retry_limit(self) -> int:
        """Failed attempts after which reconciliation stops re-queueing a push."""
        return self.push_max_attempts * 3


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
