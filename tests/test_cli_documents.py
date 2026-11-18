"""`issue-sync add-document` against a real PostgreSQL (see tests/db_fixtures.py).

The Sheets API is replaced by a fixed header row.
"""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command

from issue_sync import cli
from tests.db_fixtures import alembic_config, integration, throwaway_database

pytestmark = integration

SPREADSHEET = "1AbCdEfGh"
HEADERS = ["Title", "Description", "Reporter", "System ID", "Detail link", "Status"]


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    with throwaway_database() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
def sheet_headers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    headers = list(HEADERS)

    class FixedSheet:
        async def read_values(self, spreadsheet_id: str, worksheet: str) -> list[list[str]]:
            return [list(headers)]

    monkeypatch.setattr(cli, "create_sheets_client", lambda settings, http: FixedSheet())
    return headers


async def _add(
    database_url: str, *maps: str, worksheet: str = "Requests", update: bool = False
) -> str:
    settings = SimpleNamespace(
        database_url=database_url, google_service_account_file=Path("unused.json")
    )
    return await cli._add_document(
        settings,  # type: ignore[arg-type]
        "Support requests",
        SPREADSHEET,
        worksheet,
        list(maps),
        update=update,
    )


async def test_register_then_update_the_mapping(
    database_url: str, sheet_headers: list[str]
) -> None:
    first = await _add(database_url)
    assert first.startswith("Registered document")
    assert "attachments" not in first  # no Attachments column yet

    with pytest.raises(cli.CliError, match="--update"):
        await _add(database_url)

    sheet_headers.append("Attachments")
    updated = await _add(database_url, update=True)

    assert updated.startswith("Updated document")
    assert "attachments  -> Attachments" in updated
    assert updated.split(":")[0].split()[-1] == first.split(":")[0].split()[-1]  # same id


async def test_update_maps_a_differently_named_column(
    database_url: str, sheet_headers: list[str]
) -> None:
    sheet_headers.append("上传的文件")
    output = await _add(
        database_url, "attachments=上传的文件", worksheet="Form responses", update=True
    )
    assert output.startswith("Registered document")  # --update also registers new ones
    assert "attachments  -> 上传的文件" in output
