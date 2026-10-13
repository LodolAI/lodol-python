"""AsyncLodol does what Lodol does, awaited."""

from __future__ import annotations

import httpx2
import pytest

from lodol import (
    APIConnectionError,
    AsyncLodol,
    BadRequestError,
    ConcurrencyLimitError,
    ConflictError,
    DeletedWorkflow,
    InternalServerError,
    RunFailedError,
    WaitTimeoutError,
)
from tests.conftest import (
    FakeAPI,
    FakeClock,
    approval_json,
    respond,
    run_json,
    workflow_json,
)

pytestmark = pytest.mark.anyio


async def test_workflows(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, {"workflows": [workflow_json()]}),
        respond(200, workflow_json(program={"body": []})),
        respond(200, {"id": "665f", "name": "Monthly report", "deleted": True}),
    )

    (workflow,) = await aclient.workflows.list()
    detailed = await aclient.workflows.get(workflow)
    deleted = await aclient.workflows.delete(workflow)

    assert detailed.program == {"body": []}
    assert deleted == DeletedWorkflow(id="665f", name="Monthly report")
    assert [api.sent(i)[0] for i in range(3)] == ["GET", "GET", "DELETE"]


async def test_run_and_wait(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(202, run_json()),
        respond(200, run_json(status="running")),
        respond(200, run_json(status="success")),
        respond(
            200, run_json(status="success", steps=[{"index": 0, "status": "completed"}])
        ),
    )

    run = await aclient.workflows.run("665f", inputs={"client": "Acme"}, wait=True)

    assert run.succeeded and run.steps is not None
    assert api.body(0) == {"inputs": {"client": "Acme"}}
    assert clock.sleeps == [1.0, 1.5]


async def test_a_failed_run_raises(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(
        respond(202, run_json()),
        respond(200, run_json(status="failed", error="Bad address")),
        respond(200, run_json(status="failed", error="Bad address", steps=[])),
    )

    with pytest.raises(RunFailedError, match="Bad address"):
        await aclient.workflows.run("665f", wait=True)


async def test_wait_times_out(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(*[respond(200, run_json(status="running"))] * 3)

    with pytest.raises(WaitTimeoutError, match="it is still running"):
        await aclient.runs.wait("683b", timeout=2)


async def test_wait_rides_out_an_outage(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        *[httpx2.ConnectError("refused")] * 3,
        respond(200, run_json(status="success")),
    )

    run = await aclient.runs.wait("683b", include_steps=False)

    assert run.succeeded


async def test_list_pages_with_async_for(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200, {"executions": [run_json(execution_id=f"r{i}") for i in range(100)]}
        ),
        respond(200, {"executions": [run_json(execution_id="r100")]}),
    )

    ids = [run.id async for run in aclient.runs.list(workflow="665f")]

    assert len(ids) == 101
    assert api.sent(1)[2] == {"limit": "100", "workflow_id": "665f", "after": "r99"}


async def test_list_with_a_limit(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"executions": [run_json(execution_id="r1")]}))

    runs = [run async for run in aclient.runs.list(limit=1)]

    assert [run.id for run in runs] == ["r1"]
    assert "up to 1 runs" in repr(aclient.runs.list(limit=1))


async def test_stop(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            400,
            {"error": "Execution already failed", "error_code": "run_already_finished"},
        ),
        respond(200, run_json(status="failed")),
    )

    run = await aclient.runs.stop("683b")

    assert run.status == "failed"


async def test_stop_returns_the_stopping_run(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"execution_id": "683b", "status": "stopping"}))

    assert (await aclient.runs.stop("683b")).status == "stopping"


async def test_approvals(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, {"approvals": [approval_json()], "next_before": None}),
        respond(200, approval_json(status="approved")),
        respond(200, approval_json(status="rejected")),
        respond(200, approval_json(kind="reply", status="replied")),
        respond(200, approval_json()),
    )

    pending = [approval async for approval in aclient.approvals.list(limit=10)]
    approved = await aclient.approvals.approve(pending[0], comment="OK")
    rejected = await aclient.approvals.reject("a1")
    replied = await aclient.approvals.reply("a2", "4471")
    fetched = await aclient.approvals.get("a1")

    assert approved.status == "approved" and rejected.status == "rejected"
    assert replied.status == "replied" and fetched.pending
    assert api.body(1) == {"decision": "approved", "comment": "OK"}
    assert api.body(3) == {"reply": "4471"}


async def test_retries_sleep_without_blocking(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(503),
        respond(429, headers={"Retry-After": "2"}),
        respond(200, {"workflows": []}),
    )

    assert await aclient.workflows.list() == []
    assert clock.sleeps[1] == 2.0


async def test_errors_match_the_sync_client(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(429, {"error": "Too many", "error_code": "concurrency_limit"}))

    with pytest.raises(ConcurrencyLimitError):
        await aclient.workflows.run("665f")


async def test_a_timeout_after_retries(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(*[respond(500)] * 3)

    with pytest.raises(InternalServerError):
        await aclient.workflows.list()


async def test_request(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"ok": True}))

    assert await aclient.request("GET", "/anything", params={"flag": False}) == {
        "ok": True
    }
    assert api.sent()[2] == {"flag": "false"}


async def test_closing_closes_the_connections_it_opened() -> None:
    async with AsyncLodol(api_key="k") as client:
        pass

    assert client._http_client.is_closed


async def test_leaves_a_given_http_client_open() -> None:
    http = httpx2.AsyncClient()
    async with AsyncLodol(api_key="k", http_client=http):
        pass

    assert not http.is_closed
    await http.aclose()


async def test_run_without_waiting(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()))

    run = await aclient.workflows.run("665f")

    assert run.status == "queued"
    assert len(api.requests) == 1


async def test_wait_gives_up_when_it_cant_look_for_too_long(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[httpx2.ConnectError("refused")] * 300)

    with pytest.raises(APIConnectionError):
        await aclient.runs.wait("683b")

    assert 300 <= sum(clock.sleeps) <= 330


async def test_stop_raises_other_refusals(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(respond(400, {"error": "Something else"}))

    with pytest.raises(BadRequestError, match="Something else"):
        await aclient.runs.stop("683b")


async def test_a_run_that_succeeded_is_never_reported_as_an_error(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(200, run_json(status="success")),
        *[httpx2.ConnectError("refused")] * 100,
    )

    run = await aclient.runs.wait("683b")

    assert run.succeeded and run.steps is None


async def test_wait_keeps_its_deadline(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    limited = respond(429, {"error": "Rate limit exceeded."}, {"Retry-After": "60"})
    api.queue(respond(200, run_json(status="running")), *[limited] * 5)

    with pytest.raises(WaitTimeoutError):
        await aclient.runs.wait("683b", timeout=5)

    assert sum(clock.sleeps) <= 5


async def test_a_workflow_that_starts_itself_isnt_waited_for(
    aclient: AsyncLodol, api: FakeAPI
) -> None:
    api.queue(respond(202, run_json(status="running", is_trigger_listener=True)))

    with pytest.warns(UserWarning, match="it is listening"):
        run = await aclient.workflows.run("665f", wait=True)

    assert run.status == "running" and len(api.requests) == 1


async def test_waiting_on_a_listening_workflow_returns_it(
    aclient: AsyncLodol, api: FakeAPI
) -> None:
    api.queue(respond(200, run_json(status="running", is_trigger_listener=True)))

    with pytest.warns(UserWarning, match="it is listening") as warned:
        run = await aclient.runs.wait("683b")

    assert run.is_trigger_listener and len(api.requests) == 1
    assert warned[0].filename == __file__


async def test_waiting_for_an_earlier_attempt(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    in_flight = respond(409, {"error": "Still processing"}, {"Retry-After": "5"})
    api.queue(httpx2.ReadTimeout("slow"), *[in_flight] * 5, respond(202, run_json()))

    run = await aclient.workflows.run("665f")

    assert run.id == "683b" and clock.sleeps.count(5.0) == 5


async def test_an_answer_that_cant_be_read(aclient: AsyncLodol, api: FakeAPI) -> None:
    api.queue(httpx2.DecodingError("bad gzip"))

    with pytest.raises(APIConnectionError, match="couldn't be read"):
        await aclient.workflows.list()


async def test_an_earlier_attempt_still_in_flight_after_five_minutes(
    aclient: AsyncLodol, api: FakeAPI, clock: FakeClock
) -> None:
    in_flight = respond(409, {"error": "Still processing"}, {"Retry-After": "5"})
    api.queue(httpx2.ReadTimeout("slow"), *[in_flight] * 70)

    with pytest.raises(ConflictError, match="still working on an earlier attempt"):
        await aclient.workflows.run("665f")
