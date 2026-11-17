"""Process settings read from environment variables (and an optional `.env` file).

Secrets never live in code or in the adapter YAML files; they are injected here.
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """The minimal settings needed to reach PostgreSQL.

    Kept separate so the migration runner does not require Redis or adapter config.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )


class Settings(DatabaseSettings):
    redis_url: str = Field(description="Redis URL used by arq, e.g. redis://redis:6379/0")
    jira_config_path: Path = Path("config/jira.yaml")
    slack_config_path: Path = Path("config/slack.yaml")
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # values come from the environment
