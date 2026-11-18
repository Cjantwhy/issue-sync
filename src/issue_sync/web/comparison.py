"""AI judgement versus the ticket as it is now, per field (priority and assignee).

Verdicts:
- agree:   the ticket's current value is what the AI suggested.
- rule:    an assignment rule changed the AI's suggestion when the ticket was created,
           and the ticket still has the rule's value.
- human:   the current value is neither: someone changed it in the ticket system.
- unknown: the current value cannot be compared (an assignee recorded before account
           ids were kept, until the next event or reconciliation fills the id in).
- none:    nothing to compare: no AI judgement, or no ticket state read yet.

Internal values are translated with `config/jira.yaml` before comparing: urgency to a
priority name, role to an account id. The verdict is computed in SQL, so the same
definition drives the filter, the per-record badges and the per-model statistics.
"""

from typing import Literal

from sqlalchemy import ColumnElement, and_, case, literal, null, or_
from sqlalchemy.types import String

Verdict = Literal["agree", "rule", "human", "unknown", "none"]
VERDICTS: tuple[Verdict, ...] = ("agree", "rule", "human", "unknown", "none")
DIFFERS: tuple[Verdict, ...] = ("rule", "human")


def translate(mapping: dict[str, str], internal: ColumnElement[str]) -> ColumnElement[str]:
    """internal value -> external value via an adapter mapping; NULL if unmapped."""
    if not mapping:
        return null()
    return case(mapping, value=internal, else_=null())


def verdict(
    *,
    ai: ColumnElement[str],
    assigned: ColumnElement[str],
    current: ColumnElement[str],
    mapping: dict[str, str],
    ticket_read: ColumnElement[bool],
    current_unknown: ColumnElement[bool] | None = None,
) -> ColumnElement[str]:
    """SQL expression evaluating to one of VERDICTS.

    `ai` and `assigned` are internal values (the AI judgement; the assignment applied at
    creation, NULL if not kept), `current` the ticket's value in external form.
    """
    ai_ext = translate(mapping, ai)
    assigned_ext = translate(mapping, assigned)
    whens: list[tuple[ColumnElement[bool], ColumnElement[str]]] = [
        (or_(~ticket_read, ai.is_(None)), literal("none", String)),
    ]
    if current_unknown is not None:
        whens.append((current_unknown, literal("unknown", String)))
    whens += [
        (current.is_not_distinct_from(ai_ext), literal("agree", String)),
        (
            and_(assigned_ext.is_not(None), current.is_not_distinct_from(assigned_ext)),
            literal("rule", String),
        ),
    ]
    return case(*whens, else_=literal("human", String))
