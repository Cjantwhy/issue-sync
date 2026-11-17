"""Minimal Atlassian Document Format (ADF) builder.

Jira Cloud REST API v3 takes rich-text fields (description, comments) as ADF JSON.
Only the handful of nodes this project needs are implemented. Input text is treated as
plain text: it is never parsed as Markdown or wiki markup, so user content cannot inject
formatting or links.
"""

import re
import unicodedata
from typing import Any

Node = dict[str, Any]

_ALLOWED_CONTROL = {"\n", "\t"}
# One or more blank (or whitespace-only) lines.
_PARAGRAPH_SPLIT = re.compile(r"\n(?:[ \t]*\n)+")


def clean_text(value: str) -> str:
    """Normalize newlines and drop control characters Jira may reject."""
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "".join(ch for ch in value if ch in _ALLOWED_CONTROL or unicodedata.category(ch) != "Cc")


def text(value: str, *, strong: bool = False, em: bool = False, href: str | None = None) -> Node:
    if not value:
        raise ValueError("ADF text nodes must not be empty")
    marks: list[Node] = []
    if strong:
        marks.append({"type": "strong"})
    if em:
        marks.append({"type": "em"})
    if href:
        marks.append({"type": "link", "attrs": {"href": href}})
    node: Node = {"type": "text", "text": value}
    if marks:
        node["marks"] = marks
    return node


def paragraph(*inline: Node) -> Node:
    return {"type": "paragraph", "content": list(inline)}


def bullet_list(*items: list[Node]) -> Node:
    """Each item is the inline content of one list entry."""
    return {
        "type": "bulletList",
        "content": [{"type": "listItem", "content": [paragraph(*item)]} for item in items],
    }


def rule() -> Node:
    return {"type": "rule"}


def doc(*blocks: Node) -> Node:
    return {"type": "doc", "version": 1, "content": list(blocks)}


def plain_text_paragraphs(value: str) -> list[Node]:
    """Blank lines separate paragraphs; single newlines become hard breaks."""
    blocks: list[Node] = []
    for chunk in _PARAGRAPH_SPLIT.split(clean_text(value).strip()):
        lines = [line.rstrip() for line in chunk.split("\n")]
        inline: list[Node] = []
        for i, line in enumerate(lines):
            if i:
                inline.append({"type": "hardBreak"})
            if line:
                inline.append(text(line))
        if any(node["type"] == "text" for node in inline):
            blocks.append(paragraph(*inline))
    return blocks
