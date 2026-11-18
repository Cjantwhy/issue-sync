"""Page helpers and page settings that need no database."""

import pytest
from pydantic import ValidationError

from issue_sync.web.routes import humanize, safe_url
from tests.settings_factory import make_settings


@pytest.mark.parametrize(
    ("value", "expected"),
    [("in_progress", "In progress"), ("access_request", "Access request"), (None, ""), ("", "")],
)
def test_humanize(value: str | None, expected: str) -> None:
    assert humanize(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://drive.google.com/file/d/x", "https://drive.google.com/file/d/x"),
        ("http://files.example.com/a.txt", "http://files.example.com/a.txt"),
        ("javascript:alert(1)", None),
        ("JaVaScRiPt:alert(1)", None),
        ("data:text/html,<script>", None),
        ("//evil.example.com", None),
        (None, None),
        (42, None),
    ],
)
def test_only_http_links_are_rendered(value: object, expected: str | None) -> None:
    assert safe_url(value) == expected


def test_dashboard_needs_both_credentials() -> None:
    assert not make_settings().dashboard_enabled
    assert not make_settings(dashboard_username="admin").dashboard_enabled
    assert make_settings(
        dashboard_username="admin", dashboard_password="long enough secret"
    ).dashboard_enabled


def test_short_dashboard_password_is_rejected() -> None:
    with pytest.raises(ValidationError, match="dashboard_password"):
        make_settings(dashboard_username="admin", dashboard_password="short")


def test_display_timezone_must_exist() -> None:
    assert make_settings(display_timezone="Asia/Shanghai").display_timezone == "Asia/Shanghai"
    with pytest.raises(ValidationError, match="unknown time zone"):
        make_settings(display_timezone="Mars/Olympus")
