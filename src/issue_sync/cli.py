"""Operator commands. Usage: `issue-sync <command>` (or `python -m issue_sync.cli`)."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from issue_sync.adapter_config import AdapterConfigError, load_jira_config
from issue_sync.settings import JiraSettings
from issue_sync.tickets.jira.backend import create_jira_client
from issue_sync.tickets.jira.check import CheckResult, JiraConfigChecker


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="issue-sync")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "jira-check", help="Verify credentials and config/jira.yaml against the live Jira site"
    )
    args = parser.parse_args(argv)
    if args.command == "jira-check":
        return jira_check()
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
