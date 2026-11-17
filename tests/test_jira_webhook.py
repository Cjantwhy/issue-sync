import asyncio
import hashlib
import hmac
import json
import urllib.parse
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from issue_sync.api import webhooks
from issue_sync.api.app import create_app
from issue_sync.tickets.base import TicketAuthError
from issue_sync.tickets.jira import adf
from issue_sync.tickets.jira.auth import ATLASSIAN_TOKEN_URL, OAuthClientCredentials
from issue_sync.tickets.jira.backend import JiraBackend
from issue_sync.tickets.jira.client import JiraClient
from issue_sync.tickets.jira.webhook import slim_payload, valid_signature
from tests.settings_factory import make_settings

SECRET = "w" * 32
TS = 1_795_000_000_000  # milliseconds
STATUSES = [
    {
        "id": "10001",
        "name": "Task",
        "statuses": [
            {"id": "1", "name": "To Do"},
            {"id": "3", "name": "In Progress"},
            {"id": "4", "name": "In Review"},
            {"id": "5", "name": "Done"},
        ],
    }
]


def issue_updated(
    *, status: dict | None = None, actor: str = "acc-human", project: str = "OPS", **fields: Any
) -> dict[str, Any]:
    return {
        "timestamp": TS,
        "webhookEvent": "jira:issue_updated",
        "issue_event_type_name": "issue_generic",
        "user": {"accountId": actor, "displayName": "Olga Ops"},
        "issue": {
            "id": "10010",
            "key": f"{project}-7",
            "fields": {
                "project": {"key": project},
                "summary": "Export fails",
                "description": "x" * 5000,
                "status": status or {"id": "3", "name": "正在进行"},
                "priority": {"id": "2", "name": "High"},
                "assignee": {"accountId": "acc-ops", "displayName": "Olga Ops"},
                **fields,
            },
        },
        "changelog": {"items": [{"field": "status", "fromString": "待办", "toString": "正在进行"}]},
    }


def comment_created(body: Any = "Fixed in *1.4*", author: str = "acc-human") -> dict[str, Any]:
    return {
        "timestamp": TS,
        "webhookEvent": "comment_created",
        "comment": {
            "id": "10100",
            "author": {"accountId": author, "displayName": "Olga Ops"},
            "body": body,
            "created": "2026-11-17T10:00:00.000+0800",
        },
        "issue": {"id": "10010", "key": "OPS-7", "fields": {"summary": "Export fails"}},
    }


# ---------- signature ----------


def test_signature_matches_atlassians_documented_example() -> None:
    signature = "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"
    assert valid_signature(b"Hello World!", signature, "It's a Secret to Everybody")
    assert not valid_signature(b"Hello World?", signature, "It's a Secret to Everybody")
    assert not valid_signature(b"Hello World!", signature.removeprefix("sha256="), "x")
    assert not valid_signature(b"Hello World!", None, "x")


# ---------- parsing ----------


@pytest.fixture
def statuses_route(jira_mock: respx.MockRouter) -> respx.Route:
    return jira_mock.get("/project/OPS/statuses").respond(200, json=STATUSES)


async def test_status_is_resolved_by_id_not_by_localized_name(
    backend: JiraBackend, statuses_route: respx.Route
) -> None:
    # The payload says "正在进行"; jira.yaml uses what the service account sees.
    event = await backend.parse_webhook(issue_updated())

    assert event is not None and event.kind == "issue_updated"
    assert event.key == "OPS-7"
    assert event.occurred_at == datetime.fromtimestamp(TS / 1000, tz=UTC)
    assert (event.actor_id, event.actor_name) == ("acc-human", "Olga Ops")
    assert not event.from_service_account
    assert event.state is not None
    assert (event.state.status, event.state.external_status) == ("in_progress", "In Progress")
    assert (event.state.priority, event.state.assignee_name) == ("High", "Olga Ops")


async def test_unmapped_status_and_status_cache(
    backend: JiraBackend, statuses_route: respx.Route
) -> None:
    review = await backend.parse_webhook(issue_updated(status={"id": "4", "name": "审查中"}))
    assert review is not None and review.state is not None
    assert (review.state.status, review.state.external_status) == (None, "In Review")
    await backend.parse_webhook(issue_updated())
    # An id that does not exist reloads at most once a minute.
    await backend.parse_webhook(issue_updated(status={"id": "99", "name": "New"}))
    await backend.parse_webhook(issue_updated(status={"id": "98", "name": "Newer"}))
    assert statuses_route.call_count == 1


async def test_service_account_events_are_flagged(
    backend: JiraBackend, statuses_route: respx.Route
) -> None:
    event = await backend.parse_webhook(issue_updated(actor="acc-bot"))
    assert event is not None and event.from_service_account


@pytest.mark.parametrize(
    "payload",
    [
        issue_updated(project="HR"),
        {**issue_updated(), "webhookEvent": "jira:issue_deleted"},
        {**issue_updated(), "issue": {"key": "OPS-7/../x", "fields": {}}},
    ],
)
async def test_irrelevant_events_parse_to_none(backend: JiraBackend, payload: dict) -> None:
    assert await backend.parse_webhook(payload) is None


async def test_comment_with_wiki_markup_body(backend: JiraBackend) -> None:
    event = await backend.parse_webhook(comment_created())
    assert event is not None and event.comment is not None
    assert event.comment.text == "Fixed in *1.4*"
    assert event.comment.id == "10100"
    assert event.comment.created_at == datetime(2026, 11, 17, 2, 0, tzinfo=UTC)
    assert event.state is None


async def test_comment_with_adf_body(backend: JiraBackend) -> None:
    body = adf.doc(
        adf.paragraph(adf.text("Deployed "), {"type": "mention", "attrs": {"text": "@Ann"}}),
        adf.bullet_list([adf.text("one")], [adf.text("two")]),
    )
    event = await backend.parse_webhook(comment_created(body))
    assert event is not None and event.comment is not None
    assert event.comment.text == "Deployed @Ann\n- one\n- two"


def test_slim_payload_drops_bulky_fields() -> None:
    slim = slim_payload(issue_updated())
    assert set(slim["issue"]["fields"]) == {"project", "status", "priority", "assignee"}
    assert "changelog" not in slim
    assert len(json.dumps(slim)) < 1000


# ---------- endpoint ----------


@pytest.fixture
def queued() -> list[dict]:
    return []


@pytest.fixture
def client(queued: list[dict]) -> TestClient:
    app = create_app(make_settings(jira_webhook_secret=SECRET))

    async def enqueue(payload: dict) -> None:
        queued.append(payload)

    app.dependency_overrides[webhooks.get_ticket_event_enqueuer] = lambda: enqueue
    app.dependency_overrides[webhooks.get_service_account_id] = lambda: "acc-bot"
    return TestClient(app)


def _post(client: TestClient, payload: object, *, secret: str = SECRET, raw: bytes | None = None):  # type: ignore[no-untyped-def]
    body = raw if raw is not None else json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/jira",
        content=body,
        headers={"X-Hub-Signature": signature, "Content-Type": "application/json"},
    )


def test_signed_event_is_queued_slim(client: TestClient, queued: list[dict]) -> None:
    response = _post(client, issue_updated())
    assert response.status_code == 202 and response.json() == {"status": "queued"}
    assert queued == [slim_payload(issue_updated())]


def test_wrong_signature_is_rejected(client: TestClient, queued: list[dict]) -> None:
    assert _post(client, issue_updated(), secret="x" * 32).status_code == 401
    assert client.post("/webhooks/jira", json=issue_updated()).status_code == 401
    assert queued == []


@pytest.mark.parametrize(
    "payload",
    [issue_updated(actor="acc-bot"), comment_created(author="acc-bot"), {"webhookEvent": "x"}],
)
def test_own_and_unsupported_events_are_ignored(
    client: TestClient, queued: list[dict], payload: dict
) -> None:
    response = _post(client, payload)
    assert response.json() == {"status": "ignored"}
    assert queued == []


def test_signed_garbage_is_400(client: TestClient) -> None:
    assert _post(client, None, raw=b"not json").status_code == 400


def test_api_docs_are_off_by_default() -> None:
    client = TestClient(create_app(make_settings()))
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    enabled = TestClient(create_app(make_settings(enable_api_docs=True)))
    assert enabled.get("/openapi.json").status_code == 200


# ---------- OAuth 2.0 client credentials ----------

GATEWAY = "https://api.atlassian.com/ex/jira/cloud-1"


class Clock:
    now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def oauth_mock() -> respx.MockRouter:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
async def oauth_client() -> AsyncIterator[tuple[JiraClient, Clock]]:
    clock = Clock()
    auth = OAuthClientCredentials("client-1", "very-secret", clock=clock)
    async with JiraClient("https://example.atlassian.net", auth, api_base_url=GATEWAY) as client:
        yield client, clock


def _tokens(mock: respx.MockRouter, *tokens: str) -> respx.Route:
    return mock.post(ATLASSIAN_TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": t, "expires_in": 3600}) for t in tokens
        ]
    )


async def test_oauth_exchanges_client_credentials_and_uses_the_gateway(
    oauth_client: tuple[JiraClient, Clock], oauth_mock: respx.MockRouter
) -> None:
    client, clock = oauth_client
    token = _tokens(oauth_mock, "t1", "t2")
    myself = oauth_mock.get(f"{GATEWAY}/rest/api/3/myself").respond(200, json={})

    await client.request("GET", "/myself")
    await client.request("GET", "/myself")
    clock.now += 3500  # within the refresh margin
    await client.request("GET", "/myself")

    form = urllib.parse.parse_qs(token.calls[0].request.content.decode())
    assert form == {
        "grant_type": ["client_credentials"],
        "client_id": ["client-1"],
        "client_secret": ["very-secret"],
    }
    assert token.call_count == 2
    assert myself.calls[0].request.headers["Authorization"] == "Bearer t1"
    assert myself.calls[2].request.headers["Authorization"] == "Bearer t2"
    assert client.browse_url("OPS-7") == "https://example.atlassian.net/browse/OPS-7"


async def test_oauth_refreshes_once_after_a_401(
    oauth_client: tuple[JiraClient, Clock], oauth_mock: respx.MockRouter
) -> None:
    client, _ = oauth_client
    _tokens(oauth_mock, "revoked", "fresh")
    route = oauth_mock.get(f"{GATEWAY}/rest/api/3/myself").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"accountId": "a"})]
    )
    assert await client.request("GET", "/myself") == {"accountId": "a"}
    assert route.calls.last.request.headers["Authorization"] == "Bearer fresh"


async def test_oauth_rejected_credentials_are_an_auth_error(
    oauth_client: tuple[JiraClient, Clock], oauth_mock: respx.MockRouter
) -> None:
    client, _ = oauth_client
    oauth_mock.post(ATLASSIAN_TOKEN_URL).respond(401, json={"error": "access_denied"})
    with pytest.raises(TicketAuthError) as exc_info:
        await client.request("GET", "/myself")
    assert "very-secret" not in str(exc_info.value)


def test_adf_to_plain_text_handles_unknown_nodes() -> None:
    assert adf.to_plain_text({"type": "doc", "content": [{"type": "weird"}]}) == ""
    assert adf.to_plain_text(None) == ""


async def test_concurrent_events_wait_for_one_status_load(
    backend: JiraBackend, jira_mock: respx.MockRouter
) -> None:
    # Seen live: two webhooks for one Jira action arrive together. The second must not
    # fall back to the payload's localized name while the first is loading statuses.
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(200, json=STATUSES)

    route = jira_mock.get("/project/OPS/statuses").mock(side_effect=slow)
    done = {"id": "5", "name": "已完成"}

    first, second = await asyncio.gather(
        backend.parse_webhook(issue_updated(status=done)),
        backend.parse_webhook(issue_updated(status=done)),
    )

    assert route.call_count == 1
    for event in (first, second):
        assert event is not None and event.state is not None
        assert (event.state.status, event.state.external_status) == ("done", "Done")
