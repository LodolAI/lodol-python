from __future__ import annotations

from typing import Any


class LodolError(Exception):
    """Base exception for the Lodol SDK."""


class ConfigurationError(LodolError):
    """Raised when client configuration is missing or invalid."""


class ExecutionNotFinishedError(LodolError):
    """Raised when waiting on a run ends without a finished run.

    Catch this to handle every reason a wait can end early — it timed out, it
    is parked waiting for a person, or the workflow runs on its own triggers
    and has no single finish. The run itself is on ``execution``.
    """

    def __init__(self, message: str, *, execution: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.execution = execution


class ExecutionTimeoutError(ExecutionNotFinishedError):
    """Raised when a run has not finished within the time allowed.

    Distinct from :class:`APITimeoutError`, which is a single HTTP request
    timing out. This one means the workflow itself was still going.
    """


class ExecutionNeedsAttentionError(ExecutionNotFinishedError):
    """Raised when a run is parked waiting for a person.

    A workflow that pauses for review or asks for input stays parked until
    someone answers it in Lodol, so waiting for it to finish on its own would
    only ever end in a timeout.
    """


class TriggerWorkflowError(ExecutionNotFinishedError):
    """Raised when waiting on a workflow that starts from its own trigger.

    Such a workflow runs once per event it receives, so the run this call
    started is the listener rather than a single piece of work — there is no
    finish to wait for. Its runs are listed like any other.
    """


class APIError(LodolError):
    """Base class for Lodol API and transport errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class APIConnectionError(APIError):
    """Raised when a request cannot be sent or completed."""


class APITimeoutError(APIConnectionError):
    """Raised when a single HTTP request times out."""


class APIStatusError(APIError):
    """Raised for non-2xx responses from the Lodol API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response: Any = None,
        body: Any = None,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        self.body = body
        # A stable machine-readable code for the failure, when the API sends
        # one. Quote it in a support request alongside ``request_id``.
        self.error_code = error_code
        self.request_id = request_id

    def __str__(self) -> str:
        if self.request_id:
            return f"{self.message} (request_id: {self.request_id})"
        return self.message


class APIResponseValidationError(APIStatusError):
    """Raised when a successful API response has an unexpected shape."""


class BadRequestError(APIStatusError):
    """Raised for 400 responses.

    Among other things, this is how the API reports an input the workflow does
    not declare, a required input left out, and a workflow still in draft.
    """


class AuthenticationError(APIStatusError):
    """Raised for 401 responses."""


class PaymentRequiredError(APIStatusError):
    """Raised for 402 responses."""


class PermissionDeniedError(APIStatusError):
    """Raised for 403 responses, including an API key missing a scope."""


class NotFoundError(APIStatusError):
    """Raised for 404 responses."""


class ConflictError(APIStatusError):
    """Raised for 409 responses, often idempotency conflicts."""


class UnprocessableEntityError(APIStatusError):
    """Raised for 422 responses."""


class RateLimitError(APIStatusError):
    """Raised when the workspace has made too many requests this minute.

    Waiting clears it: ``retry_after`` is how long the API asked you to wait.
    Rate limits are per workspace and shared across all of its API keys.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int,
        response: Any = None,
        body: Any = None,
        error_code: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            response=response,
            body=body,
            error_code=error_code,
            request_id=request_id,
        )
        self.retry_after = retry_after


class ConcurrencyLimitError(APIStatusError):
    """Raised when the workspace already has as many runs going as its plan allows.

    Shares the 429 status with :class:`RateLimitError` but is a different
    problem with a different fix: waiting a moment does not clear it, a running
    workflow finishing does. Retrying immediately only burns rate-limit budget,
    so the SDK raises this rather than retrying it.
    """


class InternalServerError(APIStatusError):
    """Raised for 5xx responses."""
