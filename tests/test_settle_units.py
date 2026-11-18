"""New-row policy and its settings: no database, no Redis."""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from issue_sync.settings import SHEET_FIELD_NAMES
from issue_sync.sources.sheets.layout import SHEET_FIELDS, DocumentMapping, parse_sheet
from issue_sync.sync.settle import NewRowPolicy, waiting_note
from tests.settings_factory import make_settings


def test_settings_field_names_match_the_sheet_fields() -> None:
    assert SHEET_FIELD_NAMES == SHEET_FIELDS


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("title,description", ("title", "description")),
        (" title , reporter ,title", ("title", "reporter")),
        ("", ()),
    ],
)
def test_required_fields_setting(value: str, expected: tuple[str, ...]) -> None:
    assert make_settings(sheet_required_fields=value).required_fields == expected


def test_required_fields_defaults_and_rejects_unknown_names() -> None:
    settings = make_settings()
    assert settings.required_fields == ("title", "description")
    assert settings.new_row_settle_seconds == 120
    with pytest.raises(ValidationError, match="unknown field"):
        make_settings(sheet_required_fields="title,system_id")


def test_settle_period_needs_a_clock() -> None:
    with pytest.raises(ValueError, match="RowClock"):
        NewRowPolicy(settle=timedelta(seconds=1))


def test_missing_only_counts_fields_the_sheet_has() -> None:
    mapping = DocumentMapping(title="Title", reporter="Reporter", system_id="ID", detail_url="Link")
    layout = parse_sheet([["Title", "Reporter", "ID", "Link"], ["", "Ann"]], mapping)
    policy = NewRowPolicy(required_fields=("title", "description", "reporter"))
    assert policy.missing(layout.rows[0], layout.columns) == ["title"]


def test_waiting_note() -> None:
    assert waiting_note(["title", "description"]) == "Waiting for: Title, Description"
