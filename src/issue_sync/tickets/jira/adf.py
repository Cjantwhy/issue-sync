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


_BLOCK_TYPES = {"paragraph", "heading", "blockquote", "codeBlock", "rule", "panel"}


def to_plain_text(node: object) -> str:
    """Readable plain text from an ADF document (comments written in Jira).

    Formatting is dropped; mentions keep their display text, links their URL, and
    attachments become "[attachment]". Blocks are separated by newlines.
    """
    parts: list[str] = []

    def walk(n: object) -> None:
        if isinstance(n, list):
            for child in n:
                walk(child)
            return
        if not isinstance(n, dict):
            return
        kind = n.get("type")
        attrs = n.get("attrs") or {}
        if kind == "text":
            parts.append(str(n.get("text", "")))
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "mention":
            parts.append(str(attrs.get("text") or "@user"))
        elif kind == "emoji":
            parts.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind in ("inlineCard", "blockCard"):
            parts.append(str(attrs.get("url") or ""))
        elif kind in ("media", "mediaInline"):
            parts.append("[attachment]")
        if kind == "listItem":
            parts.append("- ")
        walk(n.get("content") or [])
        if kind in _BLOCK_TYPES:
            parts.append("\n")

    walk(node)
    lines = [line.rstrip() for line in "".join(parts).split("\n")]
    return "\n".join(lines).strip()
