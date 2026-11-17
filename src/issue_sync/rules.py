"""Assignment rules: deterministic overrides on top of the AI judgement.

Rules come from `config/assignment.yaml` and are checked in order; the first match wins.
They change only the ticket's priority and assignee. The AI judgement in the database
stays as the model produced it, so AI accuracy can still be measured.
Conditions on keywords or reporter also work when there is no AI judgement.
"""

from dataclasses import dataclass

from issue_sync.adapter_config import AssignmentConfig, AssignmentRule, RuleCondition
from issue_sync.tickets.base import TriageNote


@dataclass(frozen=True)
class RequestFacts:
    title: str | None
    description: str | None
    reporter: str | None


@dataclass(frozen=True)
class Assignment:
    urgency: str | None
    assignee_role: str | None
    rule: str | None = None  # name of the rule that matched

    def explain(self, triage: TriageNote | None) -> str | None:
        """One line on what the rule changed versus the AI suggestion.

        None when no rule matched, or when it matched but agreed with the AI: there is
        nothing to explain to the assignee then.
        """
        if self.rule is None:
            return None
        changes = []
        for label, final, suggested in (
            ("team", self.assignee_role, triage.assignee_role if triage else None),
            ("urgency", self.urgency, triage.urgency if triage else None),
        ):
            if final != suggested:
                changes.append(f"{label} {suggested or 'none'} -> {final}")
        if not changes:
            return None
        return f"Rule '{self.rule}': {', '.join(changes)}"


def decide(config: AssignmentConfig, triage: TriageNote | None, facts: RequestFacts) -> Assignment:
    ai = Assignment(
        urgency=triage.urgency if triage else None,
        assignee_role=triage.assignee_role if triage else None,
    )
    for rule in config.rules:
        if _matches(rule.when, triage, facts):
            return _apply(rule, ai)
    return ai


def _apply(rule: AssignmentRule, ai: Assignment) -> Assignment:
    return Assignment(
        urgency=rule.set_urgency or ai.urgency,
        assignee_role=rule.assign or ai.assignee_role,
        rule=rule.name,
    )


def _matches(when: RuleCondition, triage: TriageNote | None, facts: RequestFacts) -> bool:
    for values, actual in (
        (when.type, triage.type if triage else None),
        (when.urgency, triage.urgency if triage else None),
        (when.assignee_role, triage.assignee_role if triage else None),
        (when.reporter, (facts.reporter or "").strip() or None),
    ):
        if values is not None and (
            actual is None or actual.casefold() not in {v.casefold() for v in values}
        ):
            return False
    if when.keywords is not None:
        text = f"{facts.title or ''}\n{facts.description or ''}".casefold()
        if not any(keyword.casefold() in text for keyword in when.keywords):
            return False
    return True
