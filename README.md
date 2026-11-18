# issue-sync

Two-way sync between a Google Sheet and Jira Cloud, with AI triage and assignment.

People report problems where they already work: a shared Google Sheet, or a Google Form
that writes into one. issue-sync turns each row into a Jira ticket, lets an LLM decide
the category, urgency and team, assigns it, and notifies the assignee in Slack. Status
changes and comments made in Jira flow back to the sheet, and every requester gets a
read-only link with the full history of their request, attachments included.

![Dashboard: AI suggestion versus the ticket today](docs/images/dashboard.png)

## Features

- **Sheet or form in, Jira ticket out.** Columns are matched by header name, so people
  can reorder or add columns. Each row gets a System ID and a detail link written back.
- **Never triages a half-typed row.** A new row is taken once its required fields are
  filled in and it has stopped changing; form submissions are taken at once.
- **AI triage** through any OpenAI-compatible API (DeepSeek, OpenAI, OpenRouter, ...):
  category, urgency, team, summary and reasoning, as validated structured output.
- **Deterministic assignment rules** on top of the AI ("access requests go to ops"),
  explained in a ticket comment when they override it.
- **Slack notification** to the assignee for every new ticket, exactly once.
- **Jira → sheet**: status and the latest comment are written back to the sheet;
  priority, assignee, comments and attachments are recorded for the dashboard and the
  detail page. Loops are prevented by design.
- **Reconciliation every 15 minutes** repairs whatever webhooks missed, restores
  sheet-owned fields edited in Jira, marks deleted rows, detects copied row identities,
  and alerts on what needs a person.
- **Dashboard** comparing the AI's suggestion with what the ticket has today, per model,
  so you can see how often people override it, plus a public, read-only **detail page**
  per request.

![Detail page of one request](docs/images/detail.png)

## Architecture

```mermaid
flowchart LR
    subgraph Google
        S[Google Sheet]
        F[Google Form]
        AS[Apps Script trigger]
    end
    subgraph Host["Docker Compose"]
        CF[cloudflared]
        API["api<br/>FastAPI · Jinja2 + HTMX"]
        Q[(Redis<br/>arq queue)]
        W["worker<br/>arq jobs + cron"]
        DB[(PostgreSQL)]
    end
    J[Jira Cloud]
    SL[Slack]
    LLM[LLM API]

    F --> S
    S -- edit / form submit --> AS -- webhook --> CF
    J -- webhook --> CF
    CF --> API
    API -- enqueue --> Q --> W
    W <-- Sheets API --> S
    W <-- REST API --> J
    W --> SL
    W --> LLM
    API --- DB
    W --- DB
```

- **api** authenticates webhooks and only enqueues work, so Google and Jira get an
  answer in milliseconds. It also serves the dashboard and the detail pages.
- **worker** does everything that talks to other systems, with retries and backoff,
  plus two scheduled jobs: polling the sheets (a safety net for missed triggers) and
  reconciliation.
- **PostgreSQL** holds the current snapshot of every request, an append-only event log,
  every AI judgement and the per-ticket sync state.
- **One public entry point** (a Cloudflare Tunnel, or a reverse proxy such as Caddy)
  exposes only the webhooks and the detail pages. See
  [docs/deployment.md](docs/deployment.md).

## How data flows

**Who owns which field** decides every sync rule:

| Field | Owned by | Direction |
|---|---|---|
| Title, description, reporter, contact, attachment links | Sheet | sheet → database → Jira |
| Status, priority, assignee, comments, Jira attachments | Jira | Jira → database → sheet |
| System ID, detail link | issue-sync | database → sheet |

Hand edits to a column the other side owns are overwritten with the owner's value.

### Sheet → Jira

```mermaid
sequenceDiagram
    participant S as Sheet
    participant W as worker
    participant DB as PostgreSQL
    participant L as LLM
    participant J as Jira
    participant SL as Slack
    S->>W: trigger or poll: sync_document
    W->>S: read the worksheet
    Note over W: new row complete and unchanged for 2 min<br/>(form rows: at once)
    W->>S: write System ID + detail link
    W->>DB: record + "created" event (row lock, hash)
    W->>L: classify (once per request)
    W->>DB: AI judgement
    Note over W: assignment rules
    W->>J: create issue (priority, assignee, labelled with the System ID)
    W->>J: read back status, priority, assignee
    W->>SL: notify the assignee
```

Later edits in the sheet update the ticket's title and description. A push is skipped
when the content hash has not changed.

### Jira → sheet

Jira's webhook is checked (HMAC), queued, then applied. Three guards prevent loops:
changes made by the service account are ignored; an event older than the newest one
already applied is dropped (each event carries the ticket's full state, so dropping it
loses nothing); and a value equal to the database writes no event. Status and the latest
comment are written back to the sheet in the same pass that reads it, under the same lock.

### Reconciliation

Webhooks get lost (in testing, a firewall blocked some, and Jira does not retry those).
Every 15 minutes the worker compares Jira with the database in batches of 100, recovers
missed status changes, comments and attachments, restores titles or descriptions edited
in Jira, marks rows that disappeared from the sheet (refusing to mark more than half a
worksheet at once), and posts what needs a person to Slack, at most once a day each.

## Design decisions

Each decision has its context, the choice and the rejected alternative in
[docs/decisions.md](docs/decisions.md). The ones that shape the system most:

- **The row's identity is a UUID written into the sheet**, never the row number. Rows
  can be sorted, inserted or moved without losing their ticket, and the UUID makes the
  public detail URL unguessable.
- **One ticket per row, even under failure.** Pushes for a row are serialized with
  `SELECT … FOR UPDATE`, the ticket is labelled with the row's UUID, and a create that
  timed out is looked up before retrying. A unique constraint is the last line.
- **Every webhook is a hint, never the data.** The Sheets webhook only names the
  worksheet; the worker re-reads it. So a forged or repeated call can only cause an extra
  read.
- **Internal values in the database, translations in adapter configs.** Statuses,
  urgencies and teams are stored as internal values; `config/jira.yaml` and
  `config/slack.yaml` map them. Both processes refuse to start if a value has no mapping,
  instead of failing on the first ticket that needs it.
- **AI suggests, rules decide, people override, and all three are kept.** The judgement
  is stored as the model gave it, the rule result as it was applied at creation, and the
  ticket's current values as Jira reports them. The dashboard's per-model agreement rate
  is computed from those three.
- **A new row waits until it is complete and settled**, because the AI judges a request
  once. Form submissions skip the wait: the form writes the whole row at once.
- **Append-only history** of created, edited, status, comment, attachment and deletion
  events. Deleted rows are marked, never cascaded to Jira.
- **Small, explicit adapters.** One `TicketBackend` protocol with a Jira implementation,
  one `Classifier` with an OpenAI-compatible implementation. They keep vendor details
  out of the sync logic and make every external service easy to fake in tests.

## Running it locally

Requires Docker with Compose v2.24+.

```bash
cp .env.example .env
for f in jira slack classifier assignment; do cp config/$f.example.yaml config/$f.yaml; done
# Google service account key: secrets/google-service-account.json (see below)
docker compose up -d --build --wait
curl localhost:8000/health
# {"status":"ok","checks":{"database":"ok","redis":"ok"}}
```

`migrate` applies the database migrations and exits; `api` and `worker` start after it
succeeds. Set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` to open
`http://localhost:8000/dashboard`.

**No public URL is needed to try it.** Without webhooks, the worker polls the sheet
every minute (sheet → Jira), and reconciliation brings status changes and comments back
from Jira every 15 minutes. Instant updates both ways, and detail links that open
outside your machine, need a public HTTPS entry point. Any tunnel works for a quick
test (`cloudflared tunnel --url http://localhost:8000`, ngrok).

For production (no published database ports, a Cloudflare Tunnel or a reverse proxy,
log rotation) see [docs/deployment.md](docs/deployment.md). For a five-minute walkthrough of every feature,
see [docs/demo.md](docs/demo.md).

## Configuration

Secrets and settings come from environment variables (`.env`, see
[`.env.example`](.env.example) for every variable with comments). Mappings to external
systems live in the adapter configs, each with a committed `*.example.yaml`:

| File | Contents |
|---|---|
| `config/jira.yaml` | Project key, issue type, service account id, status / priority / assignee mappings |
| `config/slack.yaml` | Channel for new tickets, alert channel, Slack user per team |
| `config/classifier.yaml` | What each category, urgency and team means, for the prompt |
| `config/assignment.yaml` | Assignment rules applied after the AI |

| Variable | Default | Purpose |
|---|---|---|
| `PUBLIC_BASE_URL` | | Public URL; detail links use it |
| `SHEET_REQUIRED_FIELDS` | `title,description` | Fields a new row needs before it is taken |
| `NEW_ROW_SETTLE_SECONDS` | `120` | How long a typed row must stay unchanged |
| `SHEETS_POLL_INTERVAL_SECONDS` | `60` | Polling safety net |
| `RECONCILE_INTERVAL_MINUTES` | `15` | Reconciliation schedule |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` | | Any OpenAI-compatible chat completions API |
| `DASHBOARD_USERNAME`, `DASHBOARD_PASSWORD` | unset (dashboard off) | HTTP Basic sign-in |
| `DISPLAY_TIMEZONE` | `UTC` | Time zone shown on the pages |

### Google Sheets

1. In Google Cloud, enable the Google Sheets API, create a service account and save its
   JSON key as `secrets/google-service-account.json` (git-ignored).
2. Share the spreadsheet with the service account's `client_email` as an **Editor**.
3. Give the worksheet a header row. Default names: `Title`, `Description`, `Reporter`,
   `Contact`, `Attachments`, `System ID`, `Detail link`, `Status`, `Latest note`. Only
   `Title`, `System ID` and `Detail link` are required.
4. Register it; the command checks access and headers first:

   ```bash
   docker compose exec worker issue-sync add-document --name "Support requests" \
     --spreadsheet "https://docs.google.com/spreadsheets/d/<id>/edit" --worksheet "Sheet1"
   # other header names: --map title="Issue"   drop an optional field: --map contact=
   # change a registered worksheet's mapping later: add --update
   ```

5. For instant sync, paste [`apps_script/Code.gs`](apps_script/Code.gs) into the
   spreadsheet's Apps Script editor, set the script properties `ISSUE_SYNC_URL`
   (`https://<public host>/webhooks/sheets`) and `ISSUE_SYNC_SECRET`
   (`SHEETS_WEBHOOK_SECRET`), and run `installTriggers` once. Without it, the worker
   polls every minute.

**Google Form as the intake** (recommended for requesters outside the team): link the
form to the spreadsheet, add `System ID`, `Detail link` and `Status` columns to the right
of the form's response columns, and register the response worksheet with `--map` for the
question titles (e.g. `--map title="What went wrong?"`). A file-upload question maps to
`attachments`. Run `installTriggers` again after linking the form.

### Jira

1. In Atlassian Administration > Directory > Service accounts, create a service account
   with the Jira role **User** and add it to the project.
2. Create an **OAuth 2.0** credential with the scopes `read:jira-work`,
   `write:jira-work` and `read:jira-user`, and put it in `.env` (`JIRA_AUTH=oauth`,
   `JIRA_CLIENT_ID`, `JIRA_CLIENT_SECRET`, `JIRA_CLOUD_ID` from
   `https://<site>/_edge/tenant_info`). A regular account's API token works too
   (`JIRA_AUTH=basic`).
3. Run `issue-sync jira-check`. It checks credentials, project, create-screen fields,
   priorities, workflow statuses and assignees, and prints the service account's
   accountId for `service_account_id` in `config/jira.yaml`.
4. Settings > System > WebHooks > Create: URL `https://<public host>/webhooks/jira`,
   secret `JIRA_WEBHOOK_SECRET`, events *Issue updated* and *Comment created*,
   JQL `project = <KEY>`.

### Slack

Create a Slack app with the bot scope `chat:write`, install it, put its `xoxb-` token in
`.env` as `SLACK_BOT_TOKEN`, set the channel and one member ID per team in
`config/slack.yaml`, and invite the bot to the channel.

### AI triage

Set `LLM_BASE_URL`, `LLM_MODEL` and `LLM_API_KEY`, and describe your categories, urgencies
and teams in `config/classifier.yaml`. Measure the result on labelled cases before going
live and after any prompt or model change:

```bash
docker compose exec worker issue-sync eval-classifier --min-accuracy 0.9
```

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest                      # external services are mocked; no network needed

# Integration tests against PostgreSQL (each module uses a throwaway database)
TEST_DATABASE_URL=postgresql+asyncpg://issue_sync:change-me@localhost:5432/postgres uv run pytest
```

CI runs the same lint and the full test suite against PostgreSQL 16, builds the image,
and validates both Compose files.

| Path | Contents |
|---|---|
| `src/issue_sync/api/` | FastAPI app, webhooks, health |
| `src/issue_sync/sync/` | Inbound, push, ticket events, notifications, reconciliation |
| `src/issue_sync/tickets/` | `TicketBackend` protocol and the Jira Cloud implementation |
| `src/issue_sync/classify/` | `Classifier` protocol, OpenAI-compatible implementation, evaluation |
| `src/issue_sync/sources/sheets/` | Sheets API client and header-based row parsing |
| `src/issue_sync/web/` | Dashboard and detail pages (Jinja2 + HTMX) |
| `db/schema.sql`, `migrations/` | Schema (source of truth) and Alembic history |

## Security

- No secret is in the repository; everything comes from `.env` and the mounted
  `secrets/` directory.
- Webhooks are authenticated in the app (HMAC-SHA256 for Jira, constant-time shared
  secret for Apps Script), and their payloads are never trusted as data.
- The dashboard needs a sign-in and is not exposed by the tunnel. Detail pages are
  public by link only: random UUIDs, no contact details, `noindex`, `no-referrer`, a
  strict Content-Security-Policy, and only http(s) links rendered.
- The app image runs as a non-root user. In production nothing accepts connections from
  the internet: the tunnel connector dials out to Cloudflare, and the worker's calls to
  Google, Jira, Slack and the LLM are outbound HTTPS.
