"""`issue-sync jira-check`: verify jira.yaml and the credentials against a live Jira site.

Run manually at deployment time and after changing Jira or jira.yaml. Deliberately not
part of process startup: a temporary Jira outage must not stop the api from booting.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from issue_sync.adapter_config import JiraConfig
from issue_sync.tickets.base import TicketAuthError, TicketBackendError, TicketNotFound
from issue_sync.tickets.jira.client import JiraClient

# Fields the backend sets when creating an issue (reporter defaults to the API account).
FIELDS_WE_SET = {
    "project", "issuetype", "summary", "description", "labels", "priority", "assignee", "reporter",
}  # fmt: skip


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class _CheckFailed(Exception):
    """This check failed; continue with the next one."""


class _Abort(_CheckFailed):
    """A prerequisite failed; later checks would only repeat the same error."""


def _require(condition: object, message: str) -> None:
    # Not `assert`: assertions are stripped when Python runs with -O.
    if not condition:
        raise _CheckFailed(message)


class JiraConfigChecker:
    def __init__(self, client: JiraClient, config: JiraConfig) -> None:
        self._client = client
        self._config = config
        self._results: list[CheckResult] = []
        self._create_fields: dict[str, dict[str, Any]] = {}
        self._issue_type_id: str | None = None

    async def run(self) -> list[CheckResult]:
        steps: list[tuple[str, Callable[[], Awaitable[str]]]] = [
            ("credentials", self._check_credentials),
            ("project and issue type", self._check_issue_type),
            ("create screen fields", self._check_create_fields),
            ("priorities", self._check_priorities),
            ("workflow statuses", self._check_statuses),
            ("assignees", self._check_assignees),
        ]
        for name, step in steps:
            try:
                self._pass(name, await step())
            except _Abort as exc:
                self._fail(name, str(exc))
                break
            except _CheckFailed as exc:
                self._fail(name, str(exc))
            except TicketBackendError as exc:
                self._fail(name, str(exc))
                if isinstance(exc, TicketAuthError):
                    break
        return self._results

    def _pass(self, name: str, detail: str) -> None:
        self._results.append(CheckResult(name, True, detail))

    def _fail(self, name: str, detail: str) -> None:
        self._results.append(CheckResult(name, False, detail))

    async def _check_credentials(self) -> str:
        me = await self._client.request("GET", "/myself")
        account_id, name = me["accountId"], me.get("displayName", "?")
        if account_id != self._config.service_account_id:
            raise _Abort(
                f"the credentials belong to {name} ({account_id}), but jira.yaml "
                f"service_account_id is {self._config.service_account_id}; webhook loop "
                "protection would not recognise the system's own changes"
            )
        return f"authenticated as {name} ({account_id}), matches service_account_id"

    async def _check_issue_type(self) -> str:
        project = self._config.project_key
        try:
            issue_types = [
                it
                async for it in self._client.paginate(
                    f"/issue/createmeta/{project}/issuetypes", items_key="issueTypes"
                )
            ]
        except TicketNotFound:
            raise _Abort(
                f"project {project} not found, or the account cannot create issues in it"
            ) from None
        wanted = self._config.issue_type.casefold()
        match = next((it for it in issue_types if it["name"].casefold() == wanted), None)
        if match is None:
            names = ", ".join(sorted(it["name"] for it in issue_types)) or "none"
            raise _Abort(
                f"issue type {self._config.issue_type!r} not available in {project} "
                f"(available: {names})"
            )
        self._issue_type_id = match["id"]
        fields = self._client.paginate(
            f"/issue/createmeta/{project}/issuetypes/{match['id']}", items_key="fields"
        )
        self._create_fields = {f["fieldId"]: f async for f in fields}
        return f"{project} / {match['name']}"

    async def _check_create_fields(self) -> str:
        needed = {"summary", "description", "labels"}
        if self._config.priorities:
            needed.add("priority")
        if self._config.assignees:
            needed.add("assignee")
        missing = sorted(needed - self._create_fields.keys())
        blocking = sorted(
            f.get("name") or field_id
            for field_id, f in self._create_fields.items()
            if f.get("required") and not f.get("hasDefaultValue") and field_id not in FIELDS_WE_SET
        )
        problems = []
        if missing:
            problems.append(f"not on the create screen: {', '.join(missing)}")
        if blocking:
            problems.append(f"required fields the system does not fill: {', '.join(blocking)}")
        _require(not problems, "; ".join(problems))
        return f"all of {', '.join(sorted(needed))} can be set"

    async def _check_priorities(self) -> str:
        allowed_values = (self._create_fields.get("priority") or {}).get("allowedValues")
        if allowed_values is not None:
            available = {p["name"] for p in allowed_values}
            source = "project priority scheme"
        else:
            available = {
                p["name"]
                async for p in self._client.paginate("/priority/search", items_key="values")
            }
            source = "site priorities"
        _require_all_present("priorities", self._config.priorities, available)
        return f"{len(self._config.priorities)} mapped, all in the {source}"

    async def _check_statuses(self) -> str:
        project = self._config.project_key
        per_type = await self._client.request("GET", f"/project/{project}/statuses")
        # Match by id: issue type names are localized differently per endpoint
        # (createmeta returns the user's language, this endpoint may not).
        entry = next((t for t in per_type if t.get("id") == self._issue_type_id), None)
        if entry is None:
            raise _CheckFailed(f"no workflow statuses returned for {self._config.issue_type}")
        available = {s["name"] for s in entry.get("statuses") or []}
        _require_all_present("statuses", self._config.statuses, available)
        mapped = {name.casefold() for name in self._config.statuses.values()}
        unmapped = sorted(n for n in available if n.casefold() not in mapped)
        note = (
            f"; not mapped (tickets moved there keep their last status): {', '.join(unmapped)}"
            if unmapped
            else ""
        )
        return (
            f"{len(self._config.statuses)} mapped, all in the {self._config.issue_type} "
            f"workflow{note}"
        )

    async def _check_assignees(self) -> str:
        not_assignable = []
        for role, account_id in sorted(self._config.assignees.items()):
            users = await self._client.request(
                "GET",
                "/user/assignable/search",
                params={"project": self._config.project_key, "accountId": account_id},
            )
            if not users:
                not_assignable.append(f"{role} ({account_id})")
        _require(
            not not_assignable,
            f"cannot be assigned issues in {self._config.project_key}: "
            + ", ".join(not_assignable),
        )
        return f"{len(self._config.assignees)} mapped, all assignable"


def _require_all_present(what: str, mapping: dict[str, str], available: set[str]) -> None:
    lookup = {name.casefold() for name in available}
    missing = [f"{key} -> {value!r}" for key, value in sorted(mapping.items())
               if value.casefold() not in lookup]  # fmt: skip
    _require(
        not missing,
        f"{what} not found in Jira: {', '.join(missing)} "
        f"(available: {', '.join(sorted(available))})",
    )
