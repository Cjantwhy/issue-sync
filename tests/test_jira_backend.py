import base64
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from issue_sync.tickets.base import (
    DuplicateTickets,
    TicketAuthError,
    TicketBackendUnavailable,
    TicketContent,
    TicketDraft,
    TicketNotFound,
    TicketRequestRejected,
    TransitionNotAvailable,
)
from issue_sync.tickets.jira.backend import JiraBackend
from tests.jira_fixtures import EMAIL, SITE, TOKEN

SYSTEM_ID = uuid.UUID("0b8f9a52-6c1e-4d5a-9a43-2f1d8e7c6b5a")


def _draft(**overrides: object) -> TicketDraft:
    data: dict[str, object] = {
        "system_id": SYSTEM_ID,
        "title": "Export\nfails",
        "description": "Clicking export shows an error.",
        "reporter": "Alice",
        "contact": "alice@example.com",
        "detail_url": "https://sync.example.com/r/abc",
        "urgency": "high",
        "assignee_role": "backend",
    }
    data.update(overrides)
    return TicketDraft.model_validate(data)


def _body(route: respx.Route, call: int = -1) -> dict:
    return json.loads(route.calls[call].request.content)


# ---------- create ----------


async def test_create_ticket_maps_internal_values(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.post("/issue").respond(201, json={"id": "10001", "key": "OPS-7"})

    ref = await backend.create_ticket(_draft())

    assert ref.key == "OPS-7"
    assert ref.url == f"{SITE}/browse/OPS-7"
    fields = _body(route)["fields"]
    assert fields["project"] == {"key": "OPS"}
    assert fields["issuetype"] == {"name": "Task"}
    assert fields["summary"] == "Export fails"
    assert fields["priority"] == {"name": "High"}
    assert fields["assignee"] == {"accountId": "acc-backend"}
    assert fields["labels"] == [f"issue-sync-{SYSTEM_ID}"]
    assert fields["description"]["type"] == "doc"


async def test_requests_use_api_token_basic_auth(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.post("/issue").respond(201, json={"key": "OPS-7"})
    await backend.create_ticket(_draft())
    expected = base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
    assert route.calls.last.request.headers["Authorization"] == f"Basic {expected}"


async def test_create_without_judgement_leaves_jira_defaults(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.post("/issue").respond(201, json={"key": "OPS-8"})
    await backend.create_ticket(_draft(urgency=None, assignee_role=None))
    fields = _body(route)["fields"]
    assert "priority" not in fields
    assert "assignee" not in fields


async def test_create_with_unmapped_value_fails_before_calling_jira(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.post("/issue")
    with pytest.raises(ValueError, match="no priority mapping for 'trivial'"):
        await backend.create_ticket(_draft(urgency="trivial"))
    assert not route.called


# ---------- find_by_system_id ----------


@pytest.mark.parametrize(("issues", "expected_key"), [([], None), ([{"key": "OPS-7"}], "OPS-7")])
async def test_find_by_system_id(
    backend: JiraBackend,
    jira_mock: respx.MockRouter,
    issues: list[dict],
    expected_key: str | None,
) -> None:
    route = jira_mock.post("/search/jql").respond(200, json={"issues": issues, "isLast": True})

    ref = await backend.find_by_system_id(SYSTEM_ID)

    assert (ref.key if ref else None) == expected_key
    assert _body(route)["jql"] == f'project = "OPS" AND labels = "issue-sync-{SYSTEM_ID}"'


async def test_find_by_system_id_reports_duplicates(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.post("/search/jql").respond(
        200, json={"issues": [{"key": "OPS-7"}, {"key": "OPS-9"}], "isLast": True}
    )
    with pytest.raises(DuplicateTickets, match="OPS-7, OPS-9"):
        await backend.find_by_system_id(SYSTEM_ID)


# ---------- update / comment ----------


async def test_update_content_only_touches_sheet_owned_fields(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    route = jira_mock.put("/issue/OPS-7").respond(204)
    content = TicketContent.model_validate(_draft(title="New title").model_dump())

    await backend.update_content("OPS-7", content)

    assert set(_body(route)["fields"]) == {"summary", "description"}
    assert _body(route)["fields"]["summary"] == "New title"


async def test_add_comment_sends_adf(backend: JiraBackend, jira_mock: respx.MockRouter) -> None:
    route = jira_mock.post("/issue/OPS-7/comment").respond(201, json={"id": "1"})
    await backend.add_comment("OPS-7", "Row was deleted from the sheet.")
    assert _body(route)["body"] == {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [
                {"type": "text", "text": "Row was deleted from the sheet."}
            ]}
        ],
    }  # fmt: skip


async def test_empty_comment_is_rejected(backend: JiraBackend) -> None:
    with pytest.raises(ValueError, match="empty"):
        await backend.add_comment("OPS-7", " \n ")


@pytest.mark.parametrize("key", ["OPS-7/../../myself", "ops-7", "OPS-0", "OPS"])
async def test_malformed_issue_keys_are_rejected(backend: JiraBackend, key: str) -> None:
    with pytest.raises(ValueError, match="Not a Jira issue key"):
        await backend.get_ticket(key)


# ---------- transition ----------

TRANSITIONS = {
    "transitions": [
        {"id": "21", "name": "Start work", "to": {"name": "In Progress"}},
        {"id": "31", "name": "Resolve", "to": {"name": "Done"}},
    ]
}


def _status(name: str) -> dict:
    return {"key": "OPS-7", "fields": {"status": {"name": name}}}


async def test_transition_uses_matching_workflow_transition(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.get("/issue/OPS-7").respond(200, json=_status("To Do"))
    jira_mock.get("/issue/OPS-7/transitions").respond(200, json=TRANSITIONS)
    post = jira_mock.post("/issue/OPS-7/transitions").respond(204)

    assert await backend.transition("OPS-7", "done") is True
    assert _body(post) == {"transition": {"id": "31"}}


async def test_transition_is_noop_when_already_there(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.get("/issue/OPS-7").respond(200, json=_status("in progress"))
    listing = jira_mock.get("/issue/OPS-7/transitions")

    assert await backend.transition("OPS-7", "in_progress") is False
    assert not listing.called


async def test_transition_not_available(backend: JiraBackend, jira_mock: respx.MockRouter) -> None:
    jira_mock.get("/issue/OPS-7").respond(200, json=_status("Done"))
    jira_mock.get("/issue/OPS-7/transitions").respond(
        200, json={"transitions": [{"id": "41", "name": "Reopen", "to": {"name": "In Progress"}}]}
    )
    with pytest.raises(TransitionNotAvailable, match=r"'Done' to 'To Do'.*In Progress"):
        await backend.transition("OPS-7", "open")


# ---------- get_ticket ----------


async def test_get_ticket_snapshot(backend: JiraBackend, jira_mock: respx.MockRouter) -> None:
    route = jira_mock.get("/issue/OPS-7").respond(
        200,
        json={
            "key": "OPS-7",
            "fields": {
                "status": {"name": "In Progress"},
                "priority": {"name": "Highest"},
                "assignee": {"accountId": "acc-ops", "displayName": "Olga Ops"},
                "updated": "2026-11-17T09:30:00.000+0800",
            },
        },
    )

    snapshot = await backend.get_ticket("OPS-7")

    assert route.calls.last.request.url.params["fields"] == "status,priority,assignee,updated"
    assert snapshot.status == "in_progress"
    assert snapshot.external_status == "In Progress"
    assert snapshot.priority == "Highest"
    assert (snapshot.assignee_id, snapshot.assignee_name) == ("acc-ops", "Olga Ops")
    assert snapshot.updated_at == datetime(2026, 11, 17, 1, 30, tzinfo=UTC)
    assert snapshot.updated_at.utcoffset() == timedelta(hours=8)


async def test_get_ticket_unassigned_and_unmapped_status(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.get("/issue/OPS-7").respond(
        200,
        json={
            "key": "OPS-7",
            "fields": {
                "status": {"name": "Blocked"},
                "priority": None,
                "assignee": None,
                "updated": "2026-11-17T09:30:00.000+0000",
            },
        },
    )
    snapshot = await backend.get_ticket("OPS-7")
    assert snapshot.status is None
    assert snapshot.external_status == "Blocked"
    assert snapshot.priority is None
    assert snapshot.assignee_id is None
    assert snapshot.updated_at.tzinfo == UTC


# ---------- error mapping ----------


async def test_rate_limit_is_retryable_with_retry_after(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.get("/issue/OPS-7").respond(429, headers={"Retry-After": "30"}, json={})
    with pytest.raises(TicketBackendUnavailable) as exc_info:
        await backend.get_ticket("OPS-7")
    assert exc_info.value.retryable
    assert exc_info.value.retry_after == 30.0


@pytest.mark.parametrize(
    ("status", "error", "retryable"),
    [
        (500, TicketBackendUnavailable, True),
        (503, TicketBackendUnavailable, True),
        (401, TicketAuthError, False),
        (403, TicketAuthError, False),
        (404, TicketNotFound, False),
        (400, TicketRequestRejected, False),
    ],
)
async def test_http_errors_are_classified(
    backend: JiraBackend,
    jira_mock: respx.MockRouter,
    status: int,
    error: type[Exception],
    retryable: bool,
) -> None:
    jira_mock.get("/issue/OPS-7").respond(status, json={"errorMessages": ["nope"]})
    with pytest.raises(error) as exc_info:
        await backend.get_ticket("OPS-7")
    assert exc_info.value.retryable is retryable  # type: ignore[attr-defined]


async def test_rejected_token_answered_with_404_is_an_auth_error(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    # Observed on Jira Cloud: a bad token on a resource endpoint yields 404, not 401.
    jira_mock.get("/issue/OPS-7").respond(
        404,
        headers={"X-Seraph-LoginReason": "AUTHENTICATED_FAILED"},
        json={"errorMessages": ["Issue does not exist or you do not have permission to see it."]},
    )
    with pytest.raises(TicketAuthError):
        await backend.get_ticket("OPS-7")


async def test_validation_error_details_are_surfaced(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.post("/issue").respond(
        400,
        json={
            "errorMessages": [],
            "errors": {"priority": "Field 'priority' cannot be set. It is not on the screen."},
        },
    )
    with pytest.raises(TicketRequestRejected, match="priority: Field 'priority' cannot be set"):
        await backend.create_ticket(_draft())


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ConnectError("refused")])
async def test_network_failures_are_retryable(
    backend: JiraBackend, jira_mock: respx.MockRouter, exc: Exception
) -> None:
    jira_mock.post("/issue").mock(side_effect=exc)
    with pytest.raises(TicketBackendUnavailable) as exc_info:
        await backend.create_ticket(_draft())
    assert exc_info.value.retryable


async def test_token_never_appears_in_errors(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    jira_mock.get("/issue/OPS-7").respond(401, text="Unauthorized")
    with pytest.raises(TicketAuthError) as exc_info:
        await backend.get_ticket("OPS-7")
    assert TOKEN not in str(exc_info.value)
