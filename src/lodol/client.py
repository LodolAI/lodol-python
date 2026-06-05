from __future__ import annotations

import os
import time
import uuid
import warnings
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

import requests

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
from lodol.models import Execution, Workflow

DEFAULT_BASE_URL = "https://api-prod.lodol.com/api/v1"
TERMINAL_STATUSES = {"success", "failed", "stopped"}
USER_AGENT = "lodol-python/0.1.0"


class Lodol:
    """Client for the Lodol Developer API.

    The public shape intentionally mirrors common Python SDKs like OpenAI's:
    instantiate ``Lodol()`` once, then use resource namespaces such as
    ``client.workflows.run(...)`` and ``client.executions.retrieve(...)``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        session: Optional[requests.Session] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        resolved_api_key = api_key or os.environ.get("LODOL_API_KEY")
        if resolved_api_key is None:
            legacy_key = os.environ.get("SKIPFLOW_API_KEY")
            if legacy_key is not None:
                warnings.warn(
                    "SKIPFLOW_API_KEY is deprecated for the Lodol SDK; use "
                    "LODOL_API_KEY instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )
                resolved_api_key = legacy_key

        if not resolved_api_key:
            raise ConfigurationError(
                "Missing API key. Pass api_key=... or set LODOL_API_KEY."
            )
        if timeout <= 0:
            raise ConfigurationError("timeout must be greater than 0")
        if max_retries < 0:
            raise ConfigurationError("max_retries must be non-negative")

        self.api_key = resolved_api_key
        self.base_url = _resolve_base_url(base_url)
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
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
        max_retries: Optional[int] = None,
        default_headers: Optional[Mapping[str, str]] = None,
    ) -> "Lodol":
        """Return a new client with selected options changed."""
        headers = dict(self.default_headers)
        if default_headers:
            headers.update(default_headers)
        return Lodol(
            api_key=api_key or self.api_key,
            base_url=base_url or self.base_url,
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
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        """Make a low-level Developer API request.

        ``path`` is relative to ``base_url`` and should usually start with ``/``.
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
        params: Optional[Mapping[str, Any]] = None,
        json: Any = None,
        headers: Optional[Mapping[str, str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> Any:
        method = method.upper()
        url = _join_url(self.base_url, path)
        request_headers = self._build_headers(headers, idempotency_key)
        last_error: Optional[Exception] = None

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
        headers: Optional[Mapping[str, str]],
        idempotency_key: Optional[str],
    ) -> Dict[str, str]:
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
        items = data.get("workflows", []) if isinstance(data, dict) else []
        return [Workflow.from_api(item, client=self._client) for item in items]

    def retrieve(self, workflow_id: str) -> Workflow:
        data = self._client._request("GET", f"/workflows/{_path_id(workflow_id)}")
        return Workflow.from_api(data, client=self._client)

    def get(self, workflow_id: str) -> Workflow:
        return self.retrieve(workflow_id)

    def run(
        self,
        workflow_id: str,
        *,
        idempotency_key: Optional[str] = None,
        wait: bool = False,
        poll_interval: float = 2.0,
        timeout: Optional[float] = None,
        include_step_results: bool = False,
    ) -> Execution:
        data = self._client._request(
            "POST",
            f"/workflows/{_path_id(workflow_id)}/run-async",
            idempotency_key=idempotency_key or _new_idempotency_key("workflow-run"),
        )
        execution = Execution.from_api(data, client=self._client)
        if wait:
            return execution.wait(
                poll_interval=poll_interval,
                timeout=timeout,
                include_step_results=include_step_results,
            )
        return execution

    def run_async(self, workflow_id: str, **kwargs: Any) -> Execution:
        return self.run(workflow_id, **kwargs)


class ExecutionsResource:
    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self,
        *,
        workflow_id: Optional[str] = None,
        limit: int = 20,
        after: Optional[str] = None,
    ) -> list[Execution]:
        params: Dict[str, Any] = {"limit": limit}
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        if after is not None:
            params["after"] = after
        data = self._client._request("GET", "/executions", params=params)
        items = data.get("executions", []) if isinstance(data, dict) else []
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
        return Execution.from_api(data, client=self._client)

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
        idempotency_key: Optional[str] = None,
    ) -> Execution:
        data = self._client._request(
            "POST",
            f"/executions/{_path_id(execution_id)}/stop",
            idempotency_key=idempotency_key or _new_idempotency_key("execution-stop"),
        )
        return Execution.from_api(data, client=self._client)

    def wait(
        self,
        execution_id: str,
        *,
        poll_interval: float = 2.0,
        timeout: Optional[float] = None,
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


def _resolve_base_url(base_url: Optional[str]) -> str:
    raw = (
        base_url
        or os.environ.get("LODOL_BASE_URL")
        or os.environ.get("LODOL_API_BASE_URL")
        or DEFAULT_BASE_URL
    )
    return raw.rstrip("/")


def _join_url(base_url: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{base_url}/{path.lstrip('/')}"


def _path_id(value: str) -> str:
    return quote(value, safe="")


def _new_idempotency_key(prefix: str) -> str:
    return f"lodol-{prefix}-{uuid.uuid4()}"


def _can_retry(method: str, idempotency_key: Optional[str]) -> bool:
    return method in {"GET", "HEAD", "OPTIONS"} or bool(idempotency_key)


def _should_retry_response(status_code: int) -> bool:
    return status_code in {408, 409, 429} or status_code >= 500


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
    return min(8.0, 0.5 * (2.0**attempt))


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

