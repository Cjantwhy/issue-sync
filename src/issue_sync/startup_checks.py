"""Fail-fast checks run by both the api and the worker before they accept work.

Every internal value in the database constant tables must have a mapping in the adapter
configs. A missing mapping would otherwise only surface at runtime, e.g. when the first
ticket with that urgency is created.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from issue_sync.adapter_config import AdapterConfigs, AssignmentConfig, load_adapter_configs
from issue_sync.db.models import (
    AiJudgementType,
    AiJudgementUrgency,
    AssigneeRole,
    DocumentRecordStatus,
)
from issue_sync.settings import Settings

logger = logging.getLogger(__name__)


class StartupCheckError(Exception):
    """Configuration and database disagree; the process must not start."""


@dataclass(frozen=True)
class InternalConstants:
    statuses: frozenset[str]
    urgencies: frozenset[str]
    roles: frozenset[str]
    types: frozenset[str]


@dataclass(frozen=True)
class MappingReport:
    missing: list[str]  # fatal: internal value without a mapping, or empty constant table
    unknown: list[str]  # warning: mapping for a value the database does not have

    @property
    def ok(self) -> bool:
        return not self.missing


async def load_internal_constants(engine: AsyncEngine) -> InternalConstants:
    async with engine.connect() as conn:
        statuses = (await conn.scalars(select(DocumentRecordStatus.status_name))).all()
        urgencies = (await conn.scalars(select(AiJudgementUrgency.urgency_level_name))).all()
        roles = (await conn.scalars(select(AssigneeRole.role_name))).all()
        types = (await conn.scalars(select(AiJudgementType.type_name))).all()
    return InternalConstants(
        frozenset(statuses), frozenset(urgencies), frozenset(roles), frozenset(types)
    )


def check_mappings(constants: InternalConstants, configs: AdapterConfigs) -> MappingReport:
    jira, slack, classifier = configs.jira, configs.slack, configs.classifier
    # (constant table, internal values, config file label, mapped keys)
    pairs = [
        ("document_record_status", constants.statuses, "jira.yaml: statuses", jira.statuses),
        ("ai_judgement_urgency", constants.urgencies, "jira.yaml: priorities", jira.priorities),
        ("assignee_role", constants.roles, "jira.yaml: assignees", jira.assignees),
        ("assignee_role", constants.roles, "slack.yaml: users", slack.users),
        ("ai_judgement_type", constants.types, "classifier.yaml: types", classifier.types),
        (
            "ai_judgement_urgency",
            constants.urgencies,
            "classifier.yaml: urgencies",
            classifier.urgencies,
        ),
        ("assignee_role", constants.roles, "classifier.yaml: roles", classifier.roles),
    ]

    missing: list[str] = []
    unknown: list[str] = []
    empty_tables_reported: set[str] = set()
    for table, values, label, mapping in pairs:
        if not values and table not in empty_tables_reported:
            missing.append(f"table {table} is empty; seed it before starting (db/extras.sql)")
            empty_tables_reported.add(table)
        missing.extend(
            f"{label} has no entry for {table} value {value!r}"
            for value in sorted(values - mapping.keys())
        )
        unknown.extend(
            f"{label} maps {key!r}, which is not a value in table {table}"
            for key in sorted(mapping.keys() - values)
        )
    missing.extend(check_rules(constants, configs.assignment))
    return MappingReport(missing=missing, unknown=unknown)


def check_rules(constants: InternalConstants, assignment: AssignmentConfig) -> list[str]:
    """Every value a rule mentions must exist; a typo would make the rule never match."""
    problems = []
    for rule in assignment.rules:
        refs = [
            ("when.type", rule.when.type or [], constants.types),
            ("when.urgency", rule.when.urgency or [], constants.urgencies),
            ("when.assignee_role", rule.when.assignee_role or [], constants.roles),
            ("assign", [rule.assign] if rule.assign else [], constants.roles),
            ("set_urgency", [rule.set_urgency] if rule.set_urgency else [], constants.urgencies),
        ]
        problems.extend(
            f"assignment.yaml: rule {rule.name!r} {field} uses unknown value {value!r}"
            for field, values, known in refs
            for value in values
            if value not in known
        )
    return problems


def ensure_mappings(constants: InternalConstants, configs: AdapterConfigs) -> None:
    report = check_mappings(constants, configs)
    for warning in report.unknown:
        logger.warning("Adapter config: %s", warning)
    if not report.ok:
        details = "\n  - ".join(report.missing)
        raise StartupCheckError(f"Adapter config does not cover the database:\n  - {details}")


async def run_startup_checks(settings: Settings, engine: AsyncEngine) -> AdapterConfigs:
    """Load adapter configs and verify they cover every internal value. Raises on failure."""
    configs = load_adapter_configs(
        settings.jira_config_path,
        settings.slack_config_path,
        settings.classifier_config_path,
        settings.assignment_config_path,
    )
    constants = await load_internal_constants(engine)
    ensure_mappings(constants, configs)
    logger.info("Startup checks passed")
    return configs
