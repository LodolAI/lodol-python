from __future__ import annotations

from typing import Any, Optional


class LodolError(Exception):
    """Base exception for the Lodol SDK."""


class ConfigurationError(LodolError):
    """Raised when client configuration is missing or invalid."""


class LodolTimeoutError(LodolError):
    """Raised when an SDK polling operation times out."""


class APIError(LodolError):
    """Base class for Lodol API and transport errors."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class APIConnectionError(APIError):
    """Raised when a request cannot be sent or completed."""


class APITimeoutError(APIConnectionError):
    """Raised when a request times out."""


class APIStatusError(APIError):
    """Raised for non-2xx responses from the Lodol API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        response: Any = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        self.body = body
        self.request_id = None
        if response is not None:
            self.request_id = getattr(response, "headers", {}).get("X-Request-Id")


class APIResponseValidationError(APIStatusError):
    """Raised when a successful API response has an unexpected shape."""


class BadRequestError(APIStatusError):
    """Raised for 400 responses."""


class AuthenticationError(APIStatusError):
    """Raised for 401 responses."""


class PaymentRequiredError(APIStatusError):
    """Raised for 402 responses."""


class PermissionDeniedError(APIStatusError):
    """Raised for 403 responses."""


class NotFoundError(APIStatusError):
    """Raised for 404 responses."""


class ConflictError(APIStatusError):
    """Raised for 409 responses, often idempotency conflicts."""


class UnprocessableEntityError(APIStatusError):
    """Raised for 422 responses."""


class RateLimitError(APIStatusError):
    """Raised for 429 responses."""

    def __init__(
        self,
        message: str,
        *,
        retry_after: Optional[str] = None,
        status_code: int,
        response: Any = None,
        body: Any = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            response=response,
            body=body,
        )
        self.retry_after = retry_after


class InternalServerError(APIStatusError):
    """Raised for 5xx responses."""
