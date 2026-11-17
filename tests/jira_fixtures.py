"""Shared fixtures for Jira tests. All HTTP is mocked with respx; nothing hits the network."""

from collections.abc import AsyncIterator

import pytest
import respx

from issue_sync.adapter_config import JiraConfig
from issue_sync.tickets.jira.backend import JiraBackend
from issue_sync.tickets.jira.client import JiraClient

SITE = "https://example.atlassian.net"
API = f"{SITE}/rest/api/3"
EMAIL = "bot@example.com"
TOKEN = "s3cret-token"


def make_config(**overrides: object) -> JiraConfig:
    data: dict[str, object] = {
        "project_key": "OPS",
        "service_account_id": "acc-bot",
        "statuses": {"open": "To Do", "in_progress": "In Progress", "done": "Done"},
        "priorities": {"critical": "Highest", "high": "High", "medium": "Medium", "low": "Low"},
        "assignees": {"backend": "acc-backend", "ops": "acc-ops"},
    }
    data.update(overrides)
    return JiraConfig.model_validate(data)


@pytest.fixture
def jira_config() -> JiraConfig:
    return make_config()


@pytest.fixture
async def jira_mock() -> AsyncIterator[respx.MockRouter]:
    # assert_all_mocked: any request without a matching route fails the test.
    async with respx.mock(base_url=API, assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
async def jira_client(jira_mock: respx.MockRouter) -> AsyncIterator[JiraClient]:
    async with JiraClient(SITE, EMAIL, TOKEN) as client:
        yield client


@pytest.fixture
def backend(jira_client: JiraClient, jira_config: JiraConfig) -> JiraBackend:
    return JiraBackend(jira_client, jira_config)
