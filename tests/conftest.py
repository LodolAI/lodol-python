from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx2
import pytest

from lodol import AsyncLodol, Lodol, _transport
from tests import standin

API = "https://api-prod.lodol.com/api/v1"

Reply = httpx2.Response | Exception | Callable[[httpx2.Request], httpx2.Response]


def respond(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> httpx2.Response:
    if body is None:
        return httpx2.Response(status, headers=headers)
    return httpx2.Response(status, json=body, headers=headers)


class FakeAPI:
    """Stands in for the Developer API: answers with the replies queued on
    it, in order, and keeps every request it was sent."""

    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[httpx2.Request] = []

    def queue(self, *replies: Reply) -> None:
        self.replies.extend(replies)

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.replies:
            raise AssertionError(f"Unexpected request: {request.method} {request.url}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx2.Response):
            return reply
        return reply(request)

    @property
    def last(self) -> httpx2.Request:
        return self.requests[-1]

    def sent(self, index: int = -1) -> tuple[str, str, dict[str, str]]:
        """Method, path (from the API's root) and query of a request."""
        request = self.requests[index]
        path = str(request.url).split("?")[0].removeprefix(API)
        return request.method, path, dict(request.url.params)

    def body(self, index: int = -1) -> Any:
        content = self.requests[index].content
        return json.loads(content) if content else None


class FakeClock:
    """Time that passes only when the SDK sleeps, so retries and waits run
    instantly and every pause is on record."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)

    def monotonic(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LODOL_API_KEY", "LODOL_BASE_URL", "LODOL_LOG"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(params=["asyncio", "trio"])
def anyio_backend(request: pytest.FixtureRequest) -> str:
    """Every async test runs on both event loops AsyncLodol supports."""
    return str(request.param)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(_transport, "sleep", fake.sleep)
    monkeypatch.setattr(_transport, "async_sleep", fake.async_sleep)
    monkeypatch.setattr(_transport, "monotonic", fake.monotonic)
    return fake


@pytest.fixture
def client(api: FakeAPI, clock: FakeClock) -> Iterator[Lodol]:
    http = httpx2.Client(transport=httpx2.MockTransport(api.handle))
    with Lodol(api_key="sk_live_test", http_client=http) as lodol:
        yield lodol
    http.close()


@pytest.fixture
async def aclient(api: FakeAPI, clock: FakeClock) -> AsyncIterator[AsyncLodol]:
    http = httpx2.AsyncClient(transport=httpx2.MockTransport(api.handle))
    async with AsyncLodol(api_key="sk_live_test", http_client=http) as lodol:
        yield lodol
    await http.aclose()


def workflow_json(**fields: Any) -> dict[str, Any]:
    return {"id": "665f", "name": "Monthly report", **fields}


def run_json(**fields: Any) -> dict[str, Any]:
    return {"execution_id": "683b", "workflow_id": "665f", "status": "queued", **fields}


def approval_json(**fields: Any) -> dict[str, Any]:
    return {
        "id": "a1",
        "kind": "approval",
        "status": "pending",
        "title": "Send the invoice?",
        "workflow_id": "665f",
        "workflow_name": "Monthly report",
        "execution_id": "683b",
        **fields,
    }


class ApprovalsServer:
    """Answers ``GET /approvals`` as the server does: it reads a page of
    requests, then leaves out those from workflows the key's member can't
    see, and still says where the next page starts. Queue it once for each
    page it should answer."""

    def __init__(
        self, approvals: list[dict[str, Any]], *, hidden_workflows: set[str]
    ) -> None:
        self.approvals = approvals  # newest first
        self.hidden_workflows = hidden_workflows

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        params = request.url.params
        rows = self.approvals
        if "before" in params:
            ids = [row["id"] for row in rows]
            rows = rows[ids.index(params["before"]) + 1 :]
        limit = int(params["limit"])
        read = rows[:limit]
        return respond(
            200,
            {
                "approvals": [
                    row
                    for row in read
                    if row["workflow_id"] not in self.hidden_workflows
                ],
                "next_before": read[-1]["id"] if len(read) == limit else None,
            },
        )


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[standin.Server]:
    """The stand-in Developer API, on a real socket on this machine."""
    yield from standin.serve(monkeypatch)
