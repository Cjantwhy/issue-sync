"""Server-rendered pages: the dashboard and the public detail page.

- `/dashboard` lists all records and needs a sign-in (HTTP Basic, see `auth.py`).
  Filters are a GET form; with HTMX only the results are swapped and the URL is pushed,
  so a filtered view can be bookmarked or shared. Without JavaScript the form still works.
- `/r/{system_id}` is read-only and public: the random UUID is the only key. It leaves
  out the reporter's contact details and asks search engines not to index it.
"""

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from issue_sync.adapter_config import JiraConfig
from issue_sync.web import queries
from issue_sync.web.auth import require_dashboard_user

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# Scripts and styles come from /static only; no inline code, no third-party origins.
CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
# Fields of a source_modified event that the public page may show (never `contact`).
PUBLIC_CHANGE_FIELDS = ("title", "description", "reporter")


def humanize(value: str | None) -> str:
    """Internal value as shown on the pages: in_progress -> "In progress"."""
    return value.replace("_", " ").capitalize() if value else ""


def safe_url(value: object) -> str | None:
    """Only http(s) links are rendered; anything else (e.g. javascript:) is dropped."""
    if not isinstance(value, str):
        return None
    return value if urlsplit(value).scheme in ("http", "https") else None


def _format_time(value: datetime | None, request: Request) -> str:
    if value is None:
        return ""
    zone = ZoneInfo(request.app.state.settings.display_timezone)
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


templates.env.filters["humanize"] = humanize
templates.env.filters["safe_url"] = safe_url


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.sessionmaker


def get_jira_config(request: Request) -> JiraConfig:
    return request.app.state.adapter_configs.jira


Sessionmaker = Annotated[async_sessionmaker[AsyncSession], Depends(get_sessionmaker)]
Jira = Annotated[JiraConfig, Depends(get_jira_config)]


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    public: bool = False,
) -> HTMLResponse:
    settings = request.app.state.settings
    response = templates.TemplateResponse(
        request,
        template,
        {
            "fmt_time": lambda value: _format_time(value, request),
            "timezone": settings.display_timezone,
            "jira_url": lambda key: f"{settings.jira_base_url}/browse/{key}",
            **context,
        },
        status_code=status_code,
    )
    headers = response.headers
    headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Referrer-Policy"] = "no-referrer"
    headers["Cache-Control"] = "no-store"
    if public:
        headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _blank_to_none(value: str | None) -> str | None:
    return (value.strip() or None) if value else None


@router.get(
    "/dashboard",
    response_class=HTMLResponse,
    dependencies=[Depends(require_dashboard_user)],
    include_in_schema=False,
)
async def dashboard(
    request: Request,
    sessionmaker: Sessionmaker,
    jira: Jira,
    status: str | None = None,
    type: str | None = None,
    urgency: str | None = None,
    document: str | None = None,
    comparison: str | None = None,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
) -> HTMLResponse:
    document_id = _blank_to_none(document)
    comparison = _blank_to_none(comparison)
    filters = queries.Filters(
        status=_blank_to_none(status),
        type=_blank_to_none(type),
        urgency=_blank_to_none(urgency),
        document_id=int(document_id) if document_id and document_id.isdigit() else None,
        comparison=comparison if comparison in queries.COMPARISON_FILTERS else None,
        page=page,
    )
    # htmx asks for the results only, except when restoring history after a cache miss.
    partial = (
        request.headers.get("HX-Request") == "true"
        and request.headers.get("HX-History-Restore-Request") != "true"
    )
    async with sessionmaker() as session:
        results = await queries.list_records(session, jira, filters)
        context: dict[str, Any] = {
            "filters": filters,
            "results": results,
            "page_url": lambda **kw: "/dashboard?" + urlencode(filters.query(**kw)),
        }
        if not partial:
            context["options"] = await queries.filter_options(session)
            context["stats"] = await queries.model_stats(session, jira)
            context["comparison_filters"] = queries.COMPARISON_FILTERS
    response = _render(request, "_records.html" if partial else "dashboard.html", context)
    response.headers["Vary"] = "HX-Request, HX-History-Restore-Request"
    return response


@router.get("/r/{system_id}", response_class=HTMLResponse, include_in_schema=False)
async def record_page(
    request: Request, system_id: str, sessionmaker: Sessionmaker, jira: Jira
) -> HTMLResponse:
    try:
        key = uuid.UUID(system_id)
    except ValueError:
        key = None
    detail = None
    if key is not None:
        async with sessionmaker() as session:
            detail = await queries.record_detail(session, jira, key)
    if detail is None:
        # Same answer for malformed and unknown ids: nothing to learn by probing.
        return _render(request, "not_found.html", {}, status_code=404, public=True)
    return _render(
        request,
        "detail.html",
        {"d": detail, "r": detail.row, "public_change_fields": PUBLIC_CHANGE_FIELDS},
        public=True,
    )
