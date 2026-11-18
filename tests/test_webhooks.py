import pytest
from fastapi.testclient import TestClient

from issue_sync.api import webhooks
from issue_sync.api.app import create_app
from tests.settings_factory import make_settings

SECRET = "s" * 40
EVENT = {"spreadsheet_id": "1AbC", "worksheet_name": "Requests"}
DOCUMENTS = {("1AbC", "Requests"): (7, True), ("1AbC", "Archive"): (8, False)}


@pytest.fixture
def queued() -> list[int]:
    return []


@pytest.fixture
def form_rows() -> list[int | None]:
    return []


@pytest.fixture
def client(queued: list[int], form_rows: list[int | None]) -> TestClient:
    app = create_app(make_settings(sheets_webhook_secret=SECRET))

    async def find(spreadsheet_id: str, worksheet_name: str) -> tuple[int, bool] | None:
        return DOCUMENTS.get((spreadsheet_id, worksheet_name))

    async def enqueue(document_id: int, form_row: int | None) -> None:
        queued.append(document_id)
        form_rows.append(form_row)

    app.dependency_overrides[webhooks.get_document_finder] = lambda: find
    app.dependency_overrides[webhooks.get_sync_enqueuer] = lambda: enqueue
    return TestClient(app)  # lifespan not started: no real DB or Redis


def _post(client: TestClient, payload: object, secret: str | None = SECRET):  # type: ignore[no-untyped-def]
    headers = {webhooks.SECRET_HEADER: secret} if secret is not None else {}
    return client.post("/webhooks/sheets", json=payload, headers=headers)


def test_valid_event_enqueues_a_sync(client: TestClient, queued: list[int]) -> None:
    response = _post(client, EVENT)
    assert response.status_code == 202
    assert response.json() == {"status": "queued"}
    assert queued == [7]


@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET[:-1]])
def test_bad_secret_is_rejected_before_anything_else(
    client: TestClient, queued: list[int], secret: str | None
) -> None:
    # Even a malformed body gets 401, not 422: unauthenticated callers learn nothing.
    response = _post(client, {"junk": True}, secret)
    assert response.status_code == 401
    assert queued == []


def test_unknown_worksheet_is_404(client: TestClient, queued: list[int]) -> None:
    response = _post(client, {**EVENT, "worksheet_name": "Other"})
    assert response.status_code == 404
    assert queued == []


def test_disabled_document_is_ignored(client: TestClient, queued: list[int]) -> None:
    response = _post(client, {**EVENT, "worksheet_name": "Archive"})
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}
    assert queued == []


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, None),
        ({"source": "edit", "row": 5}, None),  # only a form submission names its row
        ({"source": "change"}, None),
        ({"source": "form", "row": 5}, 5),
        ({"source": "form"}, None),
    ],
)
def test_form_submission_passes_its_row(
    client: TestClient, form_rows: list[int | None], extra: dict, expected: int | None
) -> None:
    assert _post(client, {**EVENT, **extra}).status_code == 202
    assert form_rows == [expected]


@pytest.mark.parametrize("extra", [{"source": "other"}, {"source": "form", "row": 1}])
def test_invalid_source_or_row_is_422(client: TestClient, extra: dict) -> None:
    assert _post(client, {**EVENT, **extra}).status_code == 422


def test_malformed_body_is_422(client: TestClient) -> None:
    assert _post(client, {"spreadsheet_id": ""}).status_code == 422
