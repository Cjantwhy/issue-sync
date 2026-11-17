# issue-sync

Two-way sync between Google Sheets and Jira, with AI triage and assignment.

> Work in progress. Architecture, sync flow and the full setup guide come with the deployment milestone.

## Quick start (local)

Requires Docker with Compose v2.

```bash
cp .env.example .env
cp config/jira.example.yaml config/jira.yaml
cp config/slack.example.yaml config/slack.yaml
cp config/classifier.example.yaml config/classifier.yaml
cp config/assignment.example.yaml config/assignment.yaml
# plus the Google service account key in secrets/ (see "Google Sheets setup")
docker compose up -d --build --wait
curl localhost:8000/health
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}
```

`migrate` applies the database migrations and exits. `api` and `worker` start only after it succeeds.
Both refuse to start if an internal status, category, urgency or role in the database has
no entry in `config/jira.yaml`, `config/slack.yaml` or `config/classifier.yaml`, or if an
assignment rule refers to a value that does not exist.

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

## Google Sheets setup

1. In Google Cloud, enable the Google Sheets API, create a service account and save its
   JSON key as `secrets/google-service-account.json` (git-ignored).
2. Share the spreadsheet with the service account's `client_email` as an **Editor**.
3. Give the worksheet a header row. Default header names are `Title`, `Description`,
   `Reporter`, `Contact`, `System ID`, `Detail link`, plus `Status` and `Latest note`
   (written back from Jira in a later milestone).
4. Register the worksheet. The command checks access and headers before saving:

   ```bash
   uv run issue-sync add-document --name "Support requests" \
     --spreadsheet "https://docs.google.com/spreadsheets/d/<id>/edit" --worksheet "Sheet1"
   # other header names: --map title="Issue"   drop an optional field: --map contact=
   ```

The worker polls every registered worksheet (`SHEETS_POLL_INTERVAL_SECONDS`, default 60).
For near-instant sync, install the Apps Script trigger in
[`apps_script/Code.gs`](apps_script/Code.gs). It calls `POST /webhooks/sheets`, so the api
must be reachable from Google (for local testing, use a tunnel such as `cloudflared`).

## AI triage setup

Any OpenAI-compatible chat completions API works. Set `LLM_BASE_URL`, `LLM_MODEL` and
`LLM_API_KEY` in `.env` (DeepSeek is the default in `.env.example`), and copy
`config/classifier.example.yaml` to `config/classifier.yaml`. That file describes each
category, urgency and team so the model knows what to choose. Check the quality on the
labelled cases before going live, and again after changing the prompt config or model:

```bash
uv run issue-sync eval-classifier                     # per-field accuracy and latency
uv run issue-sync eval-classifier --min-accuracy 0.9  # exit 1 below 90 %
```

## Slack notifications and assignment rules

1. Create a Slack app with the bot scope `chat:write`, install it in the workspace and put
   its bot token (`xoxb-...`) in `.env` as `SLACK_BOT_TOKEN`.
2. In `config/slack.yaml`, set `default_channel` (channel ID, `C...`) and the Slack member
   ID (`U...`) for each team. Invite the bot to the channel (`/invite @your-app`).
3. Copy `config/assignment.example.yaml` to `config/assignment.yaml` and adapt the rules.
   They adjust the AI's choice of team and urgency, top to bottom, first match wins:

   ```yaml
   rules:
     - name: Access requests go to ops
       when: {type: access_request}
       assign: ops
     - name: Month-end close is at least high
       when: {keywords: [month-end], urgency: [low, medium]}
       set_urgency: high
   ```

Each new ticket is posted to the channel with an `@mention` of its assignee, exactly once.
When a rule changed the AI's choice, the ticket gets a comment saying so.

## Jira service account and webhook

1. In Atlassian Administration > Directory > Service accounts, create a service account
   with the Jira role **User**, and add it to the project as a member.
2. Create an **OAuth 2.0** credential for it with the scopes `read:jira-work`,
   `write:jira-work` and `read:jira-user`. Put the client id and secret in `.env`
   (`JIRA_AUTH=oauth`, `JIRA_CLIENT_ID`, `JIRA_CLIENT_SECRET`) together with the site's
   cloud id (`JIRA_CLOUD_ID`; `https://<site>/_edge/tenant_info` shows it).
3. Run `uv run issue-sync jira-check`. Its first check prints the service account's
   accountId: put it in `config/jira.yaml` as `service_account_id`. Names in `jira.yaml`
   (issue type, statuses) must be the ones the service account sees, which may be English
   even on a site in another language.
4. In Jira, Settings > System > WebHooks > Create a WebHook:
   - URL: `https://<public host>/webhooks/jira`
   - Secret: the value of `JIRA_WEBHOOK_SECRET`
   - Events: Issue **updated**, Comment **created**; JQL: `project = <KEY>`

Status changes, priority/assignee changes and new comments made in Jira then flow back to
the database, and the sheet's Status and Latest note columns are updated. Changes made
by the service account itself are ignored, so the sync never loops.

## Reconciliation and alerts

Every 15 minutes (`RECONCILE_INTERVAL_MINUTES`) the worker reconciles everything that
events can miss:

- ticket status, priority, assignee and comments whose webhook never arrived;
- titles and descriptions edited in Jira, restored from the sheet (the sheet owns them);
- rows deleted from the sheet: the record is marked deleted, and the ticket is kept;
- System IDs copied into several rows or worksheets;
- pushes that failed or got stuck (re-queued, then reported) and missing notifications.

Problems that need a person are posted to Slack (`alert_channel` in `config/slack.yaml`,
default: `default_channel`), each at most once a day. To run it now:

```bash
uv run issue-sync reconcile
```

## How a row becomes a ticket

1. A trigger or the poll enqueues `sync_document`, which re-reads the worksheet.
2. Rows without a `System ID` get a UUID and a detail link written back to the sheet.
3. Rows whose content changed (SHA-256 of the row, system columns excluded) are saved
   with a `created` or `source_modified` event and marked for a push.
4. `push_ticket` classifies new requests with the LLM (category, urgency, team, summary,
   reason), applies the assignment rules, then creates the Jira issue with the mapped
   priority and assignee, and queues the Slack notification. Or it updates
   the title and description of an existing one. It retries with backoff on transient
   failures. Pushes are serialized per row and look for an existing ticket before
   creating one, so a row never produces two tickets.

See [docs/decisions.md](docs/decisions.md) for design decisions.
