"""Thin async client for the two Sheets API v4 calls the sync needs.

Like the Jira client, it classifies failures but does not retry transient errors; the
arq job does. The one exception is a single re-authentication after a 401, which only
means the cached access token went stale.
"""

import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from issue_sync.settings import GoogleSettings
from issue_sync.sources.sheets.auth import ServiceAccountKey, TokenProvider
from issue_sync.sources.sheets.errors import SheetsError

logger = logging.getLogger(__name__)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=5.0)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    if index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def quote_sheet(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


@dataclass(frozen=True)
class CellWrite:
    row: int  # 1-based sheet row number
    column: int  # 0-based column index
    value: str


class SheetsClient:
    def __init__(
        self, tokens: TokenProvider, http: httpx.AsyncClient, base_url: str = SHEETS_API
    ) -> None:
        self._tokens = tokens
        self._http = http
        self._base_url = base_url

    @property
    def client_email(self) -> str:
        return self._tokens.client_email

    async def read_values(self, spreadsheet_id: str, worksheet: str) -> list[list[str]]:
        """All rows of a worksheet as displayed (formatted strings).

        Trailing empty cells and rows are omitted by the API, so rows can be ragged.
        """
        a1 = urllib.parse.quote(quote_sheet(worksheet), safe="")
        body = await self._request(
            "GET",
            f"/{_spreadsheet(spreadsheet_id)}/values/{a1}",
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
        return [[str(cell) for cell in row] for row in body.get("values", [])]

    async def write_cells(
        self, spreadsheet_id: str, worksheet: str, cells: list[CellWrite]
    ) -> None:
        """Write single cells in one request. RAW: values are never parsed as formulas."""
        if not cells:
            return
        sheet = quote_sheet(worksheet)
        data = [
            {"range": f"{sheet}!{column_letter(c.column)}{c.row}", "values": [[c.value]]}
            for c in cells
        ]
        await self._request(
            "POST",
            f"/{_spreadsheet(spreadsheet_id)}/values:batchUpdate",
            json={"valueInputOption": "RAW", "data": data},
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> dict[str, Any]:
        for attempt in (1, 2):
            token = await self._tokens.token()
            try:
                response = await self._http.request(
                    method,
                    self._base_url + path,
                    params=params,
                    json=json,
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=DEFAULT_TIMEOUT,
                )
            except httpx.TimeoutException:
                raise SheetsError(f"Sheets {method} timed out", retryable=True) from None
            except httpx.TransportError as exc:
                raise SheetsError(f"Sheets {method} failed: {exc!r}", retryable=True) from None
            if response.status_code == 401 and attempt == 1:
                self._tokens.invalidate()
                continue
            if response.is_success:
                return response.json() if response.content else {}
            raise self._error_for(method, response)
        raise AssertionError("unreachable")

    def _error_for(self, method: str, response: httpx.Response) -> SheetsError:
        status = response.status_code
        message = f"Sheets {method} returned {status}: {_error_message(response)}"
        if status == 429 or status >= 500:
            retry_after = response.headers.get("Retry-After")
            return SheetsError(
                message,
                retryable=True,
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if status == 403:
            message += f" (is the spreadsheet shared with {self.client_email} as an editor?)"
        return SheetsError(message)


def create_sheets_client(settings: GoogleSettings, http: httpx.AsyncClient) -> SheetsClient:
    """The caller owns `http` and closes it."""
    key = ServiceAccountKey.from_file(settings.google_service_account_file)
    return SheetsClient(TokenProvider(key, http), http)


def _spreadsheet(spreadsheet_id: str) -> str:
    # Spreadsheet ids are [A-Za-z0-9_-]; reject anything that could change the path.
    if not spreadsheet_id or not all(ch.isalnum() or ch in "-_" for ch in spreadsheet_id):
        raise ValueError(f"Not a spreadsheet id: {spreadsheet_id!r}")
    return spreadsheet_id


def _error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:500]
    except (ValueError, KeyError, TypeError):
        return response.text[:300] or response.reason_phrase
