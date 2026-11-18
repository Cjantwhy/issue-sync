"""Dashboard and detail pages against a real PostgreSQL (see tests/db_fixtures.py).

The AI-vs-ticket verdict is computed in SQL, so it is tested on a real database with
one record per case. Pages are requested through the ASGI app without its lifespan;
the session factory and the Jira config are injected.
"""

import base64
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from alembic import command
from sqlalchemy import pool, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from issue_sync.adapter_config import JiraConfig, load_jira_config
from issue_sync.api.app import create_app
from issue_sync.db.models import (
    AiJudgement,
    AiJudgementType,
    AiJudgementUrgency,
    AssigneeRole,
    Document,
    DocumentRecord,
    DocumentRecordHistory,
    DocumentRecordStatus,
    TicketSync,
)
from issue_sync.db.session import create_sessionmaker
from issue_sync.web import queries, routes
from tests.db_fixtures import REPO_ROOT, alembic_config, integration, throwaway_database
from tests.settings_factory import make_settings

pytestmark = integration

JIRA = load_jira_config(REPO_ROOT / "config" / "jira.example.yaml")
ACCOUNT = JIRA.assignees  # role -> account id
T0 = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
USER, PASSWORD = "admin", "correct horse battery"
CONTACT = "alice.private@example.com"
XSS_TITLE = "<script>alert(1)</script> Export"


# ---------- fixtures ----------


@pytest.fixture(scope="module")
def migrated_url() -> Iterator[str]:
    with throwaway_database() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture
async def sessionmaker(migrated_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(migrated_url, poolclass=pool.NullPool)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE document, document_record, document_record_history, "
                "ai_judgement, ticket_sync RESTART IDENTITY CASCADE"
            )
        )
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


class Seeder:
    """Builds one record per comparison case."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.ids: dict[str, dict[str, int]] = {}
        self.count = 0

    async def load(self) -> None:
        for name, model, column in (
            ("status", DocumentRecordStatus, DocumentRecordStatus.status_name),
            ("type", AiJudgementType, AiJudgementType.type_name),
            ("urgency", AiJudgementUrgency, AiJudgementUrgency.urgency_level_name),
            ("role", AssigneeRole, AssigneeRole.role_name),
        ):
            rows = await self.session.execute(select(column, model.id))
            self.ids[name] = {key: value for key, value in rows}
        type_id = await self.session.scalar(text("SELECT id FROM document_type LIMIT 1"))
        document = Document(
            document_type_id=type_id,
            name="Requests",
            spreadsheet_id="1AbC",
            worksheet_name="Requests",
            mapping={"title": "Title"},
        )
        self.session.add(document)
        await self.session.flush()
        self.document_id = document.id

    async def record(
        self,
        title: str,
        *,
        ai: tuple[str, str, str] | None = None,  # (type, urgency, role)
        model: str = "model-a",
        assigned: tuple[str | None, str | None, str | None] | None = None,
        status: str | None = "open",
        priority: str | None = None,
        assignee: tuple[str | None, str | None] = (None, None),  # (name, account id)
        ticket_read: bool = True,
        push_status: str = "success",
        **fields: Any,
    ) -> DocumentRecord:
        self.count += 1
        record = DocumentRecord(
            document_id=self.document_id,
            title=title,
            status_id=self.ids["status"][status] if status else None,
            current_priority=priority,
            current_assignee=assignee[0],
            current_assignee_id=assignee[1],
            create_time=T0 + timedelta(minutes=self.count),
            **fields,
        )
        self.session.add(record)
        await self.session.flush()
        if ai is not None:
            await self.judge(record, ai, model)
        urgency, role, rule = assigned or (None, None, None)
        self.session.add(
            TicketSync(
                document_record_id=record.id,
                issue_key=f"OPS-{self.count}" if push_status == "success" else None,
                push_status=push_status,
                ticket_updated_at=T0 if ticket_read else None,
                assigned_urgency_id=self.ids["urgency"][urgency] if urgency else None,
                assigned_role_id=self.ids["role"][role] if role else None,
                assignment_rule=rule,
            )
        )
        await self.session.flush()
        return record

    async def judge(
        self,
        record: DocumentRecord,
        ai: tuple[str, str, str],
        model: str,
        at: datetime | None = None,
        reason: str = "Because.",
    ) -> None:
        self.session.add(
            AiJudgement(
                document_record_id=record.id,
                type_id=self.ids["type"][ai[0]],
                urgency_id=self.ids["urgency"][ai[1]],
                assignee_role_id=self.ids["role"][ai[2]],
                summary=f"Summary of {record.title}",
                reason=reason,
                model=model,
                create_time=at or T0 + timedelta(hours=1),
            )
        )
        await self.session.flush()


@pytest.fixture
async def seeded(sessionmaker: async_sessionmaker[AsyncSession]) -> dict[str, DocumentRecord]:
    async with sessionmaker() as session, session.begin():
        s = Seeder(session)
        await s.load()
        agree = await s.record(
            "Agree",
            ai=("bug", "high", "backend"),
            assigned=("high", "backend", None),
            priority="High",
            assignee=("Ben Back", ACCOUNT["backend"]),
        )
        # An older judgement with other values: the newest one counts.
        await s.judge(agree, ("question", "critical", "ops"), "model-old", at=T0)
        rule = await s.record(
            "Rule",
            ai=("access_request", "medium", "backend"),
            assigned=("medium", "ops", "Access requests go to ops"),
            status="in_progress",
            priority="Medium",
            assignee=("Olga Ops", ACCOUNT["ops"]),
        )
        human = await s.record(
            "Human",
            ai=("question", "low", "support"),
            model="model-b",
            assigned=("low", "support", None),
            status="done",
            priority="Highest",
            assignee=("Fiona Front", ACCOUNT["frontend"]),
        )
        legacy = await s.record(
            "Legacy",
            ai=("bug", "high", "backend"),
            priority="High",
            assignee=("Ben Back", None),  # recorded before account ids were kept
        )
        unread = await s.record(
            "Not created yet",
            ai=("bug", "critical", "ops"),
            status=None,
            ticket_read=False,
            push_status="pending",
        )
        public = await s.record(
            XSS_TITLE,
            priority="Low",
            description="Line one\nLine two",
            reporter="Alice",
            contact=CONTACT,
            source_deleted_at=T0 + timedelta(days=1),
        )
        session.add_all(
            [
                DocumentRecordHistory(
                    document_record_id=public.id,
                    event_type="created",
                    created_by="sheet",
                    create_time=T0,
                ),
                DocumentRecordHistory(
                    document_record_id=public.id,
                    event_type="source_modified",
                    created_by="sheet",
                    detail={
                        "row": 2,
                        "changes": {
                            "title": {"from": "Export", "to": XSS_TITLE},
                            "contact": {"from": "old@example.com", "to": CONTACT},
                        },
                    },
                    create_time=T0 + timedelta(minutes=5),
                ),
                DocumentRecordHistory(
                    document_record_id=public.id,
                    event_type="status_changed",
                    from_status_id=s.ids["status"]["open"],
                    to_status_id=s.ids["status"]["in_progress"],
                    created_by="Olga Ops",
                    create_time=T0 + timedelta(minutes=10),
                ),
                DocumentRecordHistory(
                    document_record_id=public.id,
                    event_type="comment",
                    comment="Fixed in <b>1.4</b>",
                    attachments=[
                        {"name": "log.txt", "url": "https://files.example.com/log.txt"},
                        {"name": "evil", "url": "javascript:alert(1)"},
                    ],
                    created_by="Olga Ops",
                    create_time=T0 + timedelta(minutes=20),
                ),
                DocumentRecordHistory(
                    document_record_id=public.id,
                    event_type="source_deleted",
                    created_by="reconciliation",
                    create_time=T0 + timedelta(days=1),
                ),
            ]
        )
    return {
        "agree": agree,
        "rule": rule,
        "human": human,
        "legacy": legacy,
        "unread": unread,
        "public": public,
    }


def _app(sessionmaker: async_sessionmaker[AsyncSession], **settings: Any):  # type: ignore[no-untyped-def]
    app = create_app(make_settings(**settings))
    app.dependency_overrides[routes.get_sessionmaker] = lambda: sessionmaker
    app.dependency_overrides[routes.get_jira_config] = lambda: JIRA
    return app


@pytest.fixture
async def client(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = _app(sessionmaker, dashboard_username=USER, dashboard_password=PASSWORD)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _auth(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def _list(
    sessionmaker: async_sessionmaker[AsyncSession], jira: JiraConfig = JIRA, **filters: Any
) -> dict[str, queries.RecordRow]:
    async with sessionmaker() as session:
        page = await queries.list_records(session, jira, queries.Filters(**filters))
    return {r.title or "": r for r in page.rows}


# ---------- comparison ----------


async def test_verdict_per_case(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: dict[str, DocumentRecord]
) -> None:
    rows = await _list(sessionmaker)

    verdicts = {title: (r.priority_verdict, r.assignee_verdict) for title, r in rows.items()}
    assert verdicts == {
        "Agree": ("agree", "agree"),
        "Rule": ("agree", "rule"),
        "Human": ("human", "human"),
        "Legacy": ("agree", "unknown"),
        "Not created yet": ("none", "none"),
        XSS_TITLE: ("none", "none"),
    }
    agree = rows["Agree"]
    assert (agree.ai_urgency, agree.ai_priority, agree.ai_model) == ("high", "High", "model-a")
    rule = rows["Rule"]
    assert (rule.ai_role, rule.assigned_role, rule.assignment_rule) == (
        "backend",
        "ops",
        "Access requests go to ops",
    )


async def test_assignee_compared_by_account_id_not_name(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: dict[str, DocumentRecord]
) -> None:
    # Same display name, different account: it is a different person.
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text("UPDATE document_record SET current_assignee_id = 'acc-other' WHERE id = :id"),
            {"id": seeded["agree"].id},
        )
    assert (await _list(sessionmaker))["Agree"].assignee_verdict == "human"


async def test_unassigned_ticket_counts_as_a_human_change(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: dict[str, DocumentRecord]
) -> None:
    async with sessionmaker() as session, session.begin():
        await session.execute(
            text(
                "UPDATE document_record SET current_assignee = NULL, current_assignee_id = NULL "
                "WHERE id = :id"
            ),
            {"id": seeded["agree"].id},
        )
    assert (await _list(sessionmaker))["Agree"].assignee_verdict == "human"


@pytest.mark.parametrize(
    ("filters", "titles"),
    [
        ({"comparison": "differs"}, {"Rule", "Human"}),
        ({"comparison": "rule"}, {"Rule"}),
        ({"comparison": "human"}, {"Human"}),
        ({"comparison": "agree"}, {"Agree"}),
        ({"status": "done"}, {"Human"}),
        ({"type": "bug"}, {"Agree", "Legacy", "Not created yet"}),
        # The newest judgement counts: "Agree" was judged critical first, then high.
        ({"urgency": "critical"}, {"Not created yet"}),
        ({"type": "bug", "comparison": "differs"}, set()),
    ],
)
async def test_filters(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: dict[str, DocumentRecord],
    filters: dict[str, str],
    titles: set[str],
) -> None:
    assert set(await _list(sessionmaker, **filters)) == titles


async def test_model_stats(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: dict[str, DocumentRecord]
) -> None:
    async with sessionmaker() as session:
        stats = await queries.model_stats(session, JIRA)

    assert stats == [
        queries.ModelStats(
            model="model-a",
            compared=3,  # Agree, Rule, Legacy; "Not created yet" has nothing to compare
            priority={"agree": 3, "rule": 0, "human": 0},
            assignee={"agree": 1, "rule": 1, "human": 0},
        ),
        queries.ModelStats(
            model="model-b",
            compared=1,
            priority={"agree": 0, "rule": 0, "human": 1},
            assignee={"agree": 0, "rule": 0, "human": 1},
        ),
    ]


async def test_pagination(
    sessionmaker: async_sessionmaker[AsyncSession],
    seeded: dict[str, DocumentRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queries, "PAGE_SIZE", 4)
    async with sessionmaker() as session:
        first = await queries.list_records(session, JIRA, queries.Filters())
        second = await queries.list_records(session, JIRA, queries.Filters(page=2))
        beyond = await queries.list_records(session, JIRA, queries.Filters(page=9))

    assert (first.total, first.pages, len(first.rows), len(second.rows)) == (6, 2, 4, 2)
    assert first.rows[0].title == XSS_TITLE  # newest first
    assert beyond.page == 2  # clamped to the last page


# ---------- dashboard: access ----------


async def test_dashboard_is_off_without_credentials_configured(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    transport = httpx.ASGITransport(app=_app(sessionmaker))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        response = await c.get("/dashboard", headers=_auth())
    assert response.status_code == 404


@pytest.mark.parametrize(
    "headers",
    [{}, _auth(password="wrong"), _auth(user="other"), {"Authorization": "Bearer x"}],
)
async def test_dashboard_requires_sign_in(
    client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    response = await client.get("/dashboard", headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


# ---------- dashboard: rendering ----------


async def test_dashboard_full_page(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    response = await client.get("/dashboard", headers=_auth())

    assert response.status_code == 200
    html = response.text
    assert "<html" in html and 'name="comparison"' in html
    assert "model-a" in html and "6 records" in html
    assert f'href="/r/{seeded["agree"].system_id}"' in html
    assert "https://example.atlassian.net/browse/OPS-1" in html
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert CONTACT not in html
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["Cache-Control"] == "no-store"


async def test_htmx_request_gets_only_the_results(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    response = await client.get(
        "/dashboard", params={"comparison": "human"}, headers={**_auth(), "HX-Request": "true"}
    )

    assert response.status_code == 200
    assert "<html" not in response.text
    assert "1 record" in response.text and "Human" in response.text
    assert "HX-Request" in response.headers["Vary"]


async def test_history_restore_gets_the_full_page(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    headers = {**_auth(), "HX-Request": "true", "HX-History-Restore-Request": "true"}
    response = await client.get("/dashboard", headers=headers)
    assert "<html" in response.text


async def test_unknown_filter_values_are_harmless(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    response = await client.get(
        "/dashboard",
        params={"comparison": "nonsense", "document": "x' OR 1=1", "status": "nope"},
        headers=_auth(),
    )
    assert response.status_code == 200
    assert "0 records" in response.text


# ---------- detail page ----------


async def test_detail_page(client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]) -> None:
    response = await client.get(f"/r/{seeded['public'].system_id}")

    assert response.status_code == 200
    html = response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Export" in html
    assert "<script>alert(1)" not in html
    assert "Line one\nLine two" in html
    assert "Submitted 2026-11-17" in html and "by Alice" in html
    assert "Row deleted from the sheet" in html
    assert "Open → <strong>In progress</strong>" in html
    assert "Fixed in &lt;b&gt;1.4&lt;/b&gt;" in html
    assert 'href="https://files.example.com/log.txt"' in html
    assert "javascript:" not in html
    # Contact details are never shown, not even in the edit history.
    assert CONTACT not in html and "old@example.com" not in html
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert response.headers["Referrer-Policy"] == "no-referrer"


async def test_detail_page_shows_the_comparison(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    html = (await client.get(f"/r/{seeded['rule'].system_id}")).text

    assert "Summary of Rule" in html
    assert "Assignment rule applied at creation: Access requests go to ops" in html
    assert "Olga Ops" in html and 'class="verdict rule"' in html


async def test_detail_page_lists_earlier_judgements(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord]
) -> None:
    html = (await client.get(f"/r/{seeded['agree'].system_id}")).text
    assert "Earlier judgements (1)" in html and "model-old" in html


async def test_display_timezone(
    sessionmaker: async_sessionmaker[AsyncSession], seeded: dict[str, DocumentRecord]
) -> None:
    app = _app(sessionmaker, display_timezone="Asia/Shanghai")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        html = (await c.get(f"/r/{seeded['public'].system_id}")).text
    # Created 09:06 UTC, which is 17:06 in Shanghai.
    assert "Submitted 2026-11-17 17:06" in html and "Times in Asia/Shanghai" in html


@pytest.mark.parametrize("system_id", ["not-a-uuid", str(uuid.uuid4()), "1"])
async def test_unknown_or_malformed_id_is_the_same_404(
    client: httpx.AsyncClient, seeded: dict[str, DocumentRecord], system_id: str
) -> None:
    response = await client.get(f"/r/{system_id}")
    assert response.status_code == 404
    assert "Request not found" in response.text


async def test_static_files_are_served(client: httpx.AsyncClient) -> None:
    assert (await client.get("/static/htmx-2.0.4.min.js")).status_code == 200
    assert (await client.get("/static/app.css")).status_code == 200
