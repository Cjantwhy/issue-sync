class SheetsError(Exception):
    """A Google Sheets call failed. `retryable` tells the job runner whether retrying helps."""

    def __init__(
        self, message: str, *, retryable: bool = False, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


class SheetLayoutError(Exception):
    """The worksheet's header row does not match the document's mapping."""
