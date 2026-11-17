from typing import Any

import httpx
import pytest
import respx

from issue_sync import cli
from issue_sync.adapter_config import JiraConfig
from issue_sync.tickets.jira.check import CheckResult, JiraConfigChecker
from issue_sync.tickets.jira.client import JiraClient
from tests.jira_fixtures import make_config

CREATEMETA = "/issue/createmeta/OPS/issuetypes"
PRIORITIES = [{"name": n} for n in ("Highest", "High", "Medium", "Low", "Lowest")]


def _field(field_id: str, *, required: bool = False, default: bool = False, **extra: Any) -> dict:
    return {
        "fieldId": field_id,
        "name": field_id.title(),
        "required": required,
        "hasDefaultValue": default,
        **extra,
    }


def _healthy_site() -> dict[str, Any]:
    """Responses of a Jira site that matches make_config() exactly."""
    return {
        "myself": {"accountId": "acc-bot", "displayName": "Sync Bot"},
        "issue_types": [{"id": "10001", "name": "Task"}, {"id": "10002", "name": "Bug"}],
        "fields": [
            _field("project", required=True),
            _field("issuetype", required=True),
            _field("summary", required=True),
            _field("description"),
            _field("labels"),
            _field("priority", default=True, allowedValues=PRIORITIES),
            _field("assignee"),
            _field("reporter", required=True, default=True),
        ],
        "statuses": [
            {
                "id": "10001",
                "name": "Task",
                "statuses": [{"name": n} for n in ("To Do", "In Progress", "Done")],
            },
            {"id": "10002", "name": "Bug", "statuses": [{"name": "Open"}]},
        ],
        "assignable": {"acc-backend", "acc-ops"},
    }


def _install(mock: respx.MockRouter, site: dict[str, Any]) -> None:
    mock.get("/myself").respond(200, json=site["myself"])
    mock.get(CREATEMETA).respond(
        200,
        json={"issueTypes": site["issue_types"], "startAt": 0, "total": len(site["issue_types"])},
    )
    mock.get(f"{CREATEMETA}/10001").respond(
        200, json={"fields": site["fields"], "startAt": 0, "total": len(site["fields"])}
    )
    mock.get("/priority/search").respond(200, json={"values": PRIORITIES, "isLast": True})
    mock.get("/project/OPS/statuses").respond(200, json=site["statuses"])

    def assignable(request: httpx.Request) -> httpx.Response:
        account_id = request.url.params["accountId"]
        users = [{"accountId": account_id}] if account_id in site["assignable"] else []
        return httpx.Response(200, json=users)

    mock.get("/user/assignable/search").mock(side_effect=assignable)


async def _run(client: JiraClient, config: JiraConfig | None = None) -> dict[str, CheckResult]:
    results = await JiraConfigChecker(client, config or make_config()).run()
    return {r.name: r for r in results}


def _failed(results: dict[str, CheckResult]) -> dict[str, str]:
    return {name: r.detail for name, r in results.items() if not r.ok}


async def test_healthy_site_passes_everything(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    _install(jira_mock, _healthy_site())
    results = await _run(jira_client)
    assert list(results) == [
        "credentials",
        "project and issue type",
        "create screen fields",
        "priorities",
        "workflow statuses",
        "assignees",
    ]
    assert _failed(results) == {}
    assert "project priority scheme" in results["priorities"].detail


async def test_wrong_service_account_aborts(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    site = _healthy_site()
    site["myself"] = {"accountId": "acc-human", "displayName": "Alice"}
    _install(jira_mock, site)

    results = await _run(jira_client)

    assert list(results) == ["credentials"]
    assert "loop protection" in results["credentials"].detail


async def test_rejected_token_aborts(jira_client: JiraClient, jira_mock: respx.MockRouter) -> None:
    jira_mock.get("/myself").respond(401, text="Unauthorized")
    results = await _run(jira_client)
    assert list(results) == ["credentials"]
    assert "401" in results["credentials"].detail


async def test_missing_issue_type_aborts(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    _install(jira_mock, _healthy_site())
    results = await _run(jira_client, make_config(issue_type="Incident"))
    assert list(results) == ["credentials", "project and issue type"]
    assert "available: Bug, Task" in results["project and issue type"].detail


async def test_unknown_project_aborts(jira_client: JiraClient, jira_mock: respx.MockRouter) -> None:
    _install(jira_mock, _healthy_site())
    jira_mock.get(CREATEMETA).respond(404, json={"errorMessages": ["No project"]})
    results = await _run(jira_client)
    assert "project OPS not found" in _failed(results)["project and issue type"]


async def test_field_problems_on_create_screen(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    site = _healthy_site()
    site["fields"] = [f for f in site["fields"] if f["fieldId"] != "labels"]
    site["fields"].append(_field("customfield_10050", required=True, name="Customer"))
    _install(jira_mock, site)

    detail = _failed(await _run(jira_client))["create screen fields"]

    assert "not on the create screen: labels" in detail
    assert "required fields the system does not fill: Customer" in detail


async def test_unknown_priority_status_and_assignee_are_reported(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    site = _healthy_site()
    site["assignable"] = {"acc-backend"}
    _install(jira_mock, site)
    config = make_config(
        priorities={"critical": "Blocker", "high": "High"},
        statuses={"open": "To Do", "in_progress": "Doing", "done": "Done"},
    )

    failed = _failed(await _run(jira_client, config))

    assert "critical -> 'Blocker'" in failed["priorities"]
    assert "in_progress -> 'Doing'" in failed["workflow statuses"]
    assert "ops (acc-ops)" in failed["assignees"]
    assert "backend" not in failed["assignees"]


async def test_statuses_match_localized_issue_type_by_id(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    # Seen on a zh-CN site: createmeta returns the localized issue type name,
    # /project/{key}/statuses returns the English one. Only the id is stable.
    site = _healthy_site()
    site["issue_types"] = [{"id": "10001", "name": "任务"}]
    _install(jira_mock, site)

    results = await _run(jira_client, make_config(issue_type="任务"))

    assert _failed(results) == {}
    assert "任务 workflow" in results["workflow statuses"].detail


async def test_priorities_fall_back_to_site_list(
    jira_client: JiraClient, jira_mock: respx.MockRouter
) -> None:
    site = _healthy_site()
    for field in site["fields"]:
        field.pop("allowedValues", None)
    _install(jira_mock, site)

    results = await _run(jira_client)

    assert results["priorities"].ok
    assert "site priorities" in results["priorities"].detail


def test_cli_exit_codes(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    async def fake(settings: object) -> list[CheckResult]:
        return [CheckResult("credentials", True, "ok"), CheckResult("assignees", False, "bad")]

    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    monkeypatch.setattr(cli, "_jira_check", fake)

    assert cli.main(["jira-check"]) == 1
    out = capsys.readouterr().out
    assert "[PASS] credentials: ok" in out
    assert "[FAIL] assignees: bad" in out
    assert "1 check(s) failed." in out


def test_cli_reports_missing_settings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, tmp_path: Any
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    assert cli.main(["jira-check"]) == 2
    assert "jira_api_token" in capsys.readouterr().err
