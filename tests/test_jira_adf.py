import pytest

from issue_sync.tickets.base import TicketContent
from issue_sync.tickets.jira import adf
from issue_sync.tickets.jira.backend import FOOTER, build_description, build_summary


def _texts(node: object) -> list[str]:
    """All text values in document order."""
    if isinstance(node, dict):
        own = [node["text"]] if node.get("type") == "text" else []
        return own + [t for child in node.get("content", []) for t in _texts(child)]
    return []


def test_paragraphs_and_hard_breaks() -> None:
    blocks = adf.plain_text_paragraphs("line one\r\nline two\n\n\n  \nsecond para\n")
    assert blocks == [
        adf.paragraph(adf.text("line one"), {"type": "hardBreak"}, adf.text("line two")),
        adf.paragraph(adf.text("second para")),
    ]


def test_blank_text_gives_no_blocks() -> None:
    assert adf.plain_text_paragraphs(" \n\n \t") == []


def test_control_characters_are_removed() -> None:
    assert adf.clean_text("a\x00b\x07c\td\ne") == "abc\td\ne"


def test_empty_text_node_is_rejected() -> None:
    with pytest.raises(ValueError):
        adf.text("")


def test_markup_in_user_text_is_not_interpreted() -> None:
    [block] = adf.plain_text_paragraphs("see [here](https://evil.example) *now*")
    [node] = block["content"]
    assert node == {"type": "text", "text": "see [here](https://evil.example) *now*"}


def _content(**overrides: object) -> TicketContent:
    data: dict[str, object] = {
        "title": "Export fails",
        "description": "Clicking export shows an error.\n\nSince Monday.",
        "reporter": "Alice",
        "contact": "alice@example.com",
        "detail_url": "https://sync.example.com/r/123",
    }
    data.update(overrides)
    return TicketContent.model_validate(data)


def test_description_layout() -> None:
    document = build_description(_content())
    assert document["type"] == "doc"
    assert document["version"] == 1
    assert [b["type"] for b in document["content"]] == [
        "paragraph", "paragraph", "rule", "bulletList", "paragraph",
    ]  # fmt: skip
    assert _texts(document) == [
        "Clicking export shows an error.",
        "Since Monday.",
        "Reporter: ", "Alice",
        "Contact: ", "alice@example.com",
        "Full history: ", "https://sync.example.com/r/123",
        FOOTER,
    ]  # fmt: skip
    link = document["content"][3]["content"][2]["content"][0]["content"][1]
    assert link["marks"] == [{"type": "link", "attrs": {"href": "https://sync.example.com/r/123"}}]


def test_description_without_optional_fields() -> None:
    document = build_description(_content(description="  ", reporter=None, contact=""))
    texts = _texts(document)
    assert texts[0] == "No description provided."
    assert not any(t.startswith(("Reporter", "Contact")) for t in texts)
    assert "Full history: " in texts


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Export\nfails  on\tMonday", "Export fails on Monday"),
        (None, "(untitled request)"),
        ("   \n ", "(untitled request)"),
    ],
)
def test_summary_is_single_line(title: str | None, expected: str) -> None:
    assert build_summary(title) == expected


def test_summary_is_truncated_to_jira_limit() -> None:
    summary = build_summary("x" * 300)
    assert len(summary) == 255
    assert summary.endswith("…")
