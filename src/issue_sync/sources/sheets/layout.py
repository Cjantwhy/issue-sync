"""Header mapping and row parsing. Pure functions, no IO.

Columns are found by header name, never by position, so people can reorder or add
columns freely. `document.mapping` maps each predefined field to a header name.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from issue_sync.sources.sheets.client import column_letter
from issue_sync.sources.sheets.errors import SheetLayoutError

# Owned by the sheet: synced sheet -> database -> ticket.
SHEET_FIELDS = ("title", "description", "reporter", "contact")
# Owned by this system: written to the sheet, never read back as content.
SYSTEM_FIELDS = ("system_id", "detail_url", "status", "latest_note")


class DocumentMapping(BaseModel):
    """Predefined field -> header name. Stored as `document.mapping`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    description: str | None = None
    reporter: str | None = None
    contact: str | None = None
    system_id: str
    detail_url: str
    # Written back from the ticket system in milestone 6.
    status: str | None = None
    latest_note: str | None = None

    @field_validator("*")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("header name must not be blank")
        return value

    @model_validator(mode="after")
    def _unique_headers(self) -> "DocumentMapping":
        seen: dict[str, str] = {}
        for name, header in self.headers().items():
            other = seen.setdefault(header.casefold(), name)
            if other != name:
                raise ValueError(f"{other} and {name} both map to header {header!r}")
        return self

    def headers(self) -> dict[str, str]:
        return {name: header for name, header in self.model_dump().items() if header}


@dataclass(frozen=True)
class SheetRow:
    row_number: int  # 1-based, as shown in the sheet
    fields: dict[str, str | None]  # sheet-owned fields, None when blank
    system_id_cell: str
    detail_url_cell: str
    raw: dict[str, str]  # the whole row, keyed by header
    raw_hash: str  # SHA-256 of the row without system-owned columns
    has_content: bool  # any non-empty cell outside the system-owned columns

    def parsed_system_id(self) -> uuid.UUID | None:
        """The row's identity. Raises ValueError for a non-empty cell that is not a UUID."""
        if not self.system_id_cell:
            return None
        return uuid.UUID(self.system_id_cell)


@dataclass(frozen=True)
class SheetLayout:
    columns: dict[str, int]  # mapped field -> 0-based column index
    unmapped_headers: list[str]
    missing_optional: list[str]  # mapped fields whose header is not in the sheet
    rows: list[SheetRow]


REQUIRED_HEADERS = ("title", "system_id", "detail_url")


def parse_sheet(values: list[list[str]], mapping: DocumentMapping) -> SheetLayout:
    if not values:
        raise SheetLayoutError("the worksheet is empty (no header row)")
    header = [cell.strip() for cell in values[0]]
    by_name: dict[str, list[int]] = {}
    for index, name in enumerate(header):
        if name:
            by_name.setdefault(name.casefold(), []).append(index)

    columns: dict[str, int] = {}
    missing_required, missing_optional, ambiguous = [], [], []
    for fld, wanted in mapping.headers().items():
        found = by_name.get(wanted.casefold(), [])
        if len(found) > 1:
            ambiguous.append(wanted)
        elif found:
            columns[fld] = found[0]
        elif fld in REQUIRED_HEADERS:
            missing_required.append(wanted)
        else:
            missing_optional.append(fld)
    problems = []
    if missing_required:
        problems.append(f"missing header(s): {', '.join(missing_required)}")
    if ambiguous:
        problems.append(f"header(s) appear more than once: {', '.join(ambiguous)}")
    if problems:
        raise SheetLayoutError("; ".join(problems))

    mapped_indexes = set(columns.values())
    unmapped = [name for i, name in enumerate(header) if name and i not in mapped_indexes]
    raw_keys = _raw_keys(header, max((len(r) for r in values), default=0))
    system_keys = frozenset(raw_keys[columns[f]] for f in SYSTEM_FIELDS if f in columns)

    rows = []
    for offset, cells in enumerate(values[1:]):
        padded = [c.strip() for c in cells] + [""] * (len(raw_keys) - len(cells))
        if not any(padded):
            continue
        raw = dict(zip(raw_keys, padded, strict=True))
        content = {k: v for k, v in raw.items() if k not in system_keys}
        rows.append(
            SheetRow(
                row_number=offset + 2,
                fields={
                    f: (padded[columns[f]] or None) if f in columns else None for f in SHEET_FIELDS
                },
                system_id_cell=padded[columns["system_id"]],
                detail_url_cell=padded[columns["detail_url"]],
                raw=raw,
                raw_hash=content_hash(content),
                has_content=any(content.values()),
            )
        )
    return SheetLayout(columns, unmapped, missing_optional, rows)


def content_hash(data: Any) -> str:
    canonical = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _raw_keys(header: list[str], width: int) -> list[str]:
    """Header text per column; the column letter for blank or repeated headers."""
    counts: dict[str, int] = {}
    for name in header:
        counts[name] = counts.get(name, 0) + 1
    keys = []
    for index in range(max(width, len(header))):
        name = header[index] if index < len(header) else ""
        letter = column_letter(index)
        if not name:
            keys.append(letter)
        elif counts[name] > 1:
            keys.append(f"{name} ({letter})")
        else:
            keys.append(name)
    return keys
