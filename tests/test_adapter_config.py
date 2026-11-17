from pathlib import Path

import pytest
import yaml

from issue_sync.adapter_config import (
    AdapterConfigError,
    load_adapter_configs,
    load_jira_config,
    load_slack_config,
)


def _write_yaml(tmp_path: Path, name: str, data: object) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _jira_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "project_key": "OPS",
        "service_account_id": "acc-service",
        "statuses": {"open": "To Do", "done": "Done"},
        "priorities": {"high": "High"},
        "assignees": {"backend": "acc-1"},
    }
    data.update(overrides)
    return data


def test_example_files_load(jira_example_path: Path, slack_example_path: Path) -> None:
    configs = load_adapter_configs(jira_example_path, slack_example_path)
    assert configs.jira.project_key == "OPS"
    assert configs.jira.issue_type == "Task"
    assert configs.slack.users["backend"].startswith("U")


def test_missing_file_names_the_example(tmp_path: Path) -> None:
    with pytest.raises(AdapterConfigError, match=r"jira\.example\.yaml"):
        load_jira_config(tmp_path / "jira.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "slack.yaml"
    path.write_text("users: [unclosed", encoding="utf-8")
    with pytest.raises(AdapterConfigError, match="invalid YAML"):
        load_slack_config(path)


def test_top_level_must_be_mapping(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "slack.yaml", ["a", "b"])
    with pytest.raises(AdapterConfigError, match="mapping"):
        load_slack_config(path)


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    # A typo such as "priority" instead of "priorities" must not be silently ignored.
    path = _write_yaml(tmp_path, "jira.yaml", _jira_data(priority={"high": "High"}))
    with pytest.raises(AdapterConfigError, match="priority"):
        load_jira_config(path)


def test_blank_mapping_value_is_rejected(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "jira.yaml", _jira_data(assignees={"backend": "  "}))
    with pytest.raises(AdapterConfigError, match="assignees"):
        load_jira_config(path)


def test_invalid_project_key(tmp_path: Path) -> None:
    path = _write_yaml(tmp_path, "jira.yaml", _jira_data(project_key="ops-1"))
    with pytest.raises(AdapterConfigError, match="project_key"):
        load_jira_config(path)


def test_duplicate_jira_status_is_rejected(tmp_path: Path) -> None:
    statuses = {"open": "To Do", "in_progress": "to do"}
    path = _write_yaml(tmp_path, "jira.yaml", _jira_data(statuses=statuses))
    with pytest.raises(AdapterConfigError, match="ambiguous"):
        load_jira_config(path)


def test_status_translation_both_directions(tmp_path: Path) -> None:
    config = load_jira_config(_write_yaml(tmp_path, "jira.yaml", _jira_data()))
    assert config.jira_status_for("open") == "To Do"
    assert config.internal_status_for("DONE") == "done"
    assert config.internal_status_for("Blocked") is None
