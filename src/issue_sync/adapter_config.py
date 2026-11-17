"""Adapter configuration: how internal values translate to external systems.

The database only stores internal values (statuses, urgencies, roles). Each adapter owns
its own YAML file mapping those values to its system. Cross-checking these files against
the database constant tables happens in `startup_checks`.
"""

from pathlib import Path
from typing import Annotated, Self

import yaml
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AdapterConfigError(Exception):
    """An adapter config file is missing, unreadable or invalid."""


class _StrictModel(BaseModel):
    # Unknown keys are rejected so a typo in YAML fails loudly instead of being ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class JiraConfig(_StrictModel):
    project_key: Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]+$")]
    issue_type: NonEmptyStr = "Task"
    # Account the system uses to call Jira; webhook events it authored are ignored (loop guard).
    service_account_id: NonEmptyStr
    # internal status name -> Jira status name (one-to-one, used in both directions)
    statuses: dict[NonEmptyStr, NonEmptyStr]
    # internal urgency level -> Jira priority name
    priorities: dict[NonEmptyStr, NonEmptyStr]
    # internal assignee role -> Jira accountId
    assignees: dict[NonEmptyStr, NonEmptyStr]

    @model_validator(mode="after")
    def _statuses_are_one_to_one(self) -> Self:
        seen: dict[str, str] = {}
        for internal, jira_name in self.statuses.items():
            key = jira_name.casefold()
            if key in seen:
                raise ValueError(
                    f"statuses: Jira status {jira_name!r} is mapped from both "
                    f"{seen[key]!r} and {internal!r}; inbound status translation "
                    "would be ambiguous"
                )
            seen[key] = internal
        return self

    def jira_status_for(self, internal_status: str) -> str:
        return self.statuses[internal_status]

    def internal_status_for(self, jira_status: str) -> str | None:
        """Translate a Jira status name back to the internal value (case-insensitive)."""
        wanted = jira_status.casefold()
        for internal, jira_name in self.statuses.items():
            if jira_name.casefold() == wanted:
                return internal
        return None


class SlackConfig(_StrictModel):
    default_channel: NonEmptyStr
    # internal assignee role -> Slack user ID
    users: dict[NonEmptyStr, NonEmptyStr]


class ClassifierConfig(_StrictModel):
    """What each internal value means, in words the model can act on.

    The database only has names like "ops"; the model needs to know what ops handles.
    """

    # ai_judgement_type.type_name -> description
    types: dict[NonEmptyStr, NonEmptyStr]
    # ai_judgement_urgency.urgency_level_name -> description
    urgencies: dict[NonEmptyStr, NonEmptyStr]
    # assignee_role.role_name -> what the team handles
    roles: dict[NonEmptyStr, NonEmptyStr]
    # Extra instructions appended to the system prompt (e.g. output language).
    guidelines: str | None = None


class AdapterConfigs(_StrictModel):
    jira: JiraConfig
    slack: SlackConfig
    classifier: ClassifierConfig


def _load_yaml_model[M: BaseModel](path: Path, model: type[M]) -> M:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise AdapterConfigError(
            f"{path}: file not found (copy {path.with_suffix('').name}.example.yaml "
            "and fill it in, or point the *_CONFIG_PATH env var at it)"
        ) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AdapterConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise AdapterConfigError(f"{path}: expected a mapping at the top level")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise AdapterConfigError(f"{path}: {exc}") from exc


def load_jira_config(path: Path) -> JiraConfig:
    return _load_yaml_model(path, JiraConfig)


def load_slack_config(path: Path) -> SlackConfig:
    return _load_yaml_model(path, SlackConfig)


def load_classifier_config(path: Path) -> ClassifierConfig:
    return _load_yaml_model(path, ClassifierConfig)


def load_adapter_configs(
    jira_path: Path, slack_path: Path, classifier_path: Path
) -> AdapterConfigs:
    return AdapterConfigs(
        jira=load_jira_config(jira_path),
        slack=load_slack_config(slack_path),
        classifier=load_classifier_config(classifier_path),
    )
