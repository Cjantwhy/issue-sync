# Design decisions

One entry per trade-off: context, what was chosen, and why not the alternative.

## Milestone 1 — Project skeleton

### Migrations run in a one-shot `migrate` service
- **Context:** Migrations must run automatically on `docker compose up`, and the api may later run as several replicas.
- **Chosen:** A dedicated `migrate` service (`alembic upgrade head`, then exit). `api` and `worker` depend on it with `service_completed_successfully`.
- **Not chosen:** Migrating in the api's startup. Every replica would run DDL at the same time, and the worker could start against a half-migrated schema.

### Advisory lock around migrations
- **Context:** Two migration runs can still overlap by accident, e.g. a deploy script plus a manual run.
- **Chosen:** `migrations/env.py` takes `pg_advisory_xact_lock` inside the migration transaction, before Alembic reads `alembic_version`. A second run waits, then finds the database at head and does nothing. The lock is released on commit or rollback, so a crashed run cannot leave it held.
- **Not chosen:** A Redis lock. That adds a dependency to the migration path, and a lock outside the database cannot be tied to the DDL transaction.

### Initial migration executes the SQL files verbatim
- **Context:** `db/schema.sql` and `db/extras.sql` are the final, authoritative schema.
- **Chosen:** Revision `0001` reads both files and runs each one as a whole script through asyncpg's simple-query protocol, inside the migration transaction.
- **Not chosen:** Restating the schema with `op.create_table()`, which would create a second source of truth. Splitting the files on `;` would break the `$$`-quoted trigger function body.

### ORM models are verified against the migrated database
- **Context:** The ORM must match the hand-written SQL exactly.
- **Chosen:** A `MetaData` naming convention that reproduces PostgreSQL's default constraint names (`<table>_<cols>_key`, `_fkey`, `_pkey`). An integration test runs Alembic's `compare_metadata` against a freshly migrated database and requires zero differences.
- **Not chosen:** Relying on code review alone. Drift between the models and the schema is silent until a query fails in production.

### ORM relationships use `lazy="raise"`
- **Context:** Under asyncio, an implicit lazy load raises an obscure greenlet error or hides N+1 queries.
- **Chosen:** Accessing a relationship that was not loaded fails immediately; related rows are loaded explicitly with `selectinload` or `joinedload`.

### Startup mapping check: missing is fatal, extra is a warning
- **Context:** Every internal value in the constant tables needs a mapping in `jira.yaml` / `slack.yaml`.
- **Chosen:** Both the api and the worker load the constant tables at startup and refuse to start if any value, or a whole table, is unmapped. All problems are listed in one error. Mappings for values that do not exist in the database are only logged as warnings.
- **Not chosen:** Making extra keys fatal as well. They are harmless, and tolerating them lets the config be updated before the matching seed rows are added.

### Strict adapter config models
- **Chosen:** The Pydantic config models reject unknown keys (`extra="forbid"`) and blank values. A typo such as `priority:` instead of `priorities:` fails at load time instead of being silently ignored.

### Jira status mapping is one-to-one
- **Context:** Statuses are translated in both directions: internal → Jira for transitions, Jira → internal for webhooks.
- **Chosen:** One Jira status per internal status, validated to be unique (case-insensitive). Inbound translation is then unambiguous.
- **Not chosen:** Several Jira statuses per internal status. More flexible, but not needed yet, and it makes the outbound direction ambiguous.

### `/health` returns 503 on failure, with no error details
- **Chosen:** The database and Redis are probed concurrently, each with a 2-second timeout. If any probe fails, the endpoint returns 503, so Docker and load-balancer health checks act on it. Exception details go to the log only, because the endpoint is unauthenticated.

### Settings are split into `DatabaseSettings` and `Settings`
- **Chosen:** The migration runner only needs `DATABASE_URL`, so `migrations/env.py` uses `DatabaseSettings`. Running migrations does not require Redis or adapter config.

### One image for api, worker and migrate
- **Chosen:** A single multi-stage image; compose selects the command for each service. Only the venv and runtime files reach the final stage, and real adapter configs are mounted read-only at runtime.
- **Not chosen:** Separate images, which would triple build time without any benefit.

### uv installed from PyPI in the Dockerfile
- **Context:** The usual `COPY --from=ghcr.io/astral-sh/uv` adds a second registry to the build. It fails when ghcr.io is unreachable or a stale login is cached.
- **Chosen:** `pip install uv==<pinned>` in the builder stage, so only Docker Hub and PyPI are needed.

### Redis runs with AOF persistence
- **Chosen:** `--appendonly yes`, so arq jobs that are queued or waiting to retry survive a Redis restart.

### arq worker registers a `ping` job
- **Context:** arq refuses to start with no functions registered, and real jobs arrive in later milestones.
- **Chosen:** A trivial `ping` job. It also lets you verify end to end that the worker consumes the queue.

### Integration tests use throwaway databases
- **Chosen:** Tests marked `integration` need `TEST_DATABASE_URL` and are skipped without it. Each run creates a uniquely named database and drops it with `WITH (FORCE)`, so existing data is never touched. The concurrent-migration test starts separate `alembic` processes, because Alembic's `context` and `op` are process-global and threads in one process would corrupt each other.

## Milestone 2 — Ticket backend interface and Jira implementation

### The interface speaks internal values only
- **Context:** The sync flow must not depend on Jira's vocabulary.
- **Chosen:** `TicketBackend` takes and returns internal statuses, urgencies and roles. `JiraBackend` translates them with `config/jira.yaml`. The interface has only the six operations the flow needs.
- **Not chosen:** A generic multi-backend abstraction. Jira is the only target, and a thin Protocol is enough to keep the flow testable.

### Webhook parsing is deferred to milestone 6
- **Chosen:** The interface does not have `parse_webhook` yet. Its shape depends on the loop-prevention rules (service-account filter, stale-event drop), which are designed together in milestone 6.

### Duplicate-create protection with a system_id label
- **Context:** A create request can time out after Jira has already created the issue. A blind retry would then open a second ticket for the same row, and the database's unique constraint cannot catch it because no `issue_key` was ever stored.
- **Chosen:** Every issue is created with the label `issue-sync-<system_id>`. Before retrying a create, the caller runs `find_by_system_id` (a JQL label search). If it finds more than one match, it raises `DuplicateTickets` for an operator to resolve.
- **Not chosen:** Issue entity properties. They are hidden from users, but JQL can only search properties declared by a Connect app, so they cannot be used for the lookup. Doing nothing was also rejected, because network blips would create duplicate tickets.
- **Caveat:** Jira's search index lags a few seconds behind writes, so the retry runs after a backoff delay instead of immediately.

### Field ownership in the Jira payloads
- **Chosen:** Creating an issue sets summary, description, labels, priority and assignee. Updating it only rewrites summary and description, which the sheet owns. Priority and assignee belong to Jira once the issue exists, so a sheet edit never overrides a human's triage. The description ends with a note that it is synced from the sheet and that edits made in Jira will be overwritten.

### User text is never interpreted as markup
- **Chosen:** A small ADF builder turns plain text into paragraphs (a blank line separates paragraphs, a single newline becomes a hard break) and strips control characters. Text from the sheet is never parsed as Markdown or wiki markup, so a reporter cannot inject links or formatting. The only link is the detail-page URL that the system generates itself.
- **Not chosen:** Jira API v2 with wiki-markup strings. API v3 requires ADF, and string markup would make injection possible.

### Summary normalization
- **Chosen:** Jira rejects summaries that contain line breaks or are longer than 255 characters. The title is collapsed to a single line and cut at 254 characters plus "…". An empty title becomes "(untitled request)" instead of failing the create.

### The client classifies errors but does not retry
- **Chosen:** `JiraClient` maps failures to error types:
  - timeouts, network errors, 429 and 5xx → `TicketBackendUnavailable` (retryable, carries `Retry-After`);
  - 401 and 403 → `TicketAuthError`;
  - 404 → `TicketNotFound`;
  - other 4xx → `TicketRequestRejected`, including Jira's field-level messages.

  The arq job that called the backend is the only place that retries.
- **Not chosen:** Retries inside the HTTP client. Two retry layers multiply the attempts and hide the real failure rate from `retry_count`.

### Issue keys are validated before building URLs
- **Chosen:** Keys must match `^[A-Z][A-Z0-9_]+-[1-9][0-9]*$` before they are put into a request path. A corrupted or malicious value such as `OPS-1/../../myself` cannot reach another endpoint.

### Transitions are matched by target status, not transition name
- **Chosen:** `transition()` lists the issue's available transitions and picks the one whose `to.name` equals the mapped Jira status (case-insensitive). If the issue is already in that status, it does nothing and returns `False`. Transition names vary between workflows, but status names are what `jira.yaml` maps. When no transition reaches the target status, the error lists the statuses that can be reached.

### `issue-sync jira-check` instead of a startup check
- **Context:** Most Jira misconfigurations only show up when the first issue is created, e.g. priority not on the create screen, a required custom field, a non-assignable user, or a token for the wrong account.
- **Chosen:** An operator command that checks the live site and reports every check, then exits with code 1 if any failed. It checks:
  - the token's account matches `service_account_id`;
  - the project and issue type exist;
  - the create screen has every field the system sets, and no required field is left unfilled;
  - each mapped priority, workflow status and assignee exists and is usable.
- **Not chosen:** Running these checks at startup. A temporary Jira outage would then stop the api from booting, and inbound processing does not need Jira to be reachable. The database-vs-YAML startup check from milestone 1 stays, because it needs no network.

### Settings split per concern
- **Chosen:** Jira credentials live in `JiraSettings`, which the full `Settings` inherits. `jira-check` loads only `JiraSettings`, so it runs without database or Redis variables. The API token is a `SecretStr`, so it never appears in logs or reprs.

### A rejected token is detected by header, not only by status code
- **Context:** Found in the live test: with a bad or revoked token, Jira Cloud treats the request as anonymous, so resource endpoints answer 404 ("issue does not exist") instead of 401.
- **Chosen:** The client treats any response carrying `X-Seraph-LoginReason: AUTHENTICATED_FAILED` (or `AUTHENTICATION_DENIED`) as `TicketAuthError`, whatever the status code.
- **Not chosen:** Trusting the status code alone. An expired token would look like a deleted issue, and later sync logic would act on tickets that still exist.

### Jira names are matched in the service account's language
- **Context:** Found in the live test on a zh-CN site: createmeta returns localized issue type names ("任务") while `/project/{key}/statuses` returns English ones ("Task"). Status names are also shown in the site's language ("待办", "正在进行", "已完成").
- **Chosen:** `jira-check` matches the issue type across endpoints by id. `jira.yaml` uses the names as the service account sees them, and `jira-check` verifies them with the same account. The service account's language should not be changed after setup.
- **Not chosen:** Switching `jira.yaml` to numeric ids. Names keep the config readable and are verified by `jira-check`; ids would only be needed if the account's language had to change.
