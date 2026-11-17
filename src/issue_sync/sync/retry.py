"""Retry and scheduling arithmetic shared by the arq jobs."""

BACKOFF_BASE_SECONDS = 10.0
BACKOFF_CAP_SECONDS = 900.0


def backoff_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Delay before the next attempt: 10s, 20s, 40s ... capped at 15 minutes.

    The first delay is deliberately not shorter: Jira's search index lags a few seconds,
    and a retried create first searches for a ticket the timed-out attempt may have made.
    A server-provided Retry-After is honoured when it asks for longer.
    """
    delay = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0))
    if retry_after:
        delay = max(delay, min(retry_after, BACKOFF_CAP_SECONDS))
    return delay


def poll_schedule(interval_seconds: int) -> dict[str, set[int] | int]:
    """arq cron fields for running every `interval_seconds` (validated in Settings)."""
    if interval_seconds < 60:
        return {"second": set(range(0, 60, interval_seconds))}
    return {"minute": set(range(0, 60, interval_seconds // 60)), "second": 0}


def retry_delay(
    *, retryable: bool, attempt: int, max_attempts: int, retry_after: float | None = None
) -> float | None:
    """Seconds until the next attempt, or None to give up."""
    if not retryable or attempt >= max_attempts:
        return None
    return backoff_seconds(attempt, retry_after)
