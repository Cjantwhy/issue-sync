import uuid

import pytest
from pydantic import ValidationError

from issue_sync.cli import CliError, build_mapping, spreadsheet_id_from
from issue_sync.settings import _valid_poll_interval
from issue_sync.sources.sheets.client import column_letter, quote_sheet
from issue_sync.sources.sheets.errors import SheetLayoutError
from issue_sync.sources.sheets.layout import DocumentMapping, parse_sheet
from issue_sync.sync.retry import backoff_seconds, poll_schedule, retry_delay

MAPPING = DocumentMapping(
    title="Title",
    description="Description",
    reporter="Reporter",
    system_id="System ID",
    detail_url="Detail link",
    status="Status",
)
HEADER = ["Title", "Description", "Reporter", "Team", "System ID", "Detail link", "Status"]
SID = "0b8f9a52-6c1e-4d5a-9a43-2f1d8e7c6b5a"


# ---------- mapping ----------


def test_mapping_rejects_unknown_fields_and_shared_headers() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        DocumentMapping.model_validate({"title": "T", "system_id": "S", "detail_url": "D", "x": 1})
    with pytest.raises(ValidationError, match="both map to header"):
        DocumentMapping(title="Same", system_id="same", detail_url="D")
    with pytest.raises(ValidationError, match="blank"):
        DocumentMapping(title="  ", system_id="S", detail_url="D")


def test_mapping_headers_omit_unset_fields() -> None:
    mapping = DocumentMapping(title=" Title ", system_id="ID", detail_url="Link")
    assert mapping.headers() == {"title": "Title", "system_id": "ID", "detail_url": "Link"}


# ---------- parse_sheet ----------


def test_columns_found_by_header_name_case_insensitively() -> None:
    header = ["detail link", "Team", "SYSTEM ID", "title", "Reporter", "Description", "status"]
    layout = parse_sheet([header, ["", "Ops", SID, "Login fails", "Bob", "Details", ""]], MAPPING)
    row = layout.rows[0]
    assert row.fields == {
        "title": "Login fails",
        "description": "Details",
        "reporter": "Bob",
        "contact": None,
    }
    assert row.parsed_system_id() == uuid.UUID(SID)
    assert layout.unmapped_headers == ["Team"]


def test_ragged_rows_blank_rows_and_row_numbers() -> None:
    values = [HEADER, ["First"], [], ["", "", "", "", "", ""], ["Second", "", "", "", SID]]
    layout = parse_sheet(values, MAPPING)
    assert [r.row_number for r in layout.rows] == [2, 5]
    assert layout.rows[0].fields["description"] is None
    assert layout.rows[0].system_id_cell == ""


def test_raw_hash_ignores_system_columns_but_not_unmapped_ones() -> None:
    base = ["Title", "Desc", "Bob", "Ops", "", "", ""]
    with_system = ["Title", "Desc", "Bob", "Ops", SID, "https://x/r/1", "Done"]
    other_team = ["Title", "Desc", "Bob", "Support", "", "", ""]
    a, b, c = (parse_sheet([HEADER, r], MAPPING).rows[0] for r in (base, with_system, other_team))
    assert a.raw_hash == b.raw_hash
    assert a.raw_hash != c.raw_hash
    assert b.raw["System ID"] == SID  # raw still keeps the whole row


def test_row_with_only_system_cells_has_no_content() -> None:
    layout = parse_sheet([HEADER, ["", "", "", "", "", "https://x/r/1"]], MAPPING)
    assert not layout.rows[0].has_content


def test_invalid_system_id_raises_on_parse() -> None:
    row = parse_sheet([HEADER, ["T", "", "", "", "not-a-uuid"]], MAPPING).rows[0]
    with pytest.raises(ValueError):
        row.parsed_system_id()


def test_blank_and_duplicate_headers_get_column_letter_keys() -> None:
    header = ["Title", "", "Note", "Note", "System ID", "Detail link"]
    row = parse_sheet([header, ["T", "x", "a", "b", "", "", "extra"]], MAPPING).rows[0]
    assert row.raw == {
        "Title": "T",
        "B": "x",
        "Note (C)": "a",
        "Note (D)": "b",
        "System ID": "",
        "Detail link": "",
        "G": "extra",
    }


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "empty"),
        ([["Title", "Description"]], "missing header(s): System ID, Detail link"),
        ([["Title", "title", "System ID", "Detail link"]], "more than once: Title"),
    ],
)
def test_layout_errors(values: list[list[str]], message: str) -> None:
    with pytest.raises(SheetLayoutError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_sheet(values, MAPPING)


def test_missing_optional_header_is_tolerated() -> None:
    layout = parse_sheet([["Title", "System ID", "Detail link"], ["T"]], MAPPING)
    assert sorted(layout.missing_optional) == ["description", "reporter", "status"]
    assert layout.rows[0].fields["reporter"] is None


# ---------- A1 helpers ----------


@pytest.mark.parametrize(("index", "letters"), [(0, "A"), (25, "Z"), (26, "AA"), (701, "ZZ")])
def test_column_letter(index: int, letters: str) -> None:
    assert column_letter(index) == letters


def test_quote_sheet_escapes_quotes() -> None:
    assert quote_sheet("Bob's sheet") == "'Bob''s sheet'"


# ---------- CLI mapping ----------


def test_build_mapping_defaults_follow_the_sheet() -> None:
    mapping = build_mapping([], ["Title", "Description", "System ID", "Detail link", "Team"])
    assert mapping.headers() == {
        "title": "Title",
        "description": "Description",
        "system_id": "System ID",
        "detail_url": "Detail link",
    }


def test_build_mapping_overrides_and_drops() -> None:
    mapping = build_mapping(
        ["title=Issue", "description=", "contact=Email"],
        ["Issue", "Description", "Email", "System ID", "Detail link"],
    )
    assert mapping.headers() == {
        "title": "Issue",
        "contact": "Email",
        "system_id": "System ID",
        "detail_url": "Detail link",
    }
    with pytest.raises(CliError, match="field=Header"):
        build_mapping(["colour=Red"], [])


def test_spreadsheet_id_from_url() -> None:
    url = "https://docs.google.com/spreadsheets/d/1AbC-d_9/edit#gid=0"
    assert spreadsheet_id_from(url) == "1AbC-d_9"
    assert spreadsheet_id_from(" 1AbC-d_9 ") == "1AbC-d_9"


# ---------- retry / schedule ----------


def test_backoff_doubles_caps_and_honours_retry_after() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3)] == [10, 20, 40]
    assert backoff_seconds(20) == 900
    assert backoff_seconds(1, retry_after=120) == 120
    assert backoff_seconds(1, retry_after=10_000) == 900


def test_retry_delay_gives_up() -> None:
    assert retry_delay(retryable=True, attempt=1, max_attempts=3) == 10
    assert retry_delay(retryable=True, attempt=3, max_attempts=3) is None
    assert retry_delay(retryable=False, attempt=1, max_attempts=3) is None


def test_poll_schedule() -> None:
    assert poll_schedule(15) == {"second": {0, 15, 30, 45}}
    assert poll_schedule(300) == {"minute": set(range(0, 60, 5)), "second": 0}


@pytest.mark.parametrize("seconds", [10, 30, 60, 120, 900, 3600])
def test_valid_poll_intervals(seconds: int) -> None:
    assert _valid_poll_interval(seconds) == seconds


@pytest.mark.parametrize("seconds", [45, 90, 420])
def test_invalid_poll_intervals(seconds: int) -> None:
    with pytest.raises(ValueError):
        _valid_poll_interval(seconds)
