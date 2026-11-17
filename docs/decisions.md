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
