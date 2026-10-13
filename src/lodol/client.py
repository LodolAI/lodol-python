"""The clients: ``Lodol`` for ordinary code, ``AsyncLodol`` for asyncio."""

from __future__ import annotations

from typing import Any, Mapping

import httpx2

from lodol._transport import APIRequest, AsyncAPIClient, SyncAPIClient
from lodol.constants import DEFAULT_MAX_RETRIES, DEFAULT_TIMEOUT
from lodol.resources import (
    Approvals,
    AsyncApprovals,
    AsyncRuns,
    AsyncWorkflows,
    Runs,
    Workflows,
)


class Lodol(SyncAPIClient):
    """Lodol's Developer API, from Python.

    ``Lodol()`` reads the API key from ``LODOL_API_KEY``. Make one client and
    reuse it: it keeps connections open between calls, and can be shared
    between threads.

    - ``base_url``: where the API is. Defaults to ``LODOL_BASE_URL``, then
      Lodol's cloud. For Lodol Local, ``"http://localhost:3901"``.
    - ``timeout``: seconds a single request may take.
    - ``max_retries``: how many times a request that failed for a passing
      reason (a dropped connection, a rate limit, a server error) is sent
      again. Only requests that can't take effect twice are retried.
    - ``http_client``: an ``httpx2.Client`` of your own, for proxies, custom
      certificates or tests. You close it; the SDK won't.
    """

    workflows: Workflows
    runs: Runs
    approvals: Approvals

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx2.Client | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self.workflows = Workflows(self)
        self.runs = Runs(self)
        self.approvals = Approvals(self)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Call any Developer API endpoint, for one the SDK doesn't cover yet.

        ``path`` is relative to the API, as in ``"/workflows"``. Returns the
        parsed JSON body; errors raise as they do everywhere else. A write is
        retried only when it carries an ``idempotency_key``.
        """
        return self._send(
            _raw_request(method, path, params, json, idempotency_key)
        ).body


class AsyncLodol(AsyncAPIClient):
    """Lodol's Developer API, from asyncio (or trio) code.

    The same as :class:`Lodol`, with every call awaited and lists looped
    over with ``async for``. ``http_client`` is an ``httpx2.AsyncClient``.
    """

    workflows: AsyncWorkflows
    runs: AsyncRuns
    approvals: AsyncApprovals

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        super().__init__(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self.workflows = AsyncWorkflows(self)
        self.runs = AsyncRuns(self)
        self.approvals = AsyncApprovals(self)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Call any Developer API endpoint, for one the SDK doesn't cover yet.

        ``path`` is relative to the API, as in ``"/workflows"``. Returns the
        parsed JSON body; errors raise as they do everywhere else. A write is
        retried only when it carries an ``idempotency_key``.
        """
        response = await self._send(
            _raw_request(method, path, params, json, idempotency_key)
        )
        return response.body


def _raw_request(
    method: str,
    path: str,
    params: Mapping[str, Any] | None,
    json: Any,
    idempotency_key: str | None,
) -> APIRequest:
    query = None
    if params is not None:
        query = {
            key: _query_value(value)
            for key, value in params.items()
            if value is not None
        }
    return APIRequest(
        method.upper(),
        path,
        params=query,
        body=json,
        idempotency_key=idempotency_key,
    )


def _query_value(value: Any) -> str | list[str]:
    if isinstance(value, (list, tuple)):
        # A list repeats the parameter: ?id=a&id=b.
        return [str(_query_value(item)) for item in value]
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
