# One image for api, worker and migrate; compose picks the command.

FROM python:3.12-slim AS builder
# uv from PyPI (pinned): avoids depending on a second registry (ghcr.io) at build time.
RUN pip install --no-cache-dir uv==0.10.6
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app

# Dependencies first so code changes do not invalidate this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable


FROM python:3.12-slim AS runtime
RUN useradd --create-home --uid 1000 app
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations ./migrations
COPY db ./db
# Real adapter configs are mounted at runtime (see docker-compose.yml), never baked in.
COPY config/*.example.yaml ./config/
USER app
EXPOSE 8000
CMD ["uvicorn", "issue_sync.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
