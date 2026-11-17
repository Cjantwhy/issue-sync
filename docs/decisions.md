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

## Milestone 3 — Sheets intake and the main pipeline

### Triggers only name the worksheet
- **Chosen:** The Apps Script trigger and the poll both just enqueue `sync_document(document_id)`. The job always re-reads the whole worksheet through the API. The webhook payload is never trusted for content, and a missed or duplicated trigger is harmless.
- **Not chosen:** Sending the edited cells in the webhook. Apps Script triggers are best effort (they miss some edits and can fire twice), and the payload would become a second source of truth.

### One sync run per worksheet at a time
- **Context:** An Apps Script push and the poll can fire together. Two runs that both see a row without an id would each write a different UUID and create two records.
- **Chosen:** A run holds `SELECT ... FOR NO KEY UPDATE` on its `document` row until it finishes; a second run waits, then reads the sheet with the ids already written. `NO KEY UPDATE` rather than `UPDATE`, because inserting a record takes a `KEY SHARE` lock on the same row for the foreign key, which `FOR UPDATE` would block. Each row is then written in its own transaction under `SELECT ... FOR UPDATE` on the record.
- **Not chosen:** De-duplicating jobs with a fixed arq job id. arq drops an enqueue while a job with that id is running, so an edit made during a run would wait for the next poll.

### Identity is written to the sheet before the record exists
- **Chosen:** New rows first get their UUID and detail link written to the sheet, then the record is inserted. If anything fails afterwards, the next run sees a row with an unknown UUID and creates the record with that UUID; it never invents a second one. Right before writing, the sheet is re-read, and a row is only claimed if it is still at the same position with the same content and an empty id cell.
- **Not chosen:** Insert first, write back later. A failed write-back would leave a record the sheet cannot point to, and the next run would create a duplicate.
- **Caveat:** The Sheets API has no compare-and-set, so a row inserted in the few milliseconds between the re-read and the write could still receive the id. Reconciliation (milestone 7) detects the resulting orphan.

### Rows with bad or repeated ids are skipped, not repaired
- **Chosen:** A system_id cell that is not a UUID, appears twice in the worksheet, or belongs to another document is logged and the row is skipped. It is never overwritten with a fresh id, because that could turn one request into two tickets. Repairing it is an operator task; milestone 7 adds alerts.

### Change detection ignores system-owned columns
- **Chosen:** `raw` keeps the whole row. `raw_hash` covers every column except system_id, detail link, status and latest note; otherwise writing a link back would look like an edit. A change in an unmapped column updates `raw` silently. Only changes to mapped fields write a `source_modified` event (with before/after values) and mark the ticket for a push.

### Push queue state lives in `ticket_sync.push_status`
- **Chosen:** A new or edited record is `pending`. Every sync run returns all pending records of the worksheet and enqueues a push for each, including ones from earlier runs. A push job lost between the commit and the enqueue (e.g. Redis briefly down) is therefore recovered by the next poll. Pushing is idempotent (`pushed_hash` comparison), so an extra job costs nothing.

### A push locks `ticket_sync` across the HTTP call and always searches before creating
- **Chosen:** `push_record` holds `SELECT ... FOR UPDATE` on the record's `ticket_sync` row for the whole push, including the Jira calls, so pushes of one record never overlap. Before creating, it always looks for a ticket with the record's `issue-sync-<system_id>` label, so a timed-out create, or a worker that crashed after Jira answered, is linked instead of duplicated. That costs one search per new ticket.
- **Not chosen:** Searching only when `retry_count > 0`. A crash between Jira's response and the commit leaves no trace in the database, so the counter cannot be trusted for this.

### Retry policy
- **Chosen:** Backoff of 10 s, doubled each attempt, capped at 15 minutes; a longer `Retry-After` wins. The first delay is not shorter because Jira's search index lags a few seconds behind a create. Pushes give up after `PUSH_MAX_ATTEMPTS` (default 8, about 20 minutes) and stay `failed` with `last_error`. A failed sync run is retried a few times and then left to the next poll, since nothing is lost.

### Detail links are corrected on every run
- **Chosen:** A detail link that is missing or edited by hand is rewritten, and `sheet_written_hash` / `sheet_written_at` are recorded. Unlike ids, links are written without re-reading first: if rows shift meanwhile, the next run fixes the wrong link, and no identity is touched.

### Sheet values: read formatted, write raw
- **Chosen:** Rows are read as `FORMATTED_VALUE`, so the system stores what people see. System cells are written with `valueInputOption=RAW`, so nothing the system writes is interpreted as a formula. `title`, `reporter` and `contact` are cut to their 255-character columns; `raw` keeps the full text.

### Google auth without a second HTTP stack
- **Chosen:** google-auth only signs the service-account JWT. The token exchange and the Sheets calls go through httpx, so they are async and mocked with respx like Jira. The token is cached and refreshed 5 minutes before expiry; one automatic retry after a 401 covers a token revoked early.
- **Not chosen:** google-api-python-client, which is synchronous and brings its own transport into an async worker.

### Worksheets are registered with `issue-sync add-document`
- **Chosen:** The command reads the worksheet's header row first and refuses a mapping that does not match, before writing the `document` row. Optional fields are only mapped when their default header exists, so a typical sheet needs no flags. Spreadsheet ids and mappings live in the database, not in env vars, so one deployment can sync several sheets.

### Service account key mounted as a directory
- **Chosen:** Compose mounts `./secrets` read-only rather than the single key file. Docker silently creates a missing single-file mount as an empty directory, which then fails with a confusing error.

## Milestone 4 — AI triage

### One LLM call decides everything
- **Context:** Category, urgency, team, summary and reason could come from separate models, e.g. a trained classifier for the labels plus an LLM for the text, or a cheap model that escalates to a stronger one.
- **Chosen:** One structured call to one model. Requests are judged once each and volume is low, so cost and latency are not the bottleneck.
- **Not chosen:** A trained classifier needs labelled history, and there is none yet. Escalating between models needs a calibrated confidence score, and self-reported LLM confidence is not one. The data for both accumulates on its own: every judgement stores its `model`, and the dashboard (milestone 8) compares it with the values humans set in Jira. Deterministic overrides belong to the assignment rules in milestone 5, which stay auditable.

### Triage runs inside the push, once per record
- **Chosen:** When a record has no ticket and no judgement, `push_ticket` classifies it before creating the ticket, under the same `ticket_sync` row lock, so a record is never classified twice concurrently. The judgement is stored in the same transaction as the attempt. If Jira then fails, the retry reuses the stored judgement instead of calling the LLM again. Only new records are judged; sheet edits do not trigger a new judgement.
- **Not chosen:** A separate classify job between sync and push. The push would then have to wait for it, and an ordering bug would create tickets without priority or assignee.

### Keep creating tickets when the LLM is down
- **Chosen:** Timeouts, rate limits, 5xx and malformed output are retried with the normal backoff. After `LLM_MAX_ATTEMPTS` (default 3), the ticket is created without a judgement: Jira's default priority, no assignee, and a description that says AI triage is not available. Errors a retry cannot fix (401 bad key, 402 no balance, 400) fall back at once.
- **Not chosen:** Holding tickets until the LLM recovers. An AI outage would then stop all intake, which is worse than a ticket a human has to triage.

### Allowed values come from the database, descriptions from `classifier.yaml`
- **Chosen:** The model may only answer with names from the constant tables. `config/classifier.yaml` describes each category, urgency and team in words, because "ops" alone does not tell the model what ops handles. Its keys go through the same startup check as `jira.yaml`. The schema has no column for descriptions, and wording is configuration, not data.

### JSON mode plus validation, not provider-enforced schemas
- **Context:** DeepSeek supports `response_format: json_object` but not `json_schema`.
- **Chosen:** `json_object` by default, with the allowed values and an example in the prompt. Every reply is validated in code: enums are checked case-insensitively, free text is length-capped, and blank or invalid output counts as a retryable failure. `LLM_RESPONSE_FORMAT=json_schema` turns on strict schemas for providers that support them. `temperature` is 0.
- **Not chosen:** Tool calling as a structured-output workaround. It adds a round of parsing without stronger guarantees than validation.

### Provider differences go into `LLM_EXTRA_BODY`
- **Chosen:** The client speaks plain OpenAI chat completions. Provider-specific fields come from a JSON env var and are merged into the request body; they can never override the model, messages or response format. For DeepSeek, `{"thinking": {"type": "disabled"}}` turns off the default reasoning mode, which costs latency and tokens without helping a short classification.

### Request text is data, and only the request text is sent
- **Chosen:** The system prompt says the request is untrusted and any instructions in it must be ignored. Title and description are sent as a JSON object, so quotes in them cannot close the data block, and they are truncated to a fixed size. Reporter names and contact details are never sent to the LLM. Because the output is limited to fixed enums, a successful injection could at worst mislabel one request. Verified live: a request asking to be marked critical and assigned to ops was classified as a low-urgency question, and the model's reason said it had ignored the embedded instructions.

### The judgement is shown in the ticket
- **Chosen:** The Jira description has an "AI triage" section with category, urgency, suggested team, summary, reason and model, so the assignee can see why the ticket landed with them and correct it. Because the description is rebuilt on every push, the section is part of `pushed_hash`.

### `issue-sync eval-classifier`
- **Chosen:** A labelled case file (`eval/triage_cases.yaml`) is run against the real model, and the command reports accuracy per field, accuracy on all three, errors and latency. Urgency is subjective, so a case may list more than one acceptable value. `--min-accuracy` makes it usable as a gate. A unit test checks that every expected value exists in the classifier config.
- **Caveat:** The 12 bundled cases are hand-written and catch regressions; they are not a benchmark. Real accuracy will come from comparing judgements with human corrections (milestone 8).

## Milestone 5 — Slack notifications and assignment rules

### Schema change: `ticket_sync.notified_at` (approved)
- **Context:** A notification must not be lost or sent twice, and the final schema had no place to record that it was sent. Without one, a crash between creating the ticket and enqueuing the notification would lose it silently.
- **Chosen:** A nullable `notified_at TIMESTAMPTZ` on `ticket_sync`, added by migration `0002`. The migration marks tickets that already exist as notified, so deploying it does not send a burst of old notifications.
- **Not chosen:** Relying on arq's job-id de-duplication alone, which is lost in that crash window. A separate notification table would be more general than one flag needs to be.

### `db/schema.sql` stays the full current schema; migration 0001 is frozen
- **Chosen:** `db/schema.sql` now includes `notified_at` and remains the readable, authoritative description of the current schema. Migration `0001` runs frozen copies of the original scripts from `migrations/sql/`, so editing `db/schema.sql` never changes history. A test builds a database straight from `db/schema.sql` and compares it with the ORM, and another compares the ORM with the migrated database. Together they keep the file, the migrations and the models in agreement.
- **Not chosen:** Editing `db/schema.sql` and letting `0001` keep reading it. Fresh and upgraded databases would then reach the same column by different paths, and `0002` would fail on fresh ones.

### Notifications are at-least-once, keyed by `notified_at`
- **Chosen:** `notify_ticket` locks the `ticket_sync` row, skips if `notified_at` is set, posts to Slack and sets `notified_at` in the same transaction. Concurrent jobs cannot both post. A crash after Slack accepted the message but before the commit repeats it once. Each sync run returns tickets that still have no notification, and the worker re-enqueues them with the fixed job id `notify:<record_id>`. arq ignores an enqueue while such a job is queued, running or its result is kept (one hour), so a misconfigured channel is retried hourly instead of on every poll.
- **Not chosen:** Sending from inside the push transaction. A slow or failing Slack call would then hold up or fail ticket creation.

### One channel message that mentions the assignee
- **Chosen:** New tickets are posted to `default_channel` with an `@mention` of the assignee's Slack user (role → user from `slack.yaml`). The team sees the intake, the assignee is notified, and the app only needs `chat:write`. Without an assignee (AI unavailable and no rule matched), the message says the ticket needs manual triage.
- **Not chosen:** Direct messages, which hide the intake from the team and need more scopes.

### Everything from users or the LLM is escaped for Slack
- **Chosen:** `&`, `<` and `>` are escaped in titles, reporter names, AI summaries and rule notes. Otherwise a request titled `<!channel>` would ping the whole channel, and `<url|text>` could disguise a link. Verified live with a title containing `<!channel>`.

### Assignment rules: first match wins, AI judgement stored unchanged
- **Chosen:** `config/assignment.yaml` holds ordered rules. Conditions are the AI's category, urgency or team, keywords in the request, and the reporter. Actions are `assign` (team) and `set_urgency`. The first matching rule decides. Rules only change the ticket's priority and assignee; `ai_judgement` keeps the model's own answer, so AI accuracy can still be measured against human corrections. Keyword and reporter rules also work when there is no AI judgement, which gives a deterministic fallback during an LLM outage. Every value a rule mentions is checked against the database at startup, so a typo cannot silently disable a rule.
- **Not chosen:** Applying all matching rules in sequence. More expressive, but the result of overlapping rules becomes hard to predict and to explain.

### A rule's effect is explained in a Jira comment, only when it changed something
- **Chosen:** When a rule changes the team or urgency, a comment such as `Rule 'Month-end close is at least high': urgency low -> high` is added right after creation. It is a comment rather than part of the description because it records a decision made once at creation, while the description is rebuilt on every push. The comment is best effort: if it fails, the push still succeeds. A rule that matches but agrees with the AI writes nothing; the live test showed such "no change" comments are just noise.
- **Caveat:** The notification job re-evaluates the rules to name the team. If the request or the rules change in the seconds between creation and notification, the message can differ from what the ticket got.

## Milestone 6 — Jira webhooks: ticket changes back to the database and sheet

### A dedicated Atlassian service account, authenticated with OAuth 2.0
- **Context:** Loop protection ignores changes made by the system's own account. While that account was a person's, that person's manual edits in Jira were ignored too.
- **Chosen:** An Atlassian service account (free, not counted as a user) with an OAuth 2.0 client-credentials credential. The client exchanges id and secret at auth.atlassian.com for a 60-minute token, caches it, refreshes it two minutes early, and retries once after a 401. Service-account calls must go through the api.atlassian.com gateway for the site's cloud id. The site URL stays separate and is used only for links people open. `JIRA_AUTH=basic` (email + API token) remains for regular accounts.
- **Not chosen:** A second human account, which takes a user seat and uses a long-lived token. A scoped API token for the service account was also possible; OAuth avoids yearly token expiry, and its access tokens are short-lived.

### Names in `jira.yaml` are the service account's; webhook statuses are matched by id
- **Context:** Found live: the service account sees English names ("Task", "In Progress") while the site and its admin use Chinese ones. Webhook payloads are not guaranteed to use the service account's language.
- **Chosen:** `jira.yaml` uses the names the service account sees, and `jira-check` verifies them. Webhook statuses are resolved by **id**: the backend loads the project's statuses once as the service account, maps id → name → internal status, and reloads (at most once a minute) when an unknown id appears.
- **Not chosen:** Matching webhook status names directly. It would break silently as soon as the payload language differed.

### The webhook endpoint verifies, filters and queues; the worker applies
- **Chosen:** `POST /webhooks/jira` checks the HMAC-SHA256 `X-Hub-Signature` over the raw body (constant-time), caps the body at 2 MB, drops unsupported events and events by the service account, and queues a slimmed payload (key, status, priority, assignee, comment). Jira gets a fast 202 and has no reason to retry. Parsing and database work happen in the `ticket_event` job, which can retry.
- **Not chosen:** Applying events inside the request. A slow database or a status lookup in Jira would make Jira time out and re-send.

### Events carry full state; ordering uses `ticket_updated_at`
- **Chosen:** For "issue updated" events the system applies the issue's full status, priority and assignee from the payload, not the changelog. Events no newer than `ticket_sync.ticket_updated_at` are dropped. Because every event carries the complete state, dropping an older event that arrives late loses nothing: the newer one already contained its change. Values equal to the database write nothing. Comment events do not move the watermark (a comment could otherwise hide an older status event). They are de-duplicated by Jira comment id instead, so redelivered webhooks do not duplicate history.
- **Not chosen:** Applying changelog items one by one. That needs strict ordering, which webhooks do not guarantee.

### What is recorded
- **Chosen:**
  - A status change writes a `status_changed` event (from/to, the actor's display name, the Jira status name).
  - Priority and assignee are snapshot fields on the record (`current_priority`, `current_assignee`, display names for people); the schema has no event type for them.
  - Comments become `comment` events with plain text (ADF or wiki markup reduced to text) and the author.
  - Comment edits and deletions are not synced: the history is append-only.
  - A Jira status without a mapping (e.g. "In Review") leaves the internal status unchanged, logs a warning, and still updates priority and assignee. `jira-check` now lists such statuses.

### A baseline is read right after creation
- **Chosen:** After creating (or linking) a ticket, the push reads it back once to set the record's status, priority, assignee and the `ticket_updated_at` watermark. The first status is recorded without a `status_changed` event, because nobody changed anything. Best effort: if the read fails, the push still succeeds and the next event fills the fields in.

### The sheet is written in one place
- **Chosen:** Applying an event updates the database only, then enqueues `sync_document` for that worksheet when something visible in the sheet changed. The sync run already reads the sheet under the document lock, so it writes Status and Latest note at the rows' current positions, together with the detail link, and records `sheet_written_hash`. The same step also restores system cells edited by hand.
- **Not chosen:** Writing cells straight from the event job. It would need to find the row itself, outside the document lock, and could race with a sync run moving rows.
- **Display:**
  - Status shows the internal value in readable form ("In progress"), independent of Jira's language.
  - Latest note is the newest comment on one line: `2026-11-17 Olga Ops: Fixed in 1.4`.

### Status lookups are serialized
- **Context:** Found live: a single drag to "Done" produced two webhooks a fraction of a second apart. While one job was loading the status list, the other skipped the load (cool-down) and fell back to the payload's status name. That worked only because the name happened to be English.
- **Chosen:** Loading the project's statuses is guarded by a lock. Concurrent events wait for the one load in flight, then resolve by id. The cool-down starts only after a load succeeds. A regression test sends two concurrent events with Chinese status names.
- **Also seen live:** The older of the two events was processed second and dropped as stale, as designed.

### Public exposure through a Cloudflare Tunnel
- **Chosen:** A named tunnel on a dedicated hostname that routes only `/webhooks/(sheets|jira)` to the api. Everything else (`/health`, `/docs`, later the dashboard) answers 404 from outside. A WAF rule blocks non-POST requests to the webhook paths. Authentication stays in the application (HMAC for Jira, shared secret for Sheets), so the tunnel never relies on a URL staying secret.
- **Found live:** Cloudflare's Bot Fight Mode blocks Jira's webhook deliveries, which come from Atlassian's servers as automated traffic. On the free plan it cannot be skipped per path, so it is turned off for the zone. Deliveries rejected that way are not retried by Jira. Missed changes are recovered by the reconciliation in milestone 7.

### API docs off by default
- **Chosen:** `/docs` and `/openapi.json` are served only with `ENABLE_API_DOCS=true`. Once webhooks are reachable from the internet, an endpoint map is information an attacker does not need.
