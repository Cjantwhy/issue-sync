from pathlib import Path

import pytest

# Jira fixtures (respx-mocked client and backend), shared by the Jira test modules.
from tests.jira_fixtures import backend, jira_client, jira_config, jira_mock  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def jira_example_path() -> Path:
    return CONFIG_DIR / "jira.example.yaml"


@pytest.fixture
def slack_example_path() -> Path:
    return CONFIG_DIR / "slack.example.yaml"


@pytest.fixture
def classifier_example_path() -> Path:
    return CONFIG_DIR / "classifier.example.yaml"


@pytest.fixture
def assignment_example_path() -> Path:
    return CONFIG_DIR / "assignment.example.yaml"
