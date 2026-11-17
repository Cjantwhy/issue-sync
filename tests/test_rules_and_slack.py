import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from pydantic import ValidationError

from issue_sync.adapter_config import (
    AssignmentConfig,
    AssignmentRule,
    load_assignment_config,
)
from issue_sync.notify.message import TicketNotice, build_message, escape
from issue_sync.notify.slack import SLACK_API, SlackClient, SlackError
from issue_sync.rules import Assignment, RequestFacts, decide
from issue_sync.startup_checks import InternalConstants, check_rules
from issue_sync.tickets.base import TriageNote

AI = TriageNote(
    type="bug",
    urgency="medium",
    assignee_role="backend",
    summary="Export broken.",
    reason="r",
    model="m",
)
FACTS = RequestFacts(title="Export fails", description="Month-end report is stuck", reporter="Ann")


def _rules(*rules: dict) -> AssignmentConfig:
    return AssignmentConfig.model_validate({"rules": list(rules)})


# ---------- rules ----------


def test_no_rule_keeps_the_ai_choice() -> None:
    assert decide(_rules(), AI, FACTS) == Assignment(urgency="medium", assignee_role="backend")


def test_first_matching_rule_wins() -> None:
    config = _rules(
        {"name": "never", "when": {"type": "question"}, "assign": "support"},
        {"name": "month-end", "when": {"keywords": ["MONTH-END"]}, "set_urgency": "high"},
        {"name": "later", "when": {"type": "bug"}, "assign": "ops"},
    )
    result = decide(config, AI, FACTS)
    assert result == Assignment(urgency="high", assignee_role="backend", rule="month-end")
    assert result.explain(AI) == "Rule 'month-end': urgency medium -> high"


def test_rule_that_agrees_with_the_ai_needs_no_explanation() -> None:
    # Found live: an "access requests go to ops" rule on a request the AI already gave
    # to ops produced a pointless "no change" comment.
    config = _rules({"name": "bugs to backend", "when": {"type": "bug"}, "assign": "backend"})
    result = decide(config, AI, FACTS)
    assert result.rule == "bugs to backend"
    assert result.explain(AI) is None


def test_all_conditions_must_hold() -> None:
    rule = {
        "name": "x",
        "when": {"type": "bug", "urgency": ["low", "high"]},  # AI said medium
        "assign": "ops",
    }
    assert decide(_rules(rule), AI, FACTS).rule is None


def test_reporter_matches_exactly_ignoring_case() -> None:
    config = _rules({"name": "vip", "when": {"reporter": "An"}, "set_urgency": "critical"})
    assert decide(config, AI, FACTS).rule is None  # a prefix is not a match
    config = _rules({"name": "vip", "when": {"reporter": " ANN "}, "set_urgency": "critical"})
    assert decide(config, AI, FACTS).urgency == "critical"


def test_keyword_rules_work_without_an_ai_judgement() -> None:
    config = _rules(
        {"name": "by type", "when": {"type": "bug"}, "assign": "backend"},
        {"name": "by keyword", "when": {"keywords": "month-end"}, "assign": "ops"},
    )
    result = decide(config, None, FACTS)
    assert result == Assignment(urgency=None, assignee_role="ops", rule="by keyword")
    assert result.explain(None) == "Rule 'by keyword': team none -> ops"


@pytest.mark.parametrize(
    ("rule", "message"),
    [
        ({"name": "x", "when": {}, "assign": "ops"}, "at least one condition"),
        ({"name": "x", "when": {"type": "bug"}}, "needs `assign`"),
        ({"name": "x", "when": {"typ": "bug"}, "assign": "ops"}, "Extra inputs"),
    ],
)
def test_invalid_rules_are_rejected(rule: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        AssignmentRule.model_validate(rule)


def test_rules_referencing_unknown_values_fail_the_startup_check(
    assignment_example_path: Path,
) -> None:
    constants = InternalConstants(
        statuses=frozenset({"open"}),
        urgencies=frozenset({"low", "medium", "high", "critical"}),
        roles=frozenset({"backend", "ops", "support", "frontend"}),
        types=frozenset({"bug", "access_request", "question", "data_issue"}),
    )
    assert check_rules(constants, load_assignment_config(assignment_example_path)) == []
    config = _rules(
        {"name": "typo", "when": {"type": "access-request"}, "assign": "secops"},
    )
    assert check_rules(constants, config) == [
        "assignment.yaml: rule 'typo' when.type uses unknown value 'access-request'",
        "assignment.yaml: rule 'typo' assign uses unknown value 'secops'",
    ]


# ---------- message ----------


def _notice(**overrides: object) -> TicketNotice:
    data: dict = {
        "issue_key": "OPS-7",
        "issue_url": "https://jira/browse/OPS-7",
        "detail_url": "https://sync/r/abc",
        "title": "Export fails",
        "reporter": "Ann",
        "urgency": "high",
        "assignee_role": "backend",
        "slack_user": "U123",
        "ai_summary": "Export is broken.",
        "rule_note": None,
    }
    data.update(overrides)
    return TicketNotice(**data)


def test_message_mentions_the_assignee() -> None:
    text, blocks = build_message(_notice())
    assert text == "New ticket OPS-7: Export fails - assigned to <@U123>"
    assert blocks[0]["text"]["text"] == "*<https://jira/browse/OPS-7|OPS-7>*  Export fails"
    fields = [f["text"] for f in blocks[1]["fields"]]
    assert "*Assignee*\n<@U123>" in fields
    assert "AI summary: Export is broken." in json.dumps(blocks)
    assert "<https://sync/r/abc|Full history>" in json.dumps(blocks)


def test_user_text_cannot_ping_or_inject_links() -> None:
    text, blocks = build_message(
        _notice(
            title="<!channel> urgent & <https://evil|click>",
            reporter="<@U999>",
            ai_summary="<!here>",
        )
    )
    dumped = json.dumps(blocks) + text
    for raw in ("<!channel>", "<!here>", "<@U999>", "<https://evil"):
        assert raw not in dumped
    assert "&lt;!channel&gt; urgent &amp;" in text


def test_message_without_assignee_asks_for_triage() -> None:
    text, blocks = build_message(
        _notice(slack_user=None, urgency=None, assignee_role=None, ai_summary=None)
    )
    assert text.endswith("needs manual triage")
    assert "AI summary" not in json.dumps(blocks)


def test_escape() -> None:
    assert escape("a<b>&c") == "a&lt;b&gt;&amp;c"


# ---------- Slack client ----------


@pytest.fixture
def slack_mock() -> respx.MockRouter:
    with respx.mock(base_url=SLACK_API, assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
async def slack() -> AsyncIterator[SlackClient]:
    async with httpx.AsyncClient() as http:
        yield SlackClient(http, "xoxb-secret")


async def test_post_message(slack: SlackClient, slack_mock: respx.MockRouter) -> None:
    route = slack_mock.post("/chat.postMessage").respond(200, json={"ok": True, "ts": "1.2"})

    assert await slack.post_message("C1", "hi", [{"type": "divider"}]) == "1.2"

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer xoxb-secret"
    body = json.loads(request.content)
    assert body["channel"] == "C1"
    assert body["unfurl_links"] is False


@pytest.mark.parametrize(
    ("response", "retryable", "retry_after"),
    [
        (httpx.Response(429, headers={"Retry-After": "30"}), True, 30.0),
        (httpx.Response(503), True, None),
        (httpx.Response(200, json={"ok": False, "error": "ratelimited"}), True, None),
        (httpx.Response(200, json={"ok": False, "error": "not_in_channel"}), False, None),
        (httpx.Response(200, json={"ok": False, "error": "invalid_auth"}), False, None),
    ],
)
async def test_slack_errors_are_classified(
    slack: SlackClient,
    slack_mock: respx.MockRouter,
    response: httpx.Response,
    retryable: bool,
    retry_after: float | None,
) -> None:
    slack_mock.post("/chat.postMessage").mock(return_value=response)
    with pytest.raises(SlackError) as exc_info:
        await slack.post_message("C1", "hi", [])
    assert exc_info.value.retryable is retryable
    assert exc_info.value.retry_after == retry_after
    assert "xoxb-secret" not in str(exc_info.value)


async def test_slack_timeout_is_retryable(slack: SlackClient, slack_mock: respx.MockRouter) -> None:
    slack_mock.post("/chat.postMessage").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(SlackError) as exc_info:
        await slack.post_message("C1", "hi", [])
    assert exc_info.value.retryable
