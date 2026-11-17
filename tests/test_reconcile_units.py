import json
import uuid

import httpx
import pytest
import respx

from issue_sync.notify.alerts import Alert, Alerter, build_alert_message
from issue_sync.notify.slack import SlackError
from issue_sync.tickets.base import TicketContent
from issue_sync.tickets.jira import adf
from issue_sync.tickets.jira.backend import JiraBackend, build_description, system_id_label

SID = uuid.UUID("0b8f9a52-6c1e-4d5a-9a43-2f1d8e7c6b5a")
OTHER = uuid.UUID("1c9fab63-7d2f-4e6b-8b54-3e2f9f8d7c6b")
CONTENT = TicketContent(
    title="Export fails",
    description="Clicking export errors.",
    reporter="Ann",
    contact=None,
    detail_url="https://sync.example.com/r/abc",
)


# ---------- alerts ----------


class Marks:
    def __init__(self) -> None:
        self.keys: dict[str, int] = {}

    async def seen(self, key: str) -> bool:
        return key in self.keys

    async def remember(self, key: str, ttl_seconds: int) -> None:
        self.keys[key] = ttl_seconds


class Slack:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.posts: list[tuple[str, str, list]] = []

    async def post_message(self, channel: str, text: str, blocks: list) -> str:
        if self.fail:
            raise SlackError("not_in_channel")
        self.posts.append((channel, text, blocks))
        return "1"


A1 = Alert("push_failed", "record:1", "Record 1 failed: <!channel>")
A2 = Alert("mass_deletion", "document:2", "Rows missing")


async def test_alerts_are_combined_and_not_repeated() -> None:
    slack, marks = Slack(), Marks()
    alerter = Alerter(slack, "C-ALERTS", marks)  # type: ignore[arg-type]

    assert await alerter.send([A1, A2, A1]) == 2
    assert await alerter.send([A1, A2]) == 0

    [(channel, text, blocks)] = slack.posts
    assert channel == "C-ALERTS"
    assert "2 problem(s)" in text
    body = blocks[1]["text"]["text"]
    assert "&lt;!channel&gt;" in body and "<!channel>" not in body
    assert marks.keys == {"push_failed:record:1": 86400, "mass_deletion:document:2": 86400}


async def test_failed_post_is_not_marked_as_sent() -> None:
    marks = Marks()
    with pytest.raises(SlackError):
        await Alerter(Slack(fail=True), "C", marks).send([A1])  # type: ignore[arg-type]
    assert marks.keys == {}


def test_long_alert_lists_are_capped() -> None:
    alerts = [Alert("x", str(i), f"problem {i}") for i in range(25)]
    _, blocks = build_alert_message(alerts)
    assert "…and 5 more" in blocks[1]["text"]["text"]


# ---------- Jira batch lookup ----------


def _issue(key: str, system_id: uuid.UUID, **fields: object) -> dict:
    return {
        "key": key,
        "fields": {
            "status": {"name": "Done"},
            "priority": {"name": "High"},
            "assignee": None,
            "updated": "2026-11-17T16:21:45.409+0800",
            "summary": "Export fails",
            "description": build_description(CONTENT),
            "labels": ["other", system_id_label(system_id)],
            "comment": {"comments": [], "total": 0},
            **fields,
        },
    }


def _comment(comment_id: str) -> dict:
    return {
        "id": comment_id,
        "author": {"accountId": "acc-human", "displayName": "Olga"},
        "body": adf.doc(adf.paragraph(adf.text(f"note {comment_id}"))),
        "created": "2026-11-17T16:21:45.346+0800",
    }


async def test_find_tickets_searches_by_label_and_reads_state(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.post("/search/jql").respond(
        200,
        json={
            "issues": [
                _issue("OPS-7", SID, comment={"comments": [_comment("1")], "total": 1}),
                _issue("OPS-8", OTHER, labels=["unrelated"]),  # not ours: skipped
            ],
            "isLast": True,
        },
    )

    [report] = await backend.find_tickets([SID, OTHER])

    body = json.loads(route.calls.last.request.content)
    assert body["jql"] == (
        f'project = "OPS" AND labels in ("{system_id_label(SID)}", "{system_id_label(OTHER)}")'
    )
    assert "comment" in body["fields"] and "description" in body["fields"]
    assert (report.key, report.system_id, report.status) == ("OPS-7", SID, "done")
    assert [c.text for c in report.comments] == ["note 1"]
    assert backend.content_matches(CONTENT, report)


async def test_find_tickets_pages_through_many_comments(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.post("/search/jql").respond(
        200,
        json={
            "issues": [_issue("OPS-7", SID, comment={"comments": [_comment("1")], "total": 2})],
            "isLast": True,
        },
    )
    jira_mock.get("/issue/OPS-7/comment").respond(
        200, json={"comments": [_comment("1"), _comment("2")], "startAt": 0, "total": 2}
    )

    [report] = await backend.find_tickets([SID])

    assert [c.id for c in report.comments] == ["1", "2"]


async def test_find_tickets_batches_and_follows_pages(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    ids = [uuid.uuid4() for _ in range(150)]
    route = jira_mock.post("/search/jql").mock(
        side_effect=[
            httpx.Response(200, json={"issues": [], "isLast": False, "nextPageToken": "t2"}),
            httpx.Response(200, json={"issues": [], "isLast": True}),
            httpx.Response(200, json={"issues": [], "isLast": True}),
        ]
    )

    assert await backend.find_tickets(ids) == []

    bodies = [json.loads(call.request.content) for call in route.calls]
    assert [b.get("nextPageToken") for b in bodies] == [None, "t2", None]
    assert bodies[0]["jql"].count("issue-sync-") == 100
    assert bodies[2]["jql"].count("issue-sync-") == 50


@pytest.mark.parametrize(
    ("override", "matches"),
    [
        ({}, True),
        ({"summary": "Export fails (edited in Jira)"}, False),
        ({"description": adf.doc(adf.paragraph(adf.text("Rewritten")))}, False),
        # Jira stores extra attributes such as localId; the visible text is what counts.
        (
            {
                "description": {
                    **build_description(CONTENT),
                    "content": [
                        {**block, "attrs": {"localId": "abc"}}
                        for block in build_description(CONTENT)["content"]
                    ],
                }
            },
            True,
        ),
    ],
)
async def test_content_matches_compares_visible_text(
    backend: JiraBackend, jira_mock: respx.MockRouter, override: dict, matches: bool
) -> None:
    jira_mock.post("/search/jql").respond(
        200, json={"issues": [_issue("OPS-7", SID, **override)], "isLast": True}
    )
    [report] = await backend.find_tickets([SID])
    assert backend.content_matches(CONTENT, report) is matches
