from __future__ import annotations

from dataclasses import dataclass
import os
import random
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import quote, urlparse

import requests

from lodol.constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    DEFAULT_WAIT_TIMEOUT,
    POLL_INTERVAL_MAX_SECONDS,
    POLL_INTERVAL_MULTIPLIER,
    POLL_INTERVAL_START_SECONDS,
    POLL_JITTER_RATIO,
    RETRY_AFTER_MAX_SECONDS,
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
    ConcurrencyLimitError,
    ConflictError,
    ConfigurationError,
    ExecutionNeedsAttentionError,
    ExecutionTimeoutError,
    InternalServerError,
    NotFoundError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    TriggerWorkflowError,
    UnprocessableEntityError,
)
from lodol.models import Execution, Workflow

CONCURRENCY_LIMIT_CODE = "concurrency_limit"


@dataclass(frozen=True)
class RateLimit:
    """What the API last said about this workspace's request budget.

    Limits are per workspace and shared across all of its API keys, so this
    reflects everything the workspace is doing, not just this client.
    """

    limit: int | None = None
    remaining: int | None = None
    reset_seconds: float | None = None


@dataclass(frozen=True)
class _APIResponse:
    body: Any
    status_code: int


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
        base_url: str | None = None,
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
        self.base_url = _resolve_base_url(base_url)
        self.timeout = timeout
        self.max_retries = max_retries
        self.default_headers = dict(default_headers or {})
        # Updated from every response, so ``wait()`` can pace itself against
        # what the workspace has left rather than against a fixed interval.
        self.rate_limit = RateLimit()
        self._session = session or requests.Session()
        self._owns_session = session is None

        self.workflows = WorkflowsResource(self)
        self.executions = ExecutionsResource(self)

    def with_options(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
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
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Make a low-level Developer API request.

        ``path`` is relative to the client's ``base_url`` and should usually
        start with ``/``. This is useful for new API endpoints before the SDK
        grows a first-class resource method.
        """
        response = self._request_response(
            method,
            path,
            params=params,
            json=json,
            headers=headers,
            idempotency_key=idempotency_key,
        )
        return response.body

    def _request_response(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
    ) -> _APIResponse:
        method = method.upper()
        url = self._url_for_path(path)
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

            self.rate_limit = _rate_limit_from(response)

            if (
                _should_retry_response(response)
                and attempt < self.max_retries
                and _can_retry(method, idempotency_key)
            ):
                time.sleep(_retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                raise _error_from_response(response)

            if not getattr(response, "content", b""):
                return _APIResponse(body={}, status_code=response.status_code)
            try:
                return _APIResponse(
                    body=response.json(),
                    status_code=response.status_code,
                )
            except ValueError as exc:
                raise APIResponseValidationError(
                    "Lodol API returned invalid JSON",
                    status_code=response.status_code,
                    response=response,
                    body=getattr(response, "text", None),
                    request_id=_request_id_of(response),
                ) from exc

        if last_error is not None:
            raise APIConnectionError(f"Request failed: {last_error}") from last_error
        raise APIError("Request failed after retries")

    def _poll_delay(self, interval: float) -> float:
        """How long to wait before the next poll.

        Jittered so runs started together don't poll in lockstep, and stretched
        to the reset when the workspace's per-minute budget is nearly spent —
        polling into a rate limit would only make the wait longer.
        """
        delay = interval * (1.0 + random.uniform(0.0, POLL_JITTER_RATIO))
        budget = self.rate_limit
        if (
            budget.remaining is not None
            and budget.remaining <= 1
            and budget.reset_seconds is not None
        ):
            return max(delay, budget.reset_seconds)
        return delay

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

    def _url_for_path(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"


class WorkflowsResource:
    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(self) -> list[Workflow]:
        """Every workflow in the workspace this API key belongs to."""
        response = self._client._request_response("GET", "/workflows")
        body = _expect_dict(response.body, "GET /workflows", response.status_code)
        items = _expect_list(
            body.get("workflows"),
            "GET /workflows field 'workflows'",
            response.status_code,
        )
        return [Workflow.from_api(item, client=self._client) for item in items]

    def retrieve(self, workflow_id: str) -> Workflow:
        """One workflow, including the values it asks for and gives back."""
        response = self._client._request_response(
            "GET",
            f"/workflows/{_path_id(workflow_id)}",
        )
        body = _expect_dict(
            response.body,
            "GET /workflows/{workflow_id}",
            response.status_code,
        )
        return Workflow.from_api(body, client=self._client)

    def get(self, workflow_id: str) -> Workflow:
        return self.retrieve(workflow_id)

    def run(
        self,
        workflow_id: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        wait: bool = False,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT,
    ) -> Execution:
        """Start a workflow, optionally waiting for it to finish.

        *inputs* are the values the workflow declares it needs, keyed by name —
        see ``workflow.inputs``. Naming one the workflow does not ask for, or
        leaving out one it requires, raises :class:`BadRequestError` naming the
        input rather than starting a run without it.

        With *wait*, returns once the run has finished and ``outputs`` holds
        what it produced. See ``client.executions.wait`` for what happens when
        a run does not finish.
        """
        body = {"inputs": dict(inputs)} if inputs else None
        response = self._client._request_response(
            "POST",
            f"/workflows/{_path_id(workflow_id)}/run-async",
            json=body,
            idempotency_key=idempotency_key or _new_idempotency_key("workflow-run"),
        )
        payload = _expect_dict(
            response.body,
            "POST /workflows/{workflow_id}/run-async",
            response.status_code,
        )
        execution = Execution.from_api(payload, client=self._client)
        if wait:
            return execution.wait(timeout=timeout)
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
        """Recent runs, newest first.

        *after* is the ``id`` of the last run from the previous page.
        """
        params: dict[str, Any] = {"limit": limit}
        if workflow_id is not None:
            params["workflow_id"] = workflow_id
        if after is not None:
            params["after"] = after
        response = self._client._request_response("GET", "/executions", params=params)
        body = _expect_dict(response.body, "GET /executions", response.status_code)
        items = _expect_list(
            body.get("executions"),
            "GET /executions field 'executions'",
            response.status_code,
        )
        return [Execution.from_api(item, client=self._client) for item in items]

    def retrieve(
        self,
        execution_id: str,
        *,
        include_step_results: bool = False,
    ) -> Execution:
        """One run as it stands now.

        ``outputs`` — what the workflow handed back — is always included. Pass
        *include_step_results* to also get per-step detail, which is for
        looking into how a run reached its result rather than reading it.
        """
        response = self._client._request_response(
            "GET",
            f"/executions/{_path_id(execution_id)}",
            params={"include_step_results": str(include_step_results).lower()},
        )
        body = _expect_dict(
            response.body,
            "GET /executions/{execution_id}",
            response.status_code,
        )
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
        """Ask a run to stop. Steps that already finished are not undone."""
        response = self._client._request_response(
            "POST",
            f"/executions/{_path_id(execution_id)}/stop",
            idempotency_key=idempotency_key or _new_idempotency_key("execution-stop"),
        )
        body = _expect_dict(
            response.body,
            "POST /executions/{execution_id}/stop",
            response.status_code,
        )
        return Execution.from_api(body, client=self._client)

    def wait(
        self,
        execution_id: str,
        *,
        timeout: float | None = DEFAULT_WAIT_TIMEOUT,
    ) -> Execution:
        """Wait for a run to finish, then return it with its ``outputs``.

        Polling starts at a second and eases off to eight, so a short run comes
        back promptly and a long one costs the workspace's shared request
        budget about eight calls a minute.

        Two situations end the wait early rather than running it down to the
        timeout, because neither resolves on its own:

        * :class:`ExecutionNeedsAttentionError` — the run is parked until
          someone answers it in Lodol.
        * :class:`TriggerWorkflowError` — the workflow starts from its own
          trigger, so it runs once per event and has no single finish.

        Both, along with :class:`ExecutionTimeoutError`, are
        :class:`ExecutionNotFinishedError`; catch that to handle every way a
        wait can end without a finished run. Pass ``timeout=None`` to wait
        without a limit.
        """
        if timeout is not None and timeout < 0:
            raise ConfigurationError("timeout must be non-negative")

        deadline = None if timeout is None else time.monotonic() + timeout
        interval = POLL_INTERVAL_START_SECONDS
        while True:
            execution = self.retrieve(execution_id)
            if execution.is_terminal:
                return execution
            if execution.is_trigger_listener:
                name = execution.workflow_name or "This workflow"
                raise TriggerWorkflowError(
                    f"{name} starts from its own trigger, so it runs once per "
                    "event rather than finishing. List its runs with "
                    "client.executions.list(workflow_id=...).",
                    execution=execution,
                )
            if execution.needs_attention:
                raise ExecutionNeedsAttentionError(
                    f"Run {execution_id} is waiting for someone to respond in "
                    f"Lodol (status: {execution.status}). It stays where it is "
                    "until they do.",
                    execution=execution,
                )

            sleep_for = self._client._poll_delay(interval)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ExecutionTimeoutError(
                        f"Run {execution_id} did not finish within "
                        f"{timeout} seconds. It is still going; read it later "
                        "with client.executions.retrieve().",
                        execution=execution,
                    )
                sleep_for = min(sleep_for, remaining)
            time.sleep(sleep_for)
            interval = min(
                POLL_INTERVAL_MAX_SECONDS, interval * POLL_INTERVAL_MULTIPLIER
            )


def _resolve_base_url(base_url: str | None) -> str:
    """The API to talk to: the argument, then ``LODOL_BASE_URL``, then Lodol."""
    resolved = (
        base_url or os.environ.get("LODOL_BASE_URL") or DEFAULT_BASE_URL
    ).strip()
    resolved = resolved.rstrip("/")
    parsed = urlparse(resolved)
    if parsed.scheme == "https":
        return resolved
    # Plain HTTP only against your own machine, where there is no network to
    # leak the API key onto. Anything else has to be HTTPS.
    if parsed.scheme == "http" and (parsed.hostname or "") in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        return resolved
    raise ConfigurationError(
        f"base_url must use HTTPS (got {resolved!r}). "
        "Plain HTTP is only allowed for localhost."
    )


def _path_id(value: str) -> str:
    return quote(value, safe="")


def _new_idempotency_key(prefix: str) -> str:
    return f"lodol-{prefix}-{uuid.uuid4()}"


def _can_retry(method: str, idempotency_key: str | None) -> bool:
    return method in {"GET", "HEAD", "OPTIONS"} or bool(idempotency_key)


def _should_retry_response(response: requests.Response) -> bool:
    """Whether coming back with the same request could plausibly succeed.

    409 is included: it is how the API says an identical request is still in
    flight, and it asks for a retry with a ``Retry-After``.
    """
    status = response.status_code
    if status in {408, 409} or status >= 500:
        return True
    if status == 429:
        # A concurrency limit is not cleared by waiting a moment — a running
        # workflow has to finish first — so retrying only spends rate-limit
        # budget. Raise it and let the caller decide.
        return _error_code_of(response) != CONCURRENCY_LIMIT_CODE
    return False


def _retry_delay(response: requests.Response, attempt: int) -> float:
    retry_after = _retry_after_seconds(response)
    if retry_after is not None:
        return retry_after
    return _backoff_seconds(attempt)


def _retry_after_seconds(response: requests.Response) -> float | None:
    """The wait the server asked for, as seconds, capped.

    Accepts both forms the header takes — a number of seconds and an HTTP
    date. Capped because honouring an unbounded value would park a synchronous
    call for that long with nothing to show the caller.
    """
    raw = getattr(response, "headers", {}).get("Retry-After")
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(raw)
            seconds = retry_at.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return None
    return max(0.0, min(seconds, RETRY_AFTER_MAX_SECONDS))


def _backoff_seconds(attempt: int) -> float:
    return min(
        RETRY_BACKOFF_MAX_SECONDS,
        RETRY_BACKOFF_BASE_SECONDS * (RETRY_BACKOFF_MULTIPLIER**attempt),
    )


def _rate_limit_from(response: requests.Response) -> RateLimit:
    headers = getattr(response, "headers", {}) or {}
    return RateLimit(
        limit=_optional_int(headers.get("X-RateLimit-Limit")),
        remaining=_optional_int(headers.get("X-RateLimit-Remaining")),
        reset_seconds=_optional_float(headers.get("X-RateLimit-Reset")),
    )


def _optional_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _optional_float(raw: Any) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _request_id_of(response: requests.Response) -> str | None:
    return getattr(response, "headers", {}).get("X-Request-Id")


def _error_code_of(response: requests.Response) -> str | None:
    """The API's stable code for a failure, when the body carries one."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        code = body.get("error_code")
        return str(code) if code else None
    return None


def _expect_dict(value: Any, context: str, status_code: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise APIResponseValidationError(
            f"{context} returned {type(value).__name__}, expected object",
            status_code=status_code,
            body=value,
        )
    return value


def _expect_list(value: Any, context: str, status_code: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise APIResponseValidationError(
            f"{context} returned {type(value).__name__}, expected list",
            status_code=status_code,
            body=value,
        )

    items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise APIResponseValidationError(
                f"{context}[{index}] returned {type(item).__name__}, expected object",
                status_code=status_code,
                body=item,
            )
        items.append(item)
    return items


def _error_from_response(response: requests.Response) -> APIStatusError:
    message = "Lodol API request failed"
    body: Any = None
    error_code: str | None = None
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
            code = body.get("error_code")
            error_code = str(code) if code else None
        elif body:
            message = str(body)

    kwargs: dict[str, Any] = {
        "status_code": response.status_code,
        "response": response,
        "body": body,
        "error_code": error_code,
        "request_id": _request_id_of(response),
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
        if error_code == CONCURRENCY_LIMIT_CODE:
            return ConcurrencyLimitError(message, **kwargs)
        return RateLimitError(
            message,
            retry_after=_retry_after_seconds(response),
            **kwargs,
        )
    if response.status_code >= 500:
        return InternalServerError(message, **kwargs)
    return APIStatusError(message, **kwargs)
