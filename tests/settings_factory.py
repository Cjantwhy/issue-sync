from typing import Any

from issue_sync.settings import Settings

# Every required setting, with dummy values, so tests never depend on a local .env.
REQUIRED: dict[str, Any] = {
    "database_url": "postgresql+asyncpg://u:p@db/x",
    "redis_url": "redis://r:6379/0",
    "jira_base_url": "https://example.atlassian.net",
    "jira_email": "bot@example.com",
    "jira_api_token": "token",
    "public_base_url": "http://localhost:8000",
    "sheets_webhook_secret": "x" * 32,
    "llm_base_url": "https://llm.example.com",
    "llm_model": "test-model",
    "llm_api_key": "llm-key",
    "slack_bot_token": "xoxb-test",
    "jira_webhook_secret": "j" * 32,
}


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **{**REQUIRED, **overrides})  # type: ignore[call-arg]
