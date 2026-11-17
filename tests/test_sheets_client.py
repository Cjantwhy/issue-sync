import json
import urllib.parse
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth import crypt
from google.auth import jwt as google_jwt

from issue_sync.sources.sheets.auth import ServiceAccountKey, TokenProvider
from issue_sync.sources.sheets.client import SHEETS_API, CellWrite, SheetsClient
from issue_sync.sources.sheets.errors import SheetsError

TOKEN_URI = "https://oauth2.googleapis.com/token"
CLIENT_EMAIL = "sync@demo-project.iam.gserviceaccount.com"
SPREADSHEET = "1AbC-d_9"


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def key_info(private_key: rsa.RSAPrivateKey) -> dict[str, str]:
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return {
        "type": "service_account",
        "client_email": CLIENT_EMAIL,
        "private_key": pem,
        "private_key_id": "kid-1",
        "token_uri": TOKEN_URI,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def google_mock() -> respx.MockRouter:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as mock:
        yield mock


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def sheets(key_info: dict[str, str], http: httpx.AsyncClient, clock: Clock) -> SheetsClient:
    return SheetsClient(TokenProvider(ServiceAccountKey(key_info), http, clock=clock), http)


def _token_route(mock: respx.MockRouter, *tokens: str) -> respx.Route:
    return mock.post(TOKEN_URI).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": t, "expires_in": 3599}) for t in tokens
        ]
    )


def _values_url(worksheet: str) -> str:
    a1 = urllib.parse.quote("'" + worksheet.replace("'", "''") + "'", safe="")
    return f"{SHEETS_API}/{SPREADSHEET}/values/{a1}"


# ---------- auth ----------


async def test_token_request_is_a_signed_jwt_bearer_grant(
    sheets: SheetsClient, google_mock: respx.MockRouter, private_key: rsa.RSAPrivateKey
) -> None:
    token = _token_route(google_mock, "tok-1")
    google_mock.get(_values_url("Requests")).respond(200, json={"values": []})

    await sheets.read_values(SPREADSHEET, "Requests")

    form = urllib.parse.parse_qs(token.calls.last.request.content.decode())
    assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:jwt-bearer"]
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    claims = google_jwt.decode(
        form["assertion"][0], certs=crypt.RSAVerifier.from_string(public_pem), verify=False
    )
    assert claims["iss"] == CLIENT_EMAIL
    assert claims["aud"] == TOKEN_URI
    assert claims["scope"] == "https://www.googleapis.com/auth/spreadsheets"
    assert claims["exp"] - claims["iat"] == 3600


async def test_token_is_cached_until_close_to_expiry(
    sheets: SheetsClient, google_mock: respx.MockRouter, clock: Clock
) -> None:
    token = _token_route(google_mock, "tok-1", "tok-2")
    read = google_mock.get(_values_url("Requests")).respond(200, json={})

    await sheets.read_values(SPREADSHEET, "Requests")
    clock.now += 3000  # still more than 5 minutes left
    await sheets.read_values(SPREADSHEET, "Requests")
    assert token.call_count == 1
    clock.now += 400  # inside the refresh margin
    await sheets.read_values(SPREADSHEET, "Requests")

    assert token.call_count == 2
    assert read.calls.last.request.headers["Authorization"] == "Bearer tok-2"


async def test_stale_token_is_replaced_once_on_401(
    sheets: SheetsClient, google_mock: respx.MockRouter
) -> None:
    _token_route(google_mock, "tok-1", "tok-2")
    read = google_mock.get(_values_url("Requests")).mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"values": [["Title"]]})]
    )
    assert await sheets.read_values(SPREADSHEET, "Requests") == [["Title"]]
    assert read.calls.last.request.headers["Authorization"] == "Bearer tok-2"


async def test_rejected_key_is_not_retryable(
    sheets: SheetsClient, google_mock: respx.MockRouter
) -> None:
    google_mock.post(TOKEN_URI).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(SheetsError, match="invalid_grant") as exc_info:
        await sheets.read_values(SPREADSHEET, "Requests")
    assert not exc_info.value.retryable


def test_key_file_errors(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        ServiceAccountKey.from_file(tmp_path / "missing.json")
    incomplete = tmp_path / "key.json"
    incomplete.write_text(json.dumps({"client_email": CLIENT_EMAIL}), encoding="utf-8")
    with pytest.raises(ValueError, match="private_key, token_uri"):
        ServiceAccountKey.from_file(incomplete)


# ---------- read / write ----------


async def test_read_values_quotes_the_worksheet_name(
    sheets: SheetsClient, google_mock: respx.MockRouter
) -> None:
    _token_route(google_mock, "tok-1")
    route = google_mock.get(_values_url("Bob's requests")).respond(
        200, json={"values": [["Title", "N"], ["Export", 3]]}
    )

    values = await sheets.read_values(SPREADSHEET, "Bob's requests")

    assert values == [["Title", "N"], ["Export", "3"]]
    assert route.calls.last.request.url.params["valueRenderOption"] == "FORMATTED_VALUE"


async def test_write_cells_sends_raw_values_in_one_batch(
    sheets: SheetsClient, google_mock: respx.MockRouter
) -> None:
    _token_route(google_mock, "tok-1")
    route = google_mock.post(f"{SHEETS_API}/{SPREADSHEET}/values:batchUpdate").respond(200, json={})

    await sheets.write_cells(
        SPREADSHEET, "Requests", [CellWrite(2, 4, "id-1"), CellWrite(2, 27, "=HYPERLINK(1)")]
    )

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "valueInputOption": "RAW",
        "data": [
            {"range": "'Requests'!E2", "values": [["id-1"]]},
            {"range": "'Requests'!AB2", "values": [["=HYPERLINK(1)"]]},
        ],
    }


async def test_write_nothing_makes_no_request(sheets: SheetsClient) -> None:
    await sheets.write_cells(SPREADSHEET, "Requests", [])  # respx would reject any call


@pytest.mark.parametrize(
    ("status", "retryable", "hint"),
    [(429, True, None), (503, True, None), (403, False, CLIENT_EMAIL), (404, False, None)],
)
async def test_errors_are_classified(
    sheets: SheetsClient,
    google_mock: respx.MockRouter,
    status: int,
    retryable: bool,
    hint: str | None,
) -> None:
    _token_route(google_mock, "tok-1")
    google_mock.get(_values_url("Requests")).respond(
        status, json={"error": {"message": "nope"}}, headers={"Retry-After": "7"}
    )
    with pytest.raises(SheetsError, match="nope") as exc_info:
        await sheets.read_values(SPREADSHEET, "Requests")
    assert exc_info.value.retryable is retryable
    if retryable:
        assert exc_info.value.retry_after == 7
    if hint:
        assert hint in str(exc_info.value)


async def test_network_failure_is_retryable(
    sheets: SheetsClient, google_mock: respx.MockRouter
) -> None:
    _token_route(google_mock, "tok-1")
    google_mock.get(_values_url("Requests")).mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(SheetsError) as exc_info:
        await sheets.read_values(SPREADSHEET, "Requests")
    assert exc_info.value.retryable


async def test_spreadsheet_id_cannot_change_the_path(sheets: SheetsClient) -> None:
    with pytest.raises(ValueError, match="Not a spreadsheet id"):
        await sheets.read_values("abc/../other", "Requests")
