"""The whole journey over a real socket, against the stand-in Developer API
in ``tests/standin.py``."""

from __future__ import annotations

import pytest

from lodol import (
    APITimeoutError,
    AsyncLodol,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    Lodol,
    NotFoundError,
    RunFailedError,
    UnprocessableEntityError,
)
from tests.standin import KEY, Server, address


def test_the_whole_journey(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        workflows = lodol.workflows.list()
        report = next(w for w in workflows if w.name == "Monthly report")
        assert [i.name for i in report.inputs] == ["client"]

        run = lodol.workflows.run(
            report, inputs={"client": "Acme"}, wait=True, timeout=10
        )
        assert run.succeeded
        assert run.steps is not None and run.steps[0].result == "Report for Acme"
        assert run.completed_at is not None

        with pytest.raises(RunFailedError, match="Sheet not found") as failed:
            lodol.workflows.run("7a1c", wait=True)
        assert failed.value.run.steps is not None

        history = list(lodol.runs.list(workflow=report))
        assert [r.id for r in history] == [run.id]
        assert history[0].workflow_name == "Monthly report"

        assert lodol.runs.stop(
            run
        ).succeeded  # already finished: returned as it finished

        with pytest.raises(
            BadRequestError, match="needs a value for its 'client' input"
        ):
            lodol.workflows.run(report)

        pending = list(lodol.approvals.list())
        assert [a.kind for a in pending] == ["reply", "approval"]
        replied = lodol.approvals.reply(pending[0], "4471")
        approved = lodol.approvals.approve(pending[1], comment="Checked")
        assert replied.decision is not None and replied.decision.reply == "4471"
        assert (
            approved.decision is not None
            and approved.decision.decided_by == "API key 'e2e'"
        )
        with pytest.raises(ConflictError):
            lodol.approvals.reject(pending[1])
        assert list(lodol.approvals.list()) == []

        deleted = lodol.workflows.delete(report)
        assert deleted.name == "Monthly report"
        with pytest.raises(NotFoundError):
            lodol.workflows.get(report)

    sent = server.state.headers_seen[0]
    assert sent["User-Agent"].startswith("lodol-python/")
    assert sent["Authorization"] == f"Bearer {KEY}"


def test_paging_through_real_history(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        for client_name in ["A", "B", "C", "D", "E"]:
            lodol.workflows.run("665f", inputs={"client": client_name})

        assert len(list(lodol.runs.list())) == 5
        # Two to a page is three requests, and nothing skipped or repeated.
        before = len(server.state.headers_seen)
        lodol_pages = lodol.runs.list(limit=4)
        assert [r.id for r in lodol_pages] == ["r5", "r4", "r3", "r2"]
        assert len(server.state.headers_seen) - before == 1


def test_your_own_key_starts_a_run_once(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        first = lodol.workflows.run(
            "665f", inputs={"client": "Acme"}, idempotency_key="acme-2026-05"
        )
        again = lodol.workflows.run(
            "665f", inputs={"client": "Acme"}, idempotency_key="acme-2026-05"
        )
        assert first.id == again.id
        assert len(server.state.runs) == 1

        with pytest.raises(UnprocessableEntityError) as reused:
            lodol.workflows.run(
                "665f", inputs={"client": "Other"}, idempotency_key="acme-2026-05"
            )
        assert reused.value.code == "idempotency_key_reused"


def test_a_wrong_key(server: Server) -> None:
    with Lodol(api_key="sk_live_wrong", base_url=address(server)) as lodol:
        with pytest.raises(AuthenticationError, match="Invalid or expired API key"):
            lodol.workflows.list()


def test_a_slow_server_times_out(server: Server) -> None:
    server.state.slow_seconds = 1.0
    with Lodol(
        api_key=KEY, base_url=address(server), timeout=0.2, max_retries=1
    ) as lodol:
        with pytest.raises(APITimeoutError, match="within 0.2 seconds"):
            lodol.workflows.list()
    # The read was retried once: a read can't take effect twice.
    assert len(server.state.headers_seen) == 2


@pytest.mark.anyio
async def test_the_journey_async(server: Server) -> None:
    async with AsyncLodol(api_key=KEY, base_url=address(server)) as lodol:
        run = await lodol.workflows.run("665f", inputs={"client": "Acme"}, wait=True)
        assert run.succeeded

        runs = [r async for r in lodol.runs.list(limit=10)]
        assert [r.id for r in runs] == [run.id]

        pending = [a async for a in lodol.approvals.list()]
        assert len(pending) == 2
        await lodol.approvals.approve("a1")
        assert (await lodol.approvals.get("a1")).status == "approved"


def test_a_slow_start_is_waited_for_not_repeated(server: Server) -> None:
    """Over a real socket: the first attempt times out while the server is
    still starting the run; the retry hears 409 + Retry-After until the
    server can replay it. One run, not a conflict and not two."""
    # Margins wide enough for a busy machine: the first attempt gives up at
    # 0.5s, a second into a 1.5s start, and each 409 comes back at once.
    server.state.start_seconds = 1.5
    server.state.in_flight_retry_after = "0.2"
    with Lodol(api_key=KEY, base_url=address(server), timeout=0.5) as lodol:
        run = lodol.workflows.run("665f", inputs={"client": "Acme"})

    assert run.id == "r1"
    assert len(server.state.runs) == 1


def test_a_workflow_that_starts_itself(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        with pytest.warns(UserWarning, match="it is listening"):
            run = lodol.workflows.run("9b2e", wait=True)
        # Waiting on it later says the same, rather than waiting forever.
        with pytest.warns(UserWarning, match="'New invoices' starts from its own"):
            again = lodol.runs.wait(run.id)

    assert run.status == "running" and run.is_trigger_listener
    assert again.id == run.id and again.is_trigger_listener


def test_listed_runs_say_why_they_failed(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        with pytest.raises(RunFailedError):
            lodol.workflows.run("7a1c", wait=True)
        (listed,) = lodol.runs.list()

    assert listed.error == "Sheet not found" and listed.workflow_name == "Always fails"


def test_a_run_hands_back_its_outputs(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        report = lodol.workflows.get("665f")
        run = lodol.workflows.run(report, inputs={"client": "Acme"}, wait=True)
        (listed,) = lodol.runs.list()

    assert [o.name for o in report.outputs] == ["total"]
    assert run.outputs == {"total": 1250}
    # A list is a summary; the run itself has what it handed back.
    assert listed.outputs == {}


def test_an_error_names_its_request(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        with pytest.raises(NotFoundError) as missing:
            lodol.runs.get("nope")

    error = missing.value
    assert error.code == "not_found"
    assert error.request_id is not None and error.request_id.startswith("req_")
    assert str(error) == f"Execution not found (request ID: {error.request_id})"
    assert error.message == "Execution not found"
