from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


@pytest.fixture
def jira_example_path() -> Path:
    return CONFIG_DIR / "jira.example.yaml"


@pytest.fixture
def slack_example_path() -> Path:
    return CONFIG_DIR / "slack.example.yaml"
