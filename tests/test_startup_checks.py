import logging
from pathlib import Path
from typing import Any

import pytest

from issue_sync.adapter_config import AdapterConfigs, load_adapter_configs
from issue_sync.startup_checks import (
    InternalConstants,
    StartupCheckError,
    check_mappings,
    ensure_mappings,
)

# Mirrors the seed data in db/extras.sql.
SEED = InternalConstants(
    statuses=frozenset({"open", "in_progress", "done"}),
    urgencies=frozenset({"critical", "high", "medium", "low"}),
    roles=frozenset({"backend", "frontend", "ops", "support"}),
    types=frozenset({"bug", "data_issue", "access_request", "question"}),
)


@pytest.fixture
def example_configs(
    jira_example_path: Path, slack_example_path: Path, classifier_example_path: Path
) -> AdapterConfigs:
    return load_adapter_configs(jira_example_path, slack_example_path, classifier_example_path)


def _with(
    configs: AdapterConfigs,
    jira: dict[str, Any] | None = None,
    slack: dict[str, Any] | None = None,
) -> AdapterConfigs:
    return AdapterConfigs(
        jira=configs.jira.model_copy(update=jira or {}),
        slack=configs.slack.model_copy(update=slack or {}),
        classifier=configs.classifier,
    )


def test_example_configs_cover_seed_data(example_configs: AdapterConfigs) -> None:
    report = check_mappings(SEED, example_configs)
    assert report.ok
    assert report.unknown == []


def test_every_missing_mapping_is_reported(example_configs: AdapterConfigs) -> None:
    constants = InternalConstants(
        statuses=SEED.statuses | {"blocked"},
        urgencies=SEED.urgencies | {"trivial"},
        roles=SEED.roles | {"security"},
        types=SEED.types | {"incident"},
    )
    report = check_mappings(constants, example_configs)
    assert not report.ok
    assert report.missing == [
        "jira.yaml: statuses has no entry for document_record_status value 'blocked'",
        "jira.yaml: priorities has no entry for ai_judgement_urgency value 'trivial'",
        "jira.yaml: assignees has no entry for assignee_role value 'security'",
        "slack.yaml: users has no entry for assignee_role value 'security'",
        "classifier.yaml: types has no entry for ai_judgement_type value 'incident'",
        "classifier.yaml: urgencies has no entry for ai_judgement_urgency value 'trivial'",
        "classifier.yaml: roles has no entry for assignee_role value 'security'",
    ]


def test_slack_only_gap_is_reported(example_configs: AdapterConfigs) -> None:
    users = dict(example_configs.slack.users)
    del users["ops"]
    report = check_mappings(SEED, _with(example_configs, slack={"users": users}))
    assert report.missing == ["slack.yaml: users has no entry for assignee_role value 'ops'"]


def test_empty_constant_table_is_fatal_and_reported_once(
    example_configs: AdapterConfigs,
) -> None:
    constants = InternalConstants(
        statuses=SEED.statuses, urgencies=SEED.urgencies, roles=frozenset(), types=SEED.types
    )
    report = check_mappings(constants, example_configs)
    assert not report.ok
    assert [m for m in report.missing if "is empty" in m] == [
        "table assignee_role is empty; seed it before starting (db/extras.sql)"
    ]


def test_extra_config_keys_are_warnings_only(
    example_configs: AdapterConfigs, caplog: pytest.LogCaptureFixture
) -> None:
    priorities = {**example_configs.jira.priorities, "trivial": "Lowest"}
    configs = _with(example_configs, jira={"priorities": priorities})
    with caplog.at_level(logging.WARNING):
        ensure_mappings(SEED, configs)  # does not raise
    assert "maps 'trivial'" in caplog.text


def test_ensure_mappings_raises_with_all_problems(example_configs: AdapterConfigs) -> None:
    constants = InternalConstants(
        statuses=SEED.statuses | {"blocked"},
        urgencies=SEED.urgencies,
        roles=SEED.roles | {"qa"},
        types=SEED.types,
    )
    with pytest.raises(StartupCheckError) as exc_info:
        ensure_mappings(constants, example_configs)
    message = str(exc_info.value)
    assert "'blocked'" in message
    assert message.count("'qa'") == 3  # missing in jira.yaml, slack.yaml and classifier.yaml
