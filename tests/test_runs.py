"""Runs: listing, looking one up, waiting for one, stopping one."""

from __future__ import annotations

import logging

import httpx2
import pytest

from lodol import (
    APIConnectionError,
    BadRequestError,
    Lodol,
    NotFoundError,
    Run,
    RunFailedError,
    RunStatus,
    Step,
    WaitTimeoutError,
    Workflow,
)
from tests.conftest import FakeAPI, FakeClock, respond, run_json


def runs_page(*ids: str) -> dict[str, list[dict[str, object]]]:
    return {"executions": [run_json(execution_id=i, status="success") for i in ids]}


# ── Listing ──────────────────────────────────────────────────────────────


def test_list_fetches_only_as_far_as_you_go(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, runs_page(*[f"r{i}" for i in range(100)])))

    runs = client.runs.list()
    assert api.requests == []  # nothing yet: nothing has been looped over

    first = next(iter(runs))

    assert first.id == "r0"
    assert api.sent() == ("GET", "/executions", {"limit": "100"})


def test_list_pages_through_everything(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, runs_page(*[f"r{i}" for i in range(100)])),
        respond(200, runs_page("r100", "r101")),
    )

    ids = [run.id for run in client.runs.list()]

    assert ids == [f"r{i}" for i in range(102)]
    # The next page starts after the last run of the one before.
    assert api.sent(1)[2] == {"limit": "100", "after": "r99"}
    assert len(api.requests) == 2


def test_list_stops_at_an_empty_page(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, runs_page(*[f"r{i}" for i in range(100)])),
        respond(200, runs_page()),
    )

    assert len(list(client.runs.list())) == 100


def test_list_with_a_limit_asks_for_no_more(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, runs_page("r1", "r2", "r3")))

    runs = list(client.runs.list(limit=3))

    assert [run.id for run in runs] == ["r1", "r2", "r3"]
    assert api.sent() == ("GET", "/executions", {"limit": "3"})
    assert len(api.requests) == 1


def test_a_limit_over_a_page_spans_pages(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, runs_page(*[f"r{i}" for i in range(100)])),
        respond(200, runs_page(*[f"r{i}" for i in range(100, 150)])),
    )

    runs = list(client.runs.list(limit=150))

    assert len(runs) == 150
    assert api.sent(1)[2] == {"limit": "50", "after": "r99"}


def test_list_one_workflows_runs(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, runs_page("r1")))

    list(client.runs.list(workflow=Workflow(id="665f", name="Monthly report"), limit=5))

    assert api.sent()[2] == {"limit": "5", "workflow_id": "665f"}


def test_each_loop_starts_from_the_newest(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, runs_page("r1")), respond(200, runs_page("r1")))
    runs = client.runs.list(limit=1)

    assert [run.id for run in runs] == [run.id for run in runs] == ["r1"]


def test_list_parses_runs(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            {
                "executions": [
                    run_json(
                        version_id="6683",
                        workflow_name="Monthly report",
                        status="success",
                        started_at="2026-01-15T10:30:00+00:00",
                        completed_at="2026-01-15T10:30:03+00:00",
                    )
                ]
            },
        )
    )

    (run,) = client.runs.list(limit=1)

    assert run.workflow_name == "Monthly report"
    assert run.version_id == "6683"
    assert run.completed_at is not None and run.started_at is not None
    assert (run.completed_at - run.started_at).total_seconds() == 3
    assert run.steps is None


@pytest.mark.parametrize("limit", [0, -1, 2.5, True])
def test_list_refuses_a_bad_limit(client: Lodol, limit: object) -> None:
    with pytest.raises(ValueError, match="limit must be"):
        client.runs.list(limit=limit)  # type: ignore[arg-type]


def test_list_says_what_it_is(client: Lodol) -> None:
    assert repr(client.runs.list(limit=5)) == (
        "<Paginator: up to 5 runs, newest first; loop over it to fetch them>"
    )


# ── Looking one up ───────────────────────────────────────────────────────


def test_get(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, run_json(status="running", error=None)))

    run = client.runs.get("683b")

    assert api.sent() == ("GET", "/executions/683b", {})
    assert run.status == RunStatus.RUNNING
    assert run.steps is None


def test_get_with_steps(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            run_json(
                status="failed",
                error="Step 2 failed",
                steps=[
                    {"index": 0, "status": "completed", "result": {"rows": 3}},
                    {
                        "index": 1,
                        "status": "failed",
                        "result": None,
                        "error": "Bad address",
                    },
                ],
            ),
        )
    )

    run = client.runs.get(Run(id="683b", status="running"), include_steps=True)

    assert api.sent()[2] == {"include_step_results": "true"}
    assert run.steps == [
        Step(index=0, status="completed", result={"rows": 3}),
        Step(index=1, status="failed", error="Bad address"),
    ]
    assert run.finished and not run.succeeded


def test_get_unknown(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(404, {"error": "Execution not found"}))

    with pytest.raises(NotFoundError):
        client.runs.get("683b")


# ── Waiting ──────────────────────────────────────────────────────────────


def test_wait_checks_often_at_first_then_less_often(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        *[respond(200, run_json(status="running"))] * 9,
        respond(200, run_json(status="success")),
        respond(200, run_json(status="success", steps=[])),
    )

    run = client.runs.wait("683b")

    assert run.succeeded and run.steps == []
    # A long run costs few of the workspace's requests a minute.
    assert clock.sleeps == pytest.approx(
        [1.0, 1.5, 2.25, 3.375, 5.0625, 7.59375, 10.0, 10.0, 10.0]
    )


def test_wait_without_steps(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, run_json(status="success")))

    run = client.runs.wait("683b", include_steps=False)

    assert run.succeeded
    assert len(api.requests) == 1


def test_a_failed_run_raises_with_the_run(client: Lodol, api: FakeAPI) -> None:
    failed = run_json(
        status="failed",
        error="The recipient address is invalid.",
        steps=[
            {
                "index": 0,
                "status": "failed",
                "error": "The recipient address is invalid.",
            }
        ],
    )
    api.queue(respond(200, run_json(status="failed")), respond(200, failed))

    with pytest.raises(RunFailedError) as exc:
        client.runs.wait("683b")

    assert str(exc.value) == "Run 683b failed: The recipient address is invalid."
    assert exc.value.run.status == "failed"
    assert exc.value.run.steps is not None and exc.value.run.steps[0].status == "failed"


def test_a_failure_without_a_run_error_names_the_failed_steps(
    client: Lodol, api: FakeAPI
) -> None:
    failed = run_json(
        status="failed",
        steps=[
            {"index": 0, "status": "completed"},
            {"index": 1, "status": "failed", "error": "Sheet not found"},
        ],
    )
    api.queue(respond(200, run_json(status="failed")), respond(200, failed))

    with pytest.raises(RunFailedError, match="Run 683b failed: Sheet not found"):
        client.runs.wait("683b")


def test_a_failure_with_no_reason_at_all(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, run_json(status="failed")))

    with pytest.raises(RunFailedError, match="Lodol gave no reason"):
        client.runs.wait("683b", include_steps=False)


def test_a_stopped_run_raises(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, run_json(status="stopped")),
        respond(200, run_json(status="stopped")),
    )

    with pytest.raises(RunFailedError, match="Run 683b was stopped before it finished"):
        client.runs.wait("683b")


def test_wait_gives_up_at_its_timeout_and_says_why(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(200, run_json(status="running")),
        respond(200, run_json(status="awaiting_approval")),
        respond(200, run_json(status="awaiting_approval")),
    )

    with pytest.raises(WaitTimeoutError) as exc:
        client.runs.wait("683b", timeout=2)

    assert isinstance(exc.value, TimeoutError)
    assert exc.value.run is not None
    assert exc.value.run.status == "awaiting_approval"
    assert str(exc.value) == (
        "Stopped waiting for run 683b after 2 seconds: it is waiting for someone "
        "to answer an approval request. The run carries on; wait for it again, or "
        "stop it with runs.stop()."
    )
    # It never sleeps past the deadline.
    assert sum(clock.sleeps) == pytest.approx(2.0)


def test_a_zero_timeout_looks_once(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(respond(200, run_json(status="queued")))

    with pytest.raises(WaitTimeoutError, match="it is still waiting to start"):
        client.runs.wait("683b", timeout=0)

    assert len(api.requests) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("timeout", [-1, "60"])
def test_wait_refuses_a_bad_timeout(client: Lodol, timeout: object) -> None:
    with pytest.raises((ValueError, TypeError), match="timeout"):
        client.runs.wait("683b", timeout=timeout)  # type: ignore[arg-type]


def test_wait_rides_out_outages_and_rate_limits(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """Each look gets its own retries; when those run out too, the wait just
    looks again later, and once the API asks for longer, it waits longer."""
    down = respond(503, {"error": "Down for a moment"})
    limited = respond(429, {"error": "Rate limit exceeded."}, {"Retry-After": "30"})
    api.queue(
        respond(200, run_json(status="running")),
        down,
        down,
        down,
        httpx2.ConnectError("refused"),
        httpx2.ConnectError("refused"),
        httpx2.ConnectError("refused"),
        limited,
        limited,
        limited,
        respond(200, run_json(status="success")),
        respond(200, run_json(status="success", steps=[])),
    )

    run = client.runs.wait("683b")

    assert run.succeeded
    assert 30.0 in clock.sleeps


def test_wait_gives_up_when_it_cant_look_for_too_long(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[httpx2.ConnectError("refused")] * 300)

    with pytest.raises(APIConnectionError):
        client.runs.wait("683b")

    # About five minutes of trying, not forever.
    assert 300 <= sum(clock.sleeps) <= 330


def test_wait_doesnt_ride_out_a_real_answer(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(404, {"error": "Execution not found"}))

    with pytest.raises(NotFoundError):
        client.runs.wait("683b")


def test_wait_says_what_the_run_is_doing(
    client: Lodol, api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    api.queue(
        respond(200, run_json(status="queued")),
        respond(200, run_json(status="awaiting_approval")),
        respond(200, run_json(status="success")),
        respond(200, run_json(status="success", steps=[])),
    )

    with caplog.at_level(logging.INFO, logger="lodol"):
        client.runs.wait(Run(id="683b", status="queued"))

    lines = [r.getMessage() for r in caplog.records if r.name == "lodol"]
    # Said once per change, not on every look.
    assert lines == ["Run 683b is waiting for someone to answer an approval request"]


# ── Stopping ─────────────────────────────────────────────────────────────


def test_stop(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"execution_id": "683b", "status": "stopping"}))

    run = client.runs.stop("683b")

    assert api.sent() == ("POST", "/executions/683b/stop", {})
    assert api.last.headers["Idempotency-Key"].startswith("lodol-run-stop-")
    assert run.status == "stopping"


def test_stopping_a_run_that_already_finished_returns_it(
    client: Lodol, api: FakeAPI
) -> None:
    """A run that finishes just as it is stopped (a wait timed out, say) is
    no error: it is returned as it finished."""
    api.queue(
        respond(400, {"error": "Execution already success"}),
        respond(200, run_json(status="success")),
    )

    run = client.runs.stop("683b")

    assert run.succeeded
    assert api.sent(1)[0:2] == ("GET", "/executions/683b")


def test_stop_raises_other_refusals(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(400, {"error": "Something else"}))

    with pytest.raises(BadRequestError, match="Something else"):
        client.runs.stop("683b")


def test_a_wait_that_could_never_look_says_so(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[httpx2.ConnectError("refused")] * 30)

    with pytest.raises(WaitTimeoutError) as exc:
        client.runs.wait("683b", timeout=5)

    assert exc.value.run is None
    assert "after 5 seconds: it couldn't be checked" in str(exc.value)


def test_a_timeout_doesnt_pass_off_an_old_status_as_current(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[httpx2.ConnectError("refused")] * 30)
    known = Run(id="683b", status="queued")

    with pytest.raises(WaitTimeoutError) as exc:
        client.runs.wait(known, timeout=5)

    assert "it couldn't be checked" in str(exc.value)
    assert exc.value.run is known
