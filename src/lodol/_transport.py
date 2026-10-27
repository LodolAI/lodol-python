"""How the SDK talks to the Developer API.

A call is one request: built, sent, and sent again when the network or the
server says that is worth doing and the request can't take effect twice. The
decisions are plain functions shared by the sync and async clients, so both
behave the same; only sending and sleeping differ.
"""

from __future__ import annotations

import datetime as dt
import decimal
import ipaddress
import json
import logging
import os
import random
import ssl
import time
import uuid
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Mapping, Sequence, TypeVar
from urllib.parse import quote, urlsplit

import anyio
import httpx2

from lodol import constants
from lodol.exceptions import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    InternalServerError,
    NotFoundError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    RunLimitError,
    UnprocessableEntityError,
)
from lodol.models import parse_datetime

logger = logging.getLogger("lodol")


# ── Time ────────────────────────────────────────────────────────────────
# Everything that waits goes through these, so tests can let time pass
# without anyone waiting for it.


def sleep(seconds: float) -> None:
    time.sleep(seconds)


async def async_sleep(seconds: float) -> None:
    await anyio.sleep(seconds)


def monotonic() -> float:
    return time.monotonic()


# ── Requests and responses ──────────────────────────────────────────────

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

QueryParams = Mapping[str, str | Sequence[str]]


@dataclass(frozen=True)
class APIRequest:
    method: str
    path: str
    params: QueryParams | None = None
    body: Any = None
    idempotency_key: str | None = None

    @property
    def resendable(self) -> bool:
        """Whether sending this again can't make anything happen twice: a
        read, or a POST the server deduplicates by its Idempotency-Key. (The
        API takes keys on POSTs only; a key on anything else protects
        nothing.)"""
        return self.method in _SAFE_METHODS or (
            self.method == "POST" and self.idempotency_key is not None
        )

    def describe(self) -> str:
        return f"{self.method} {self.path}"


@dataclass(frozen=True)
class APIResponse:
    body: Any
    status_code: int


@dataclass(frozen=True)
class RateLimit:
    """The workspace's request allowance, as the last response reported it."""

    limit: int
    """Requests a minute."""
    remaining: int
    reset: float
    """Seconds until the allowance starts again."""


def parse_rate_limit(headers: httpx2.Headers) -> RateLimit | None:
    try:
        limit = int(headers["x-ratelimit-limit"])
        remaining = int(headers["x-ratelimit-remaining"])
        reset = float(headers.get("x-ratelimit-reset") or 0)
    except (KeyError, ValueError):
        return None
    if limit <= 0:
        return None
    return RateLimit(limit=limit, remaining=max(0, remaining), reset=max(0.0, reset))


def new_idempotency_key(action: str) -> str:
    return f"lodol-{action}-{uuid.uuid4()}"


def idempotency_key_header(key: object) -> str:
    """A caller's idempotency key, as it can travel in a header.

    Any text works: a key with characters a header can't carry (accents,
    say) is percent-encoded, the same way every time, so the same key still
    names the same request.
    """
    if not isinstance(key, str):
        raise TypeError(f"idempotency_key must be text, not {type(key).__name__}")
    if not key.strip():
        raise ValueError("idempotency_key is empty")
    header = key
    if not all(" " <= char <= "~" for char in key):
        header = quote(
            key, safe="".join(chr(c) for c in range(0x20, 0x7F) if c != 0x25)
        )
    if len(header) > constants.MAX_IDEMPOTENCY_KEY_LENGTH:
        raise ValueError(
            f"idempotency_key is {len(header)} characters; Lodol takes at most "
            f"{constants.MAX_IDEMPOTENCY_KEY_LENGTH}"
        )
    return header


def path_id(value: str) -> str:
    return quote(value, safe="")


def encode_json(value: Any) -> bytes:
    # Keys sorted: Lodol tells a retry from a new request by its exact bytes,
    # so the same inputs must always be sent the same way.
    return json.dumps(
        value, default=_json_default, ensure_ascii=False, sort_keys=True
    ).encode()


def _json_default(value: Any) -> Any:
    # Values people reach for that JSON has no word for, sent the way Lodol
    # reads them: dates and times as ISO 8601 text, which a text input keeps
    # and a date-aware step understands; Decimals as text, which a number
    # input reads without the rounding a float would add.
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, (decimal.Decimal, uuid.UUID)):
        return str(value)
    raise TypeError(
        f"a {type(value).__name__} can't be sent to Lodol. Use text, a number, "
        "true/false, a list or a dict."
    )


def expect_object(response: APIResponse, context: str) -> dict[str, Any]:
    """The response body, which should be a JSON object."""
    body = response.body
    if not isinstance(body, dict):
        raise APIResponseValidationError(
            f"{context} returned {type(body).__name__}, expected object",
            status_code=response.status_code,
            body=body,
        )
    return body


def expect_list(
    value: Any, context: str, response: APIResponse
) -> list[dict[str, Any]]:
    """A list of JSON objects from inside a response."""
    if not isinstance(value, list):
        raise APIResponseValidationError(
            f"{context} returned {type(value).__name__}, expected list",
            status_code=response.status_code,
            body=response.body,
        )
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise APIResponseValidationError(
                f"{context}[{index}] returned {type(item).__name__}, expected object",
                status_code=response.status_code,
                body=response.body,
            )
    return value


# ── Configuration ───────────────────────────────────────────────────────


def resolve_api_key(api_key: str | None) -> str:
    key = (api_key or os.environ.get("LODOL_API_KEY") or "").strip()
    if not key:
        raise ConfigurationError(
            "No Lodol API key. Create one in Lodol under Settings > API Keys, "
            "then set the LODOL_API_KEY environment variable or pass "
            "api_key=... to the client."
        )
    return key


def resolve_base_url(base_url: str | None) -> str:
    """The API's address, from ``base_url``, then ``LODOL_BASE_URL``, then
    Lodol's cloud.

    A bare host (``http://localhost:3901``) gets the API's path added. The key
    travels with every request, so anything but this machine must be HTTPS.
    """
    raw = (
        base_url or os.environ.get("LODOL_BASE_URL") or constants.DEFAULT_BASE_URL
    ).strip()
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigurationError(
            "base_url must be a full address such as "
            f"{constants.DEFAULT_BASE_URL}, not {raw!r}"
        )
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ConfigurationError(
            f"base_url must use https://, not {raw!r}, so your API key is never "
            "sent unencrypted. Plain http:// is allowed only for this machine "
            "(localhost), as Lodol Local serves it."
        )
    url = raw.rstrip("/")
    if not parts.path.strip("/"):
        url += constants.API_PATH
    return url


# Names that always mean this machine. host.docker.internal is how a script
# in a container reaches Lodol Local on the machine running it.
_THIS_MACHINE = frozenset({"localhost", "host.docker.internal"})


def _is_loopback(host: str) -> bool:
    if host in _THIS_MACHINE or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_timeout(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise ConfigurationError("timeout must be a number of seconds")
    if timeout <= 0:
        raise ConfigurationError("timeout must be greater than 0")
    return float(timeout)


def check_max_retries(max_retries: int) -> int:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise ConfigurationError("max_retries must be a whole number")
    if max_retries < 0:
        raise ConfigurationError("max_retries must be 0 or more")
    return max_retries


def http_timeouts(timeout: float) -> httpx2.Timeout:
    return httpx2.Timeout(timeout, connect=min(timeout, constants.CONNECT_TIMEOUT))


# ── Retrying ────────────────────────────────────────────────────────────

# The request never left this machine, so sending it again is safe whatever
# it was.
_NOT_SENT = (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout)
# Worth another try at all. Anything else (an unsupported URL, a proxy that
# refuses us) won't fix itself.
_TRANSIENT = (httpx2.TimeoutException, httpx2.NetworkError, httpx2.RemoteProtocolError)


def retry_delay_after_error(
    error: httpx2.TransportError,
    request: APIRequest,
    retries_taken: int,
    max_retries: int,
) -> float | None:
    """Seconds to wait before sending again after a network failure, or
    ``None`` to give up."""
    if retries_taken >= max_retries or not isinstance(error, _TRANSIENT):
        return None
    if is_certificate_error(error):
        # A certificate that can't be verified now won't be in a second.
        return None
    if not (request.resendable or isinstance(error, _NOT_SENT)):
        return None
    return backoff(retries_taken)


def retry_delay_after_response(
    response: httpx2.Response,
    request: APIRequest,
    retries_taken: int,
    max_retries: int,
) -> float | None:
    """Seconds to wait before sending again after this response, or ``None``
    to take it as the answer. (A 409 for a request still in flight under its
    Idempotency-Key is waited out separately: see ``in_flight_delay``.)"""
    status = response.status_code
    if status < 400 or retries_taken >= max_retries:
        return None
    retry_after = retry_after_seconds(response.headers)
    if retry_after is not None and retry_after > constants.MAX_RETRY_AFTER:
        # A month's runs used up, say: worth saying now, not sleeping through.
        return None
    if status == 429:
        # Lodol checks its limits before doing anything, so a refused request
        # of any kind can be sent again. It marks the limits that clear by
        # themselves (requests a minute) with Retry-After; a 429 without one
        # is the answer.
        return retry_after
    if not request.resendable:
        return None
    if status == 408 or status >= 500:
        return backoff(retries_taken) if retry_after is None else retry_after
    return None


def in_flight_delay(response: httpx2.Response, request: APIRequest) -> float | None:
    """Seconds to wait when Lodol is still working on an earlier attempt of
    this request (409 with Retry-After, for the same Idempotency-Key), or
    ``None`` when the response isn't that."""
    if response.status_code != 409 or not request.resendable:
        return None
    retry_after = retry_after_seconds(response.headers)
    if retry_after is None or retry_after > constants.MAX_RETRY_AFTER:
        return None
    return retry_after


def backoff(retries_taken: int) -> float:
    """Exponential backoff with jitter, so many clients that failed together
    don't all come back at the same moment."""
    delay = min(
        constants.INITIAL_RETRY_DELAY * 2.0**retries_taken, constants.MAX_RETRY_DELAY
    )
    return delay * (1 - 0.25 * random.random())


def retry_after_seconds(headers: httpx2.Headers) -> float | None:
    """The ``Retry-After`` header in seconds (it may also be a date)."""
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:  # pragma: no cover - older Pythons only
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        seconds = (when - dt.datetime.now(dt.timezone.utc)).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, seconds)


def is_certificate_error(error: BaseException) -> bool:
    """Whether a connection failed because the server's certificate couldn't
    be verified."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return False


# ── Reading answers ─────────────────────────────────────────────────────

_STATUS_ERRORS: dict[int, type[APIStatusError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    402: PaymentRequiredError,
    403: PermissionDeniedError,
    404: NotFoundError,
    409: ConflictError,
    422: UnprocessableEntityError,
}


def parse_response(request: APIRequest, response: httpx2.Response) -> APIResponse:
    status = response.status_code
    if 200 <= status < 300:
        if not response.content:
            return APIResponse(body=None, status_code=status)
        try:
            return APIResponse(body=response.json(), status_code=status)
        except ValueError as exc:
            raise APIResponseValidationError(
                f"{request.describe()} returned a response that isn't JSON",
                status_code=status,
                body=response.text,
                response=response,
            ) from exc
    if 300 <= status < 400:
        location = response.headers.get("location", "another address")
        raise APIStatusError(
            f"Lodol answered {request.describe()} by redirecting to {location}. "
            "If Lodol has moved, set base_url to the new address.",
            status_code=status,
            response=response,
        )
    raise status_error(response)


def status_error(response: httpx2.Response) -> APIStatusError:
    """The exception for an error response, carrying the server's message."""
    status = response.status_code
    body: Any
    try:
        body = response.json()
    except ValueError:
        body = response.text or None
    message = ""
    code: str | None = None
    if isinstance(body, dict):
        message = str(body.get("error") or body.get("message") or "")
        code = str(body["error_code"]) if body.get("error_code") else None
    if not message:
        # Never an HTML error page from a proxy as the message; it stays on
        # ``body`` for anyone who needs it.
        message = f"Lodol answered with HTTP {status} {response.reason_phrase}".strip()

    if status == 429:
        retry_after = retry_after_seconds(response.headers)
        if code == "monthly_run_limit":
            limit = body.get("limit") if isinstance(body, dict) else None
            return RunLimitError(
                message,
                status_code=status,
                retry_after=retry_after,
                resets_at=parse_datetime(
                    body.get("resets_at") if isinstance(body, dict) else None
                ),
                limit=limit if isinstance(limit, int) else None,
                code=code,
                body=body,
                response=response,
            )
        return RateLimitError(
            message,
            status_code=status,
            retry_after=retry_after,
            code=code,
            body=body,
            response=response,
        )
    error_type = _STATUS_ERRORS.get(status) or (
        InternalServerError if status >= 500 else APIStatusError
    )
    return error_type(
        message, status_code=status, code=code, body=body, response=response
    )


def still_in_flight(response: httpx2.Response, request: APIRequest) -> ConflictError:
    """The error for a request Lodol was still working on when the client
    stopped waiting for it."""
    error = status_error(response)
    return ConflictError(
        f"{error.message} Lodol was still working on an earlier attempt of "
        f"{request.describe()} after {constants.IN_FLIGHT_MAX_WAIT:.0f} seconds, "
        "so it may yet go through: check before trying again. "
        f"(Idempotency-Key: {request.idempotency_key})",
        status_code=error.status_code,
        code=error.code,
        body=error.body,
        response=response,
    )


def connection_error(
    error: httpx2.RequestError, request: APIRequest, client: BaseClient
) -> APIConnectionError:
    if isinstance(error, httpx2.TimeoutException):
        return APITimeoutError(
            f"{request.describe()} got no answer from Lodol within "
            f"{client.timeout:g} seconds"
        )
    if is_certificate_error(error):
        return APIConnectionError(
            f"Couldn't verify the certificate of {client.base_url}: {error}. If "
            "your network inspects HTTPS through a proxy, set SSL_CERT_FILE to "
            "that proxy's certificate."
        )
    if not isinstance(error, httpx2.TransportError):
        return APIConnectionError(
            f"Lodol's answer to {request.describe()} couldn't be read: "
            f"{error or type(error).__name__}"
        )
    return APIConnectionError(
        f"Couldn't reach Lodol at {client.base_url} for {request.describe()}: "
        f"{error or type(error).__name__}"
    )


def _log_response(
    request: APIRequest, response: httpx2.Response, elapsed: float
) -> None:
    if logger.isEnabledFor(logging.DEBUG):
        replayed = response.headers.get("x-idempotent-replayed") == "true"
        logger.debug(
            "%s -> %s in %.2fs%s",
            request.describe(),
            response.status_code,
            elapsed,
            " (replayed: this request was already done)" if replayed else "",
        )


def _log_retry(
    request: APIRequest, reason: str, delay: float, retries_taken: int, limit: int
) -> None:
    logger.info(
        "%s failed (%s); trying again in %.1fs (retry %d of %d)",
        request.describe(),
        reason,
        delay,
        retries_taken + 1,
        limit,
    )


def _log_in_flight(request: APIRequest, delay: float) -> None:
    logger.info(
        "%s: Lodol is still working on an earlier attempt; asking again in %.0fs",
        request.describe(),
        delay,
    )


# ── Clients ─────────────────────────────────────────────────────────────

_SyncClientT = TypeVar("_SyncClientT", bound="SyncAPIClient")
_AsyncClientT = TypeVar("_AsyncClientT", bound="AsyncAPIClient")


class BaseClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
    ) -> None:
        self.api_key = resolve_api_key(api_key)
        self.base_url = resolve_base_url(base_url)
        self.timeout = check_timeout(timeout)
        self.max_retries = check_max_retries(max_retries)
        self._timeouts = http_timeouts(self.timeout)
        # Set from each response that reports it; the wait for a run paces
        # itself by it.
        self._rate_limit: RateLimit | None = None

    def __repr__(self) -> str:
        # Never the key: a repr ends up in logs and tracebacks.
        return f"{type(self).__name__}(base_url={self.base_url!r})"

    @property
    def _trusts_environment(self) -> bool:
        # Proxy settings from the environment would carry plain-HTTP traffic,
        # key and all, through the proxy: Lodol Local is reached directly.
        return not self.base_url.startswith("http://")

    def _prepare(self, request: APIRequest) -> tuple[str, dict[str, str], bytes | None]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": constants.USER_AGENT,
        }
        content = None
        if request.body is not None:
            content = encode_json(request.body)
            headers["Content-Type"] = "application/json"
        if request.idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key_header(request.idempotency_key)
        return f"{self.base_url}/{request.path.lstrip('/')}", headers, content

    def _note(self, response: httpx2.Response) -> None:
        rate_limit = parse_rate_limit(response.headers)
        if rate_limit is not None:
            self._rate_limit = rate_limit


class SyncAPIClient(BaseClient):
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        http_client: httpx2.Client | None,
    ) -> None:
        super().__init__(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        if http_client is not None and not isinstance(http_client, httpx2.Client):
            raise TypeError(
                f"http_client must be an httpx2.Client, not {type(http_client).__name__}"
            )
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx2.Client(
            timeout=self._timeouts, trust_env=self._trusts_environment
        )

    def close(self) -> None:
        """Close the client's connections. A client given its own
        ``http_client`` leaves that open for its owner to close."""
        if self._owns_http_client:
            self._http_client.close()

    def __enter__(self: _SyncClientT) -> _SyncClientT:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _send(
        self,
        request: APIRequest,
        *,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> APIResponse:
        url, headers, content = self._prepare(request)
        retries = self.max_retries if max_retries is None else max_retries
        timeouts = self._timeouts if timeout is None else http_timeouts(timeout)
        retries_taken = 0
        waited_in_flight = 0.0
        while True:
            started = monotonic()
            try:
                response = self._http_client.request(
                    request.method,
                    url,
                    params=request.params,
                    content=content,
                    headers=headers,
                    timeout=timeouts,
                )
            except httpx2.TransportError as exc:
                delay = retry_delay_after_error(exc, request, retries_taken, retries)
                if delay is None:
                    raise connection_error(exc, request, self) from exc
                reason = type(exc).__name__
            except httpx2.RequestError as exc:
                raise connection_error(exc, request, self) from exc
            else:
                _log_response(request, response, monotonic() - started)
                self._note(response)
                pause = in_flight_delay(response, request)
                if pause is not None:
                    if waited_in_flight + pause > constants.IN_FLIGHT_MAX_WAIT:
                        raise still_in_flight(response, request)
                    _log_in_flight(request, pause)
                    sleep(pause)
                    waited_in_flight += pause
                    continue
                delay = retry_delay_after_response(
                    response, request, retries_taken, retries
                )
                if delay is None:
                    return parse_response(request, response)
                reason = f"HTTP {response.status_code}"
            _log_retry(request, reason, delay, retries_taken, retries)
            sleep(delay)
            retries_taken += 1


class AsyncAPIClient(BaseClient):
    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_retries: int,
        http_client: httpx2.AsyncClient | None,
    ) -> None:
        super().__init__(
            api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries
        )
        if http_client is not None and not isinstance(http_client, httpx2.AsyncClient):
            raise TypeError(
                "http_client must be an httpx2.AsyncClient, not "
                f"{type(http_client).__name__}"
            )
        self._owns_http_client = http_client is None
        self._http_client = http_client or httpx2.AsyncClient(
            timeout=self._timeouts, trust_env=self._trusts_environment
        )

    async def close(self) -> None:
        """Close the client's connections. A client given its own
        ``http_client`` leaves that open for its owner to close."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self: _AsyncClientT) -> _AsyncClientT:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _send(
        self,
        request: APIRequest,
        *,
        max_retries: int | None = None,
        timeout: float | None = None,
    ) -> APIResponse:
        url, headers, content = self._prepare(request)
        retries = self.max_retries if max_retries is None else max_retries
        timeouts = self._timeouts if timeout is None else http_timeouts(timeout)
        retries_taken = 0
        waited_in_flight = 0.0
        while True:
            started = monotonic()
            try:
                response = await self._http_client.request(
                    request.method,
                    url,
                    params=request.params,
                    content=content,
                    headers=headers,
                    timeout=timeouts,
                )
            except httpx2.TransportError as exc:
                delay = retry_delay_after_error(exc, request, retries_taken, retries)
                if delay is None:
                    raise connection_error(exc, request, self) from exc
                reason = type(exc).__name__
            except httpx2.RequestError as exc:
                raise connection_error(exc, request, self) from exc
            else:
                _log_response(request, response, monotonic() - started)
                self._note(response)
                pause = in_flight_delay(response, request)
                if pause is not None:
                    if waited_in_flight + pause > constants.IN_FLIGHT_MAX_WAIT:
                        raise still_in_flight(response, request)
                    _log_in_flight(request, pause)
                    await async_sleep(pause)
                    waited_in_flight += pause
                    continue
                delay = retry_delay_after_response(
                    response, request, retries_taken, retries
                )
                if delay is None:
                    return parse_response(request, response)
                reason = f"HTTP {response.status_code}"
            _log_retry(request, reason, delay, retries_taken, retries)
            await async_sleep(delay)
            retries_taken += 1
