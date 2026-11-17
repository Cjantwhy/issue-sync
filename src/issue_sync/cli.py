"""Operator commands. Usage: `issue-sync <command>` (or `python -m issue_sync.cli`)."""

import argparse
import asyncio
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from issue_sync.adapter_config import (
    AdapterConfigError,
    load_classifier_config,
    load_jira_config,
)
from issue_sync.classify.evaluation import Report, evaluate, load_cases
from issue_sync.classify.openai_compat import create_classifier
from issue_sync.db.models import Document, DocumentType
from issue_sync.db.session import create_engine, create_sessionmaker
from issue_sync.settings import DatabaseSettings, GoogleSettings, JiraSettings, LlmSettings
from issue_sync.sources.sheets.client import create_sheets_client
from issue_sync.sources.sheets.errors import SheetLayoutError, SheetsError
from issue_sync.sources.sheets.layout import REQUIRED_HEADERS, DocumentMapping, parse_sheet
from issue_sync.tickets.jira.backend import create_jira_client
from issue_sync.tickets.jira.check import CheckResult, JiraConfigChecker

DOCUMENT_TYPE = "google_sheets"
# Header names assumed when --map does not say otherwise.
DEFAULT_HEADERS = {
    "title": "Title",
    "description": "Description",
    "reporter": "Reporter",
    "contact": "Contact",
    "system_id": "System ID",
    "detail_url": "Detail link",
    "status": "Status",
    "latest_note": "Latest note",
}
_SPREADSHEET_URL = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


class CliError(Exception):
    """Reported to the operator with exit code 2."""


# ---------- jira-check ----------


async def _jira_check(settings: JiraSettings) -> list[CheckResult]:
    config = load_jira_config(settings.jira_config_path)
    async with create_jira_client(settings) as client:
        return await JiraConfigChecker(client, config).run()


def jira_check() -> int:
    try:
        settings = JiraSettings()  # type: ignore[call-arg]  # from the environment
        results = asyncio.run(_jira_check(settings))
    except (ValidationError, AdapterConfigError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    for result in results:
        print(f"[{'PASS' if result.ok else 'FAIL'}] {result.name}: {result.detail}")
    failed = sum(not r.ok for r in results)
    print(f"\n{'All checks passed.' if not failed else f'{failed} check(s) failed.'}")
    return 1 if failed else 0


# ---------- add-document ----------


class _DocumentSettings(DatabaseSettings, GoogleSettings):
    pass


def spreadsheet_id_from(value: str) -> str:
    """Accept a spreadsheet id or its full URL."""
    match = _SPREADSHEET_URL.search(value)
    return match.group(1) if match else value.strip()


def build_mapping(overrides: Sequence[str], sheet_headers: Sequence[str]) -> DocumentMapping:
    """Defaults plus `field=Header` overrides. `field=` drops an optional field.

    Optional fields whose default header is not in the sheet are left out, so a sheet
    without e.g. a "Contact" column needs no flags.
    """
    chosen: dict[str, str | None] = {}
    for item in overrides:
        name, sep, header = item.partition("=")
        name = name.strip()
        if not sep or name not in DEFAULT_HEADERS:
            raise CliError(f"--map expects field=Header with field in {', '.join(DEFAULT_HEADERS)}")
        chosen[name] = header.strip() or None
    present = {h.strip().casefold() for h in sheet_headers}
    mapping: dict[str, str] = {}
    for name, default in DEFAULT_HEADERS.items():
        if name in chosen:
            if chosen[name]:
                mapping[name] = chosen[name]  # type: ignore[assignment]
        elif name in REQUIRED_HEADERS or default.casefold() in present:
            mapping[name] = default
    try:
        return DocumentMapping.model_validate(mapping)
    except ValidationError as exc:
        raise CliError(f"invalid mapping: {exc}") from None


async def _add_document(
    settings: _DocumentSettings, name: str, spreadsheet: str, worksheet: str, maps: list[str]
) -> str:
    spreadsheet_id = spreadsheet_id_from(spreadsheet)
    async with httpx.AsyncClient() as http:
        try:
            sheets = create_sheets_client(settings, http)
        except ValueError as exc:
            raise CliError(str(exc)) from None
        values = await sheets.read_values(spreadsheet_id, worksheet)
    mapping = build_mapping(maps, values[0] if values else [])
    layout = parse_sheet(values, mapping)  # raises SheetLayoutError on a mismatch

    engine = create_engine(settings.database_url)
    try:
        async with create_sessionmaker(engine)() as session, session.begin():
            type_id = await session.scalar(
                select(DocumentType.id).where(DocumentType.name == DOCUMENT_TYPE)
            )
            if type_id is None:
                raise CliError(f"document type {DOCUMENT_TYPE!r} is missing; run the migrations")
            document = Document(
                document_type_id=type_id,
                name=name,
                spreadsheet_id=spreadsheet_id,
                worksheet_name=worksheet,
                mapping=mapping.headers(),
            )
            session.add(document)
            try:
                await session.flush()
            except IntegrityError:
                raise CliError(
                    f"worksheet {worksheet!r} of {spreadsheet_id} is already registered"
                ) from None
            document_id = document.id
    finally:
        await engine.dispose()

    lines = [f"Registered document {document_id}: {name}", "Mapping (field -> header):"]
    lines += [f"  {field:<12} -> {header}" for field, header in mapping.headers().items()]
    if layout.unmapped_headers:
        lines.append(f"Ignored columns: {', '.join(layout.unmapped_headers)}")
    lines.append(f"Data rows found: {len(layout.rows)}")
    return "\n".join(lines)


def add_document(args: argparse.Namespace) -> int:
    try:
        settings = _DocumentSettings()  # type: ignore[call-arg]  # from the environment
        print(
            asyncio.run(
                _add_document(settings, args.name, args.spreadsheet, args.worksheet, args.map)
            )
        )
    except ValidationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (CliError, SheetsError, SheetLayoutError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


# ---------- eval-classifier ----------


async def _eval_classifier(settings: LlmSettings, cases_path: Path) -> Report:
    config = load_classifier_config(settings.classifier_config_path)
    cases = load_cases(cases_path)
    async with httpx.AsyncClient() as http:
        return await evaluate(create_classifier(settings, config, http), cases)


def eval_classifier(args: argparse.Namespace) -> int:
    try:
        settings = LlmSettings()  # type: ignore[call-arg]  # from the environment
        report = asyncio.run(_eval_classifier(settings, Path(args.cases)))
    except (ValidationError, AdapterConfigError, ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(report.render())
    if args.min_accuracy is not None and report.all_correct < args.min_accuracy:
        print(f"\nBelow the required accuracy of {args.min_accuracy:.0%}.")
        return 1
    return 0


# ---------- entry point ----------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="issue-sync")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "jira-check", help="Verify credentials and config/jira.yaml against the live Jira site"
    )
    add = commands.add_parser(
        "add-document",
        help="Register a worksheet for syncing (checks access and headers first)",
    )
    add.add_argument("--name", required=True, help="Display name, e.g. 'Support requests'")
    add.add_argument("--spreadsheet", required=True, help="Spreadsheet URL or id")
    add.add_argument("--worksheet", required=True, help="Worksheet (tab) name")
    add.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="FIELD=HEADER",
        help="Override a header name; FIELD= drops an optional field. Repeatable. "
        f"Defaults: {', '.join(f'{k}={v}' for k, v in DEFAULT_HEADERS.items())}",
    )
    evaluation = commands.add_parser(
        "eval-classifier", help="Run the AI triage against labelled cases and report accuracy"
    )
    evaluation.add_argument("--cases", default="eval/triage_cases.yaml", help="Cases YAML file")
    evaluation.add_argument(
        "--min-accuracy",
        type=float,
        default=None,
        metavar="0.0-1.0",
        help="Exit with code 1 if fewer cases than this get all three fields right",
    )
    args = parser.parse_args(argv)
    if args.command == "eval-classifier":
        return eval_classifier(args)
    if args.command == "jira-check":
        return jira_check()
    if args.command == "add-document":
        return add_document(args)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
