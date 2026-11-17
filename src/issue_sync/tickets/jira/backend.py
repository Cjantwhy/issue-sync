"""Jira Cloud ticket backend.

Mapping of internal values to Jira comes from `config/jira.yaml` (see `JiraConfig`).
Field ownership: summary and description are owned by the spreadsheet and overwritten on
every update; priority and assignee are set once at creation and owned by Jira after that.
"""

import asyncio
import re
import time
import uuid
from datetime import datetime
from typing import Any

import httpx

from issue_sync.adapter_config import JiraConfig
from issue_sync.settings import JiraSettings
from issue_sync.tickets.base import (
    DuplicateTickets,
    TicketContent,
    TicketDraft,
    TicketEvent,
    TicketRef,
    TicketSnapshot,
    TransitionNotAvailable,
    TriageNote,
)
from issue_sync.tickets.jira import adf
from issue_sync.tickets.jira.auth import OAuthClientCredentials, gateway_url
from issue_sync.tickets.jira.client import JiraClient
from issue_sync.tickets.jira.webhook import parse_jira_webhook

# Every ticket is labelled with its record's system_id so a create that timed out (but
# succeeded in Jira) can be found again instead of creating a duplicate.
SYSTEM_ID_LABEL_PREFIX = "issue-sync-"
SUMMARY_MAX_LENGTH = 255  # Jira's hard limit
UNTITLED_SUMMARY = "(untitled request)"
SNAPSHOT_FIELDS = "status,priority,assignee,updated"
# An unknown status id reloads the project's statuses at most this often.
STATUS_RELOAD_SECONDS = 60.0
FOOTER = (
    "Synced from Google Sheets by issue-sync. The title and description are owned by "
    "the sheet; edits made here will be overwritten."
)

_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")
_WHITESPACE = re.compile(r"\s+")


def system_id_label(system_id: uuid.UUID) -> str:
    return f"{SYSTEM_ID_LABEL_PREFIX}{system_id}"


def build_summary(title: str | None) -> str:
    """Jira summaries are single-line and at most 255 characters."""
    summary = _WHITESPACE.sub(" ", adf.clean_text(title or "")).strip()
    if not summary:
        return UNTITLED_SUMMARY
    if len(summary) > SUMMARY_MAX_LENGTH:
        summary = summary[: SUMMARY_MAX_LENGTH - 1].rstrip() + "…"
    return summary


def build_description(content: TicketContent) -> adf.Node:
    body = adf.plain_text_paragraphs(content.description or "")
    if not body:
        body = [adf.paragraph(adf.text("No description provided.", em=True))]

    details: list[list[adf.Node]] = []
    for label, value in (("Reporter", content.reporter), ("Contact", content.contact)):
        value = adf.clean_text(value or "").strip()
        if value:
            details.append([adf.text(f"{label}: ", strong=True), adf.text(value)])
    details.append(
        [
            adf.text("Full history: ", strong=True),
            adf.text(content.detail_url, href=content.detail_url),
        ]
    )

    return adf.doc(
        *body,
        adf.rule(),
        *build_triage_section(content.triage),
        adf.bullet_list(*details),
        adf.paragraph(adf.text(FOOTER, em=True)),
    )


def build_triage_section(triage: TriageNote | None) -> list[adf.Node]:
    if triage is None:
        return [
            adf.paragraph(
                adf.text("AI triage: ", strong=True),
                adf.text("not available. Set the priority and assignee manually.", em=True),
            )
        ]
    rows = [
        ("Category", triage.type),
        ("Urgency", triage.urgency),
        ("Suggested team", triage.assignee_role),
        ("Summary", triage.summary),
        ("Reason", triage.reason),
    ]
    return [
        adf.paragraph(adf.text("AI triage", strong=True), adf.text(f" ({triage.model})", em=True)),
        adf.bullet_list(
            *(
                [adf.text(f"{label}: ", strong=True), adf.text(clean)]
                for label, value in rows
                if (clean := _single_line(value))
            )
        ),
    ]


def _single_line(value: str) -> str:
    return _WHITESPACE.sub(" ", adf.clean_text(value)).strip()


def _issue_path(key: str, suffix: str = "") -> str:
    # Keys end up in URL paths; reject anything that is not a plain issue key.
    if not _ISSUE_KEY.fullmatch(key):
        raise ValueError(f"Not a Jira issue key: {key!r}")
    return f"/issue/{key}{suffix}"


def _lookup(mapping: dict[str, str], value: str, what: str) -> str:
    try:
        return mapping[value]
    except KeyError:
        # Startup checks make this unreachable for values that exist in the database.
        raise ValueError(f"jira.yaml has no {what} mapping for {value!r}") from None


class JiraBackend:
    def __init__(self, client: JiraClient, config: JiraConfig) -> None:
        self._client = client
        self._config = config
        self._status_names: dict[str, str] = {}  # status id -> name (service account view)
        self._statuses_loaded_at = float("-inf")
        self._status_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def parse_webhook(self, payload: dict[str, Any]) -> TicketEvent | None:
        return await parse_jira_webhook(payload, self._config, self._resolve_status)

    async def _resolve_status(self, status: dict[str, Any]) -> tuple[str | None, str]:
        """Map a webhook status by id, not by name.

        Names are localized: a webhook may carry the site language's name while
        jira.yaml uses the names the service account sees. Ids are stable, so the
        project's statuses are loaded once (as the service account) and looked up by id.
        """
        status_id = str(status.get("id") or "")
        if status_id and status_id not in self._status_names:
            # Concurrent events wait for one load instead of falling back to the
            # payload's (possibly localized) name while it is in flight.
            async with self._status_lock:
                now = time.monotonic()
                if (
                    status_id not in self._status_names
                    and now - self._statuses_loaded_at > STATUS_RELOAD_SECONDS
                ):
                    per_type = await self._client.request(
                        "GET", f"/project/{self._config.project_key}/statuses"
                    )
                    for issue_type in per_type or []:
                        for item in issue_type.get("statuses") or []:
                            self._status_names[str(item["id"])] = item["name"]
                    self._statuses_loaded_at = now
        name = self._status_names.get(status_id) or str(status.get("name") or "")
        return self._config.internal_status_for(name), name

    def ticket_url(self, key: str) -> str:
        _issue_path(key)  # validates the key
        return self._client.browse_url(key)

    def _ref(self, key: str) -> TicketRef:
        return TicketRef(key=key, url=self.ticket_url(key))

    async def create_ticket(self, draft: TicketDraft) -> TicketRef:
        config = self._config
        fields: dict[str, Any] = {
            "project": {"key": config.project_key},
            "issuetype": {"name": config.issue_type},
            "summary": build_summary(draft.title),
            "description": build_description(draft),
            "labels": [system_id_label(draft.system_id)],
        }
        if draft.urgency is not None:
            priority = _lookup(config.priorities, draft.urgency, "priority")
            fields["priority"] = {"name": priority}
        if draft.assignee_role is not None:
            account_id = _lookup(config.assignees, draft.assignee_role, "assignee")
            fields["assignee"] = {"accountId": account_id}

        created = await self._client.request("POST", "/issue", json={"fields": fields})
        return self._ref(created["key"])

    async def find_by_system_id(self, system_id: uuid.UUID) -> TicketRef | None:
        # Both values are validated (project key pattern, UUID), so plain quoting is safe.
        jql = f'project = "{self._config.project_key}" AND labels = "{system_id_label(system_id)}"'
        result = await self._client.request(
            "POST", "/search/jql", json={"jql": jql, "fields": ["summary"], "maxResults": 2}
        )
        keys = [issue["key"] for issue in result.get("issues") or []]
        if len(keys) > 1:
            raise DuplicateTickets(
                f"Several Jira issues carry label {system_id_label(system_id)}: {', '.join(keys)}"
            )
        return self._ref(keys[0]) if keys else None

    async def update_content(self, key: str, content: TicketContent) -> None:
        fields = {
            "summary": build_summary(content.title),
            "description": build_description(content),
        }
        await self._client.request("PUT", _issue_path(key), json={"fields": fields})

    async def transition(self, key: str, status: str) -> bool:
        target = _lookup(self._config.statuses, status, "status")
        issue = await self._client.request("GET", _issue_path(key), params={"fields": "status"})
        current = issue["fields"]["status"]["name"]
        if current.casefold() == target.casefold():
            return False

        available = await self._client.request("GET", _issue_path(key, "/transitions"))
        transitions = available.get("transitions") or []
        match = next(
            (t for t in transitions if t["to"]["name"].casefold() == target.casefold()), None
        )
        if match is None:
            options = ", ".join(sorted({t["to"]["name"] for t in transitions})) or "none"
            raise TransitionNotAvailable(
                f"{key}: no transition from {current!r} to {target!r} "
                f"(reachable statuses: {options})"
            )
        await self._client.request(
            "POST", _issue_path(key, "/transitions"), json={"transition": {"id": match["id"]}}
        )
        return True

    async def add_comment(self, key: str, text: str) -> None:
        body = adf.plain_text_paragraphs(text)
        if not body:
            raise ValueError("Comment text is empty")
        await self._client.request(
            "POST", _issue_path(key, "/comment"), json={"body": adf.doc(*body)}
        )

    async def get_ticket(self, key: str) -> TicketSnapshot:
        issue = await self._client.request(
            "GET", _issue_path(key), params={"fields": SNAPSHOT_FIELDS}
        )
        fields = issue["fields"]
        external_status = fields["status"]["name"]
        priority = fields.get("priority") or {}
        assignee = fields.get("assignee") or {}
        return TicketSnapshot(
            key=issue["key"],
            url=self._client.browse_url(issue["key"]),
            external_status=external_status,
            status=self._config.internal_status_for(external_status),
            priority=priority.get("name"),
            assignee_id=assignee.get("accountId"),
            assignee_name=assignee.get("displayName"),
            updated_at=datetime.fromisoformat(fields["updated"]),
        )


def create_jira_client(settings: JiraSettings) -> JiraClient:
    site = str(settings.jira_base_url)
    if settings.jira_auth == "oauth":
        assert settings.jira_client_id and settings.jira_client_secret and settings.jira_cloud_id
        return JiraClient(
            site,
            OAuthClientCredentials(
                settings.jira_client_id, settings.jira_client_secret.get_secret_value()
            ),
            api_base_url=gateway_url(settings.jira_cloud_id),
        )
    assert settings.jira_email and settings.jira_api_token
    return JiraClient(
        site, httpx.BasicAuth(settings.jira_email, settings.jira_api_token.get_secret_value())
    )


def create_jira_backend(settings: JiraSettings, config: JiraConfig) -> JiraBackend:
    return JiraBackend(create_jira_client(settings), config)
