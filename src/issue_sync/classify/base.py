"""Classifier interface.

The sync flow depends only on `Classifier`; the one implementation calls an
OpenAI-compatible chat completions API. Results use internal values (the names in the
constant tables), never anything provider-specific.
"""

from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, StringConstraints

SUMMARY_MAX_LENGTH = 500
REASON_MAX_LENGTH = 1000


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class ClassifierInput(_Model):
    """Only the request text is sent: reporter names and contact details stay private."""

    title: str | None
    description: str | None


class Judgement(_Model):
    type: str
    urgency: str
    assignee_role: str
    summary: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Classification(_Model):
    judgement: Judgement
    model: str  # the model that answered, as reported by the API


class ClassifierError(Exception):
    """Classification failed. `retryable` tells the job runner whether retrying can help."""

    def __init__(
        self, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class Classifier(Protocol):
    async def classify(self, request: ClassifierInput) -> Classification: ...
