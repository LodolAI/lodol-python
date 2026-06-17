from __future__ import annotations

import os
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import requests

import lodol.constants as constants
from lodol.constants import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_TIMEOUT,
    RETRY_BACKOFF_BASE_SECONDS,
    RETRY_BACKOFF_MAX_SECONDS,
    RETRY_BACKOFF_MULTIPLIER,
    USER_AGENT,
)
from lodol.exceptions import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    ConfigurationError,
    InternalServerError,
    LodolTimeoutError,
    NotFoundError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from lodol.models import Execution, Workflow, TERMINAL_STATUSES


class Lodol:
    """Client for the Lodol Developer API.

    The public shape intentionally mirrors common Python SDKs like OpenAI's:
    instantiate ``Lodol()`` once, then use resource namespaces such as
    ``client.workflows.run(...)`` and ``client.executions.retrieve(...)``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        session: requests.Session | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> None:
        resolved_api_key = api_key or os.environ.get("LODOL_API_KEY")
        if not resolved_api_key:
            raise ConfigurationError(
                "Missing API key. Pass api_key=... or set LODOL_API_KEY."
            )
        if timeout <= 0:
            raise ConfigurationError("timeout must be greater than 0")
        if max_retries < 0:
            raise ConfigurationError("max_retries must be non-negative")

        self.api_key = resolved_api_key
        self.base_url = _default_base_url()
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        self._session = session or requests.Session()
        self._owns_session = session is None

        self.workflows = WorkflowsResource(self)
        self.executions = ExecutionsResource(self)

    def with_options(
        self,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        max_retries: int | None = None,
        default_headers: Mapping[str, str] | None = None,
    ) -> "Lodol":
        """Return a new client with selected options changed.

        The returned client allocates a new ``requests.Session``. It does not
        share the original client's connection pool, even if the original
        client was created with a custom session.
        """
        headers = dict(self.default_headers)
        if default_headers:
            headers.update(default_headers)
        return Lodol(
            api_key=api_key or self.api_key,
            timeout=self.timeout if timeout is None else timeout,
            max_retries=self.max_retries if max_retries is None else max_retries,
            default_headers=headers,
        )

    def close(self) -> None:
        """Close the underlying HTTP session if the SDK created it."""
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "Lodol":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Make a low-level Developer API request.

        ``path`` is relative to ``DEFAULT_BASE_URL`` and should usually start with ``/``.
        This is useful for new API endpoints before the SDK grows a first-class
        resource method.
        """
        return self._request(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            idempotency_key=idempotency_key,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        method = method.upper()
        url = _url_for_path(path)
        request_headers = self._build_headers(headers, idempotency_key)
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=request_headers,
                    params=params,
                    json=json,
                    timeout=self.timeout,
                )
            except requests.Timeout as exc:
                last_error = exc
                if attempt < self.max_retries and _can_retry(method, idempotency_key):
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise APITimeoutError(f"Request timed out: {exc}") from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.max_retries and _can_retry(method, idempotency_key):
                    time.sleep(_backoff_seconds(attempt))
                    continue
                raise APIConnectionError(f"Request failed: {exc}") from exc

            if (
                _should_retry_response(response.status_code)
                and attempt < self.max_retries
                and _can_retry(method, idempotency_key)
            ):
                time.sleep(_retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                raise _error_from_response(response)

            if not getattr(response, "content", b""):
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise APIResponseValidationError(
                    "Lodol API returned invalid JSON",
                    status_code=response.status_code,
                    response=response,
                    body=getattr(response, "text", None),
                ) from exc

        if last_error is not None:
            raise APIConnectionError(f"Request failed: {last_error}") from last_error
        raise APIError("Request failed after retries")

    def _build_headers(
        self,
        headers: Mapping[str, str] | None,
        idempotency_key: str | None,
    ) -> dict[str, str]:
        merged = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        merged.update(self.default_headers)
        if headers:
            merged.update(headers)
        if idempotency_key:
            merged["Idempotency-Key"] = idempotency_key
        return merged


class WorkflowsResource:
    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(self) -> list[Workflow]:
        data = self._client._request("GET", "/workflows")
        body = _expect_dict(data, "GET /workflows")
        items = _expect_list(body.get("workflows"), "GET /workflows field 'workflows'")
        return [Workflow.from_api(item, client=self._client) for item in items]

    def retrieve(self, workflow_id: str) -> Workflow:
        data = self._client._request("GET", f"/workflows/{_path_id(workflow_id)}")
        body = _expect_dict(data, "GET /workflows/{workflow_id}")
        return Workflow.from_api(body, client=self._client)

    def get(self, workflow_id: str) -> Workflow:
        return self.retrieve(workflow_id)

    def run(
        self,
        workflow_id: str,
        *,
        idempotency_key: str | None = None,
        wait: bool = False,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float | None = None,
        include_step_results: bool = False,
    ) -> Execution:
        data = self._client._request(
            "POST",
            f"/workflows/{_path_id(workflow_id)}/run-async",
            idempotency_key=idempotency_key or _new_idempotency_key("workflow-run"),
        )
        body = _expect_dict(data, "POST /workflows/{workflow_id}/run-async")
        execution = Execution.from_api(body, client=self._client)
        if wait:
            return execution.wait(
                poll_interval=poll_interval,
                timeout=timeout,
                include_step_results=include_step_results,
            )
        return execution

class ExecutionsResource:
    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self,
        *,
        workflow_id: str | None = None,
        limit: int = 20,
        after: str | None = None,
    ) -> list[Execution]:
        params: dict[str, Any] = {"limit": limit}
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        if after is not None:
            params["after"] = after
        data = self._client._request("GET", "/executions", params=params)
        body = _expect_dict(data, "GET /executions")
        items = _expect_list(body.get("executions"), "GET /executions field 'executions'")
        return [Execution.from_api(item, client=self._client) for item in items]

    def retrieve(
        self,
        execution_id: str,
        *,
        include_step_results: bool = False,
    ) -> Execution:
        data = self._client._request(
            "GET",
            f"/executions/{_path_id(execution_id)}",
            params={"include_step_results": str(include_step_results).lower()},
        )
        body = _expect_dict(data, "GET /executions/{execution_id}")
        return Execution.from_api(body, client=self._client)

    def get(
        self,
        execution_id: str,
        *,
        include_step_results: bool = False,
    ) -> Execution:
        return self.retrieve(execution_id, include_step_results=include_step_results)

    def stop(
        self,
        execution_id: str,
        *,
        idempotency_key: str | None = None,
    ) -> Execution:
        data = self._client._request(
            "POST",
            f"/executions/{_path_id(execution_id)}/stop",
            idempotency_key=idempotency_key or _new_idempotency_key("execution-stop"),
        )
        body = _expect_dict(data, "POST /executions/{execution_id}/stop")
        return Execution.from_api(body, client=self._client)

    def wait(
        self,
        execution_id: str,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float | None = None,
        include_step_results: bool = True,
    ) -> Execution:
        if poll_interval <= 0:
            raise ConfigurationError("poll_interval must be greater than 0")
        if timeout is not None and timeout < 0:
            raise ConfigurationError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            execution = self.retrieve(
                execution_id,
                include_step_results=include_step_results,
            )
            if execution.status in TERMINAL_STATUSES:
                return execution

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LodolTimeoutError(
                        f"Execution {execution_id} did not finish within {timeout} seconds"
                    )
                sleep_for = min(poll_interval, remaining)
            else:
                sleep_for = poll_interval
            time.sleep(sleep_for)


def _url_for_path(path: str) -> str:
    return f"{_default_base_url()}/{path.lstrip('/')}"


def _default_base_url() -> str:
    base_url = constants.DEFAULT_BASE_URL.rstrip("/")
    if urlparse(base_url).scheme != "https":
        raise ConfigurationError("DEFAULT_BASE_URL must use HTTPS")
    return base_url


def _path_id(value: str) -> str:
    return quote(value, safe="")


def _new_idempotency_key(prefix: str) -> str:
    return f"lodol-{prefix}-{uuid.uuid4()}"


def _can_retry(method: str, idempotency_key: str | None) -> bool:
    return method in {"GET", "HEAD", "OPTIONS"} or bool(idempotency_key)


def _should_retry_response(status_code: int) -> bool:
    return status_code in {408, 429} or status_code >= 500


def _retry_delay(response: requests.Response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                return max(0.0, retry_at.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return _backoff_seconds(attempt)


def _backoff_seconds(attempt: int) -> float:
    return min(
        RETRY_BACKOFF_MAX_SECONDS,
        RETRY_BACKOFF_BASE_SECONDS * (RETRY_BACKOFF_MULTIPLIER**attempt),
    )


def _expect_dict(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise APIResponseValidationError(
            f"{context} returned {type(value).__name__}, expected object",
            status_code=200,
            body=value,
        )
    return value


def _expect_list(value: Any, context: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise APIResponseValidationError(
            f"{context} returned {type(value).__name__}, expected list",
            status_code=200,
            body=value,
        )

    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise APIResponseValidationError(
                f"{context}[{index}] returned {type(item).__name__}, expected object",
                status_code=200,
                body=item,
            )
        items.append(item)
    return items


def _error_from_response(response: requests.Response) -> APIStatusError:
    message = "Lodol API request failed"
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        text = getattr(response, "text", "")
        if text:
            message = text
            body = text
    else:
        if isinstance(body, dict):
            error = body.get("error") or body.get("message")
            if error:
                message = str(error)
        elif body:
            message = str(body)

    kwargs = {
        "status_code": response.status_code,
        "response": response,
        "body": body,
    }
    if response.status_code == 400:
        return BadRequestError(message, **kwargs)
    if response.status_code == 401:
        return AuthenticationError(message, **kwargs)
    if response.status_code == 402:
        return PaymentRequiredError(message, **kwargs)
    if response.status_code == 403:
        return PermissionDeniedError(message, **kwargs)
    if response.status_code == 404:
        return NotFoundError(message, **kwargs)
    if response.status_code == 409:
        return ConflictError(message, **kwargs)
    if response.status_code == 422:
        return UnprocessableEntityError(message, **kwargs)
    if response.status_code == 429:
        return RateLimitError(
            message,
            retry_after=response.headers.get("Retry-After"),
            **kwargs,
        )
    if response.status_code >= 500:
        return InternalServerError(message, **kwargs)
    return APIStatusError(message, **kwargs)
