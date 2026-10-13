"""Everything the SDK raises.

All of it derives from :class:`LodolError`, so ``except LodolError`` catches
anything the SDK raises on purpose. Errors from the API derive from
:class:`APIError`; those with an HTTP status derive from
:class:`APIStatusError`, which carries the status, the server's message and,
where the server sends one, a stable machine-readable ``code``.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    import httpx2

    from lodol.models import Run


class LodolError(Exception):
    """Base class for every error the SDK raises."""


class ConfigurationError(LodolError):
    """The client was set up with a missing or invalid setting."""


class WaitTimeoutError(LodolError, TimeoutError):
    """A run didn't finish within the ``timeout`` it was waited on for.

    The run itself is untouched and keeps going. ``run`` is the latest look
    the SDK had at it, or ``None`` if it never managed one. Being a
    ``TimeoutError``, it is also caught by ``except TimeoutError``.
    """

    def __init__(self, message: str, *, run: Run | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.run = run


class RunFailedError(LodolError):
    """A run that was waited for finished without succeeding: it failed, or
    was stopped.

    ``run`` is the run as it finished, with its step results, so
    ``run.error`` and the failed step say what went wrong. To handle a
    failure without an exception, catch this and use ``run``.
    """

    def __init__(self, message: str, *, run: Run) -> None:
        super().__init__(message)
        self.message = message
        self.run = run


class APIError(LodolError):
    """A request to the Lodol API failed."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class APIConnectionError(APIError):
    """The request couldn't reach Lodol, or the connection broke."""


class APITimeoutError(APIConnectionError):
    """The request got no answer in time (the client's ``timeout``)."""


class APIStatusError(APIError):
    """The API answered with an error status.

    ``message`` is the server's own explanation, ``code`` its stable
    machine-readable error code when it sends one (such as
    ``"monthly_run_limit"``), and ``body`` the parsed response body.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        body: Any = None,
        response: httpx2.Response | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.body = body
        self.response = response


class APIResponseValidationError(APIStatusError):
    """The API answered successfully, but not in the shape the SDK expects."""


class BadRequestError(APIStatusError):
    """400: the request was refused as it stands, such as inputs that don't fit."""


class AuthenticationError(APIStatusError):
    """401: the API key is missing, malformed, revoked or expired."""


class PaymentRequiredError(APIStatusError):
    """402: this copy of Lodol needs a licence (Lodol Local)."""


class PermissionDeniedError(APIStatusError):
    """403: the key's scopes, its creator's role or the workspace's network
    rules don't allow this."""


class NotFoundError(APIStatusError):
    """404: there is no such workflow, run or approval request in this workspace."""


class ConflictError(APIStatusError):
    """409: the request collides with the state of things, such as answering
    an approval request someone else already answered."""


class UnprocessableEntityError(APIStatusError):
    """422: an ``idempotency_key`` was reused for a different request."""


class RateLimitError(APIStatusError):
    """429: too many requests for now.

    ``retry_after`` is how many seconds the server asked to wait, when it said.
    The client already waits out short limits on its own (see ``max_retries``),
    so this is raised only when the wait would be long or retries ran out.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
        code: str | None = None,
        body: Any = None,
        response: httpx2.Response | None = None,
    ) -> None:
        super().__init__(
            message, status_code=status_code, code=code, body=body, response=response
        )
        self.retry_after = retry_after


class ConcurrencyLimitError(RateLimitError):
    """429: the workspace is already running as many runs at once as its plan
    allows. Wait for one to finish (or stop one), then start another."""


class RunLimitError(RateLimitError):
    """429: the workspace has used every run its plan includes this month.

    ``resets_at`` is when runs become available again, and ``limit`` how many
    runs a month the plan includes. Retrying before then won't help.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after: float | None = None,
        resets_at: dt.datetime | None = None,
        limit: int | None = None,
        code: str | None = None,
        body: Any = None,
        response: httpx2.Response | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            retry_after=retry_after,
            code=code,
            body=body,
            response=response,
        )
        self.resets_at = resets_at
        self.limit = limit


class InternalServerError(APIStatusError):
    """5xx: something went wrong on Lodol's side. The client already retried."""
