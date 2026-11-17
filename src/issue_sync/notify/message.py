"""The Slack message for a new ticket (Block Kit).

All text that came from the sheet or the LLM is escaped: in Slack's mrkdwn, `<!channel>`
or `<@U123>` in a request title would otherwise ping people.
"""

from dataclasses import dataclass
from typing import Any

TITLE_MAX = 200
SUMMARY_MAX = 500


@dataclass(frozen=True)
class TicketNotice:
    issue_key: str
    issue_url: str
    detail_url: str
    title: str | None
    reporter: str | None
    urgency: str | None
    assignee_role: str | None
    slack_user: str | None  # None: no assignee, needs manual triage
    ai_summary: str | None = None
    rule_note: str | None = None


def escape(text: str) -> str:
    """Slack mrkdwn control characters (https://api.slack.com/reference/surfaces/formatting)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str | None, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def build_message(notice: TicketNotice) -> tuple[str, list[dict[str, Any]]]:
    title = escape(_clip(notice.title, TITLE_MAX)) or "_(untitled request)_"
    assignee = f"<@{notice.slack_user}>" if notice.slack_user else "_needs manual triage_"
    fallback = f"New ticket {notice.issue_key}: {escape(_clip(notice.title, TITLE_MAX))} - " + (
        f"assigned to <@{notice.slack_user}>" if notice.slack_user else "needs manual triage"
    )

    fields = [
        f"*Urgency*\n{escape(notice.urgency or 'not set')}",
        f"*Team*\n{escape(notice.assignee_role or 'not set')}",
        f"*Assignee*\n{assignee}",
        f"*Reporter*\n{escape(_clip(notice.reporter, 100)) or '-'}",
    ]
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*<{notice.issue_url}|{notice.issue_key}>*  {title}",
            },
        },
        {"type": "section", "fields": [{"type": "mrkdwn", "text": f} for f in fields]},
    ]
    if notice.ai_summary:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"AI summary: {escape(_clip(notice.ai_summary, SUMMARY_MAX))}",
                    }
                ],
            }
        )
    footer = [f"<{notice.detail_url}|Full history>"]
    if notice.rule_note:
        footer.append(escape(notice.rule_note))
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "  ·  ".join(footer)}]}
    )
    return fallback, blocks
