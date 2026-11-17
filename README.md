# issue-sync

Two-way sync between Google Sheets and Jira, with AI triage and assignment.

> Work in progress. Architecture, sync flow and the full setup guide come with the deployment milestone.

## Quick start (local)

Requires Docker with Compose v2.

```bash
cp .env.example .env
cp config/jira.example.yaml config/jira.yaml
cp config/slack.example.yaml config/slack.yaml
docker compose up -d --build --wait
curl localhost:8000/health
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}
```

`migrate` applies the database migrations and exits. `api` and `worker` start only after it succeeds.
Both refuse to start if an internal status, urgency or role in the database has no mapping
in `config/jira.yaml` or `config/slack.yaml`.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # creates .venv with Python 3.12
uv run ruff check . && uv run ruff format --check .
uv run pytest                # unit tests; external services are mocked

# Integration tests (need a PostgreSQL server; each run uses a throwaway database)
TEST_DATABASE_URL=postgresql+asyncpg://issue_sync:change-me@localhost:5432/postgres \
  uv run pytest -m integration
```

## Jira setup check

After filling in the `JIRA_*` variables in `.env` and `config/jira.yaml`, verify both
against your Jira site:

```bash
uv run issue-sync jira-check
```

It checks the credentials and service account, project and issue type, create-screen
fields, priorities, workflow statuses and assignees. It exits with a non-zero code if
any check fails.

See [docs/decisions.md](docs/decisions.md) for design decisions.
