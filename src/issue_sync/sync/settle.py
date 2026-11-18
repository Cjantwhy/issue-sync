"""When is a new sheet row ready to become a record?

People fill a row one cell at a time, and every cell edit triggers a sync. A record is
created once, and its AI judgement is made once, from the row as it is at that moment.
So a new row (one without a System ID) is taken only when it is

- complete: every required field that the worksheet has a column for is filled in, and
- settled: its content has not changed for a while, or it came from a form submission,
  which writes the whole row at once.

Settling is tracked per row content (document + hash of the row without system-owned
columns), not per row number: editing a cell restarts the wait, sorting or inserting
rows does not. The first-seen times live in Redis; losing them only means a row waits
one more period.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis

from issue_sync.sources.sheets.layout import SheetRow

KEY_PREFIX = "issue-sync:new-row"
# Long enough for any settle period; short enough that abandoned rows do not pile up.
KEY_TTL_SECONDS = 24 * 3600
# Marks content that arrived through a form submission: settled from the start.
SUBMITTED = "0"


class RowClock(Protocol):
    async def first_seen(self, document_id: int, raw_hash: str, now: datetime) -> datetime:
        """When this content was first seen; records `now` if it is new."""
        ...

    async def mark_submitted(self, document_id: int, raw_hash: str) -> None:
        """This content was written in one go (a form submission): no need to wait."""
        ...


class RedisRowClock:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def _key(document_id: int, raw_hash: str) -> str:
        return f"{KEY_PREFIX}:{document_id}:{raw_hash}"

    async def first_seen(self, document_id: int, raw_hash: str, now: datetime) -> datetime:
        key = self._key(document_id, raw_hash)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.set(key, str(now.timestamp()), nx=True, ex=KEY_TTL_SECONDS)
            pipe.get(key)
            _, value = await pipe.execute()
        return datetime.fromtimestamp(float(value), UTC)

    async def mark_submitted(self, document_id: int, raw_hash: str) -> None:
        await self._redis.set(self._key(document_id, raw_hash), SUBMITTED, ex=KEY_TTL_SECONDS)


@dataclass(frozen=True)
class NewRowPolicy:
    """Defaults take every row at once (the behaviour before this policy existed)."""

    required_fields: tuple[str, ...] = ()
    settle: timedelta = timedelta(0)
    clock: RowClock | None = None

    def __post_init__(self) -> None:
        if self.settle > timedelta(0) and self.clock is None:
            raise ValueError("a settle period needs a RowClock")

    def missing(self, row: SheetRow, columns: dict[str, int]) -> list[str]:
        """Required fields left blank. A field the worksheet has no column for is not
        required there: it could never be filled in."""
        return [f for f in self.required_fields if f in columns and not row.fields.get(f)]


def waiting_note(missing: list[str]) -> str:
    """Status cell text for an incomplete new row: "Waiting for: Title, Description"."""
    return "Waiting for: " + ", ".join(name.replace("_", " ").capitalize() for name in missing)
