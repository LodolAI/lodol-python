"""The whole journey over a real socket, against the stand-in Developer API
in ``tests/standin.py``."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from lodol import (
    APITimeoutError,
    ApprovalStatus,
    AsyncLodol,
    AuthenticationError,
    BadRequestError,
    ConflictError,
    Lodol,
    NotFoundError,
    PermissionDeniedError,
    RetryOptions,
    RunFailedError,
    RunStatus,
    StepStatus,
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
        # A step that failed and carried on doesn't fail the run.
        _, continued, skipped = run.steps
        assert continued.status == StepStatus.CONTINUED and continued.attempts == 3
        assert continued.error == "Slack didn't answer"
        assert skipped.status == "skipped" and skipped.reason is not None

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
        with pytest.raises(ConflictError) as conflict:
            lodol.approvals.reject(pending[1])
        assert conflict.value.code == "approval_already_answered"
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


def test_approvals_the_key_cant_see_dont_cut_a_list_short(server: Server) -> None:
    """The stand-in, like the server, leaves out requests from workflows the
    key's member can't see after reading a page."""
    server.state.hidden_workflows = {"7a1c"}
    server.state.approvals = [
        {
            "id": f"a{n}",
            "kind": "approval",
            "status": "pending",
            "title": f"Request {n}",
            "execution_id": "r0",
            "workflow_id": "7a1c" if n in (2, 3, 5) else "665f",
        }
        for n in range(6, 0, -1)
    ]
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        assert [a.id for a in lodol.approvals.list(limit=3)] == ["a6", "a4", "a1"]
        assert [a.id for a in lodol.approvals.list()] == ["a6", "a4", "a1"]
        with pytest.raises(NotFoundError):
            lodol.approvals.get("a5")


def test_a_request_no_longer_pending_says_what_happened_to_it(
    server: Server,
) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        run = lodol.workflows.run("665f", inputs={"client": "Acme"})
        server.state.approvals = [
            {"id": "a5", "kind": "approval", "status": "pending", "title": "Stop?"},
            {"id": "a4", "kind": "reply", "status": "pending", "title": "Why?"},
            {"id": "a3", "kind": "approval", "status": "expired", "title": "Late"},
            {
                "id": "a2",
                "kind": "approval",
                "status": "approved",
                "title": "Timed out",
                "decision": {"decision": "approved", "via": "timeout"},
            },
            {"id": "a1", "kind": "approval", "status": "pending", "title": "Fine"},
        ]
        for approval in server.state.approvals:
            approval.update(execution_id="r0", workflow_id="665f")
        server.state.approvals[0]["execution_id"] = run.id
        server.state.approvals[1]["workflow_id"] = "9b2e"
        lodol.runs.stop(run)
        lodol.workflows.delete("9b2e")
        lodol.approvals.approve("a1")

        def refusal(approval_id: str) -> ConflictError:
            with pytest.raises(ConflictError) as refused:
                lodol.approvals.approve(approval_id)
            return refused.value

        withdrawn_by_stop, withdrawn_by_delete = refusal("a5"), refusal("a4")
        assert withdrawn_by_stop.code == "approval_withdrawn"
        assert withdrawn_by_stop.message == (
            "This request was withdrawn. The run was stopped before anyone answered."
        )
        assert withdrawn_by_delete.code == "approval_withdrawn"
        assert refusal("a3").code == "approval_expired"
        timed_out = refusal("a2")
        assert timed_out.code == "approval_expired"
        assert "it was approved automatically" in timed_out.message
        assert refusal("a1").code == "approval_already_answered"
        assert lodol.approvals.get("a5").status == ApprovalStatus.CANCELLED


def test_answering_what_a_run_asks(server: Server) -> None:
    server.state.workflows["4e8d"]["asks"].append("Send the receipt?")
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        run = lodol.workflows.run("4e8d")

        first = lodol.approvals.wait_for(run, timeout=10)
        assert first is not None and first.title == "Pay invoice #4471?"
        assert first.run_id == run.id
        lodol.approvals.approve(first)

        # The run reads awaiting_approval a moment longer: carried on past.
        second = lodol.approvals.wait_for(run, timeout=10)
        assert second is not None and second.title == "Send the receipt?"
        assert [a.id for a in lodol.approvals.list(run=run)] == [second.id]
        assert [a.id for a in lodol.approvals.list(workflow="v4e8d")] == [second.id]
        lodol.approvals.reject(second)

        assert lodol.approvals.wait_for(run, timeout=10) is None
        assert lodol.runs.wait(run, timeout=10).outputs == {"paid": False}
        decided = lodol.approvals.list(run=run, status="decided")
        assert [a.id for a in decided] == [second.id, first.id]


def test_waiting_for_a_run_that_is_stopped_before_it_asks(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        run = lodol.workflows.run("4e8d")
        asked = lodol.approvals.wait_for(run, timeout=10)
        assert asked is not None
        lodol.runs.stop(run)

        with pytest.raises(RunFailedError, match="was stopped"):
            lodol.approvals.wait_for(run, timeout=10)
        assert lodol.approvals.get(asked).status == ApprovalStatus.CANCELLED


def test_waiting_for_an_approval_on_an_older_lodol(server: Server) -> None:
    """Which lists every run's requests, whatever the run asked for."""
    server.state.filters_approvals = False
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        run = lodol.workflows.run("4e8d")

        asked = lodol.approvals.wait_for(run, timeout=10)

        assert asked is not None and asked.run_id == run.id
        assert [a.run_id for a in lodol.approvals.list(run=run)] == [run.id]
        # A workflow can't be told apart: every pending request is listed.
        assert len(list(lodol.approvals.list(workflow="4e8d"))) == 3


def display(text: str) -> dict[str, Any]:
    return {
        "kind": "ExprStmt",
        "expr": {"kind": "DisplayBuiltIn", "value": {"kind": "Literal", "value": text}},
    }


def send_email() -> dict[str, Any]:
    return {
        "kind": "ExprStmt",
        "expr": {
            "kind": "NativeCallExpr",
            "name": "api.request",
            "args": [
                {"kind": "Literal", "value": "POST"},
                {"kind": "Literal", "value": "/actions/library/gmail/send-email"},
                {"kind": "Literal", "value": {"to": "kim@acme.test"}},
            ],
        },
    }


def test_creating_and_changing_a_workflow(server: Server) -> None:
    program = {"kind": "Program", "version": 1, "body": [display("Hello")]}
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        check = lodol.workflows.validate(program)
        assert check.valid and "warning" in check.warnings
        broken = lodol.workflows.validate({"kind": "Program", "body": [{}]})
        assert not broken.valid and broken.error is not None

        created = lodol.workflows.create("Say hello", program, description="Hi")
        assert created.program == program and created.description == "Hi"
        assert created.url is not None and created.url.endswith(created.id)
        assert created.version_id == f"v{created.id}"
        assert created.apps_to_connect == [] and created.trigger_paused is None
        assert lodol.workflows.get(created).name == "Say hello"
        assert lodol.workflows.run(created, wait=True, timeout=10).succeeded

        edited = lodol.workflows.edit(
            created,
            [
                {"op": "replace", "path": "/body/0/expr/value/value", "value": "Hi"},
                {"op": "add", "path": "/body/-", "value": send_email()},
            ],
        )
        assert edited.program is not None
        assert edited.program["body"][0] == display("Hi")
        assert edited.version_id == f"v{created.id}.2"
        assert edited.apps_to_connect == ["Gmail"]
        assert edited.trigger_paused is False
        assert lodol.workflows.update(edited, name="Say hi").name == "Say hi"
        assert lodol.workflows.get(created).program == edited.program

        with pytest.raises(BadRequestError) as invalid:
            lodol.workflows.update(created, program={"kind": "Program", "body": [{}]})
        assert invalid.value.code == "invalid_workflow"
        with pytest.raises(BadRequestError, match="with an empty program") as empty:
            lodol.workflows.update(created, program={"kind": "Program", "body": []})
        assert empty.value.code == "invalid_workflow"
        with pytest.raises(BadRequestError, match="Couldn't apply the patch") as bad:
            lodol.workflows.edit(created, [{"op": "remove", "path": "/body/9"}])
        assert bad.value.code == "invalid_request"
        # Nothing refused changed anything.
        assert lodol.workflows.get(created).program == edited.program


def test_a_saved_workflow_that_was_listening(server: Server) -> None:
    trigger = {
        "kind": "LetStmt",
        "name": "invoice",
        "init": {"kind": "NativeCallExpr", "name": "new_invoice_trigger", "args": []},
    }
    program = {"kind": "Program", "version": 1, "body": [trigger, display("Got one")]}
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        workflow = lodol.workflows.create("New invoices", program)
        assert workflow.starts_from_trigger
        with pytest.warns(UserWarning, match="'New invoices' starts from its own"):
            listening = lodol.workflows.run(workflow, wait=True)

        saved = lodol.workflows.update(workflow, description="Each new invoice")

        assert saved.trigger_paused is True
        assert lodol.runs.get(listening).status == "stopped"
        # Not listening any more, so nothing left to pause.
        assert lodol.workflows.update(workflow, name="Invoices").trigger_paused is False


def test_a_workflow_the_key_may_not_change(server: Server) -> None:
    program = {"kind": "Program", "version": 1, "body": [display("Hello")]}
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        server.state.workflows["665f"]["view_only"] = True
        with pytest.raises(PermissionDeniedError) as view_only:
            lodol.workflows.update("665f", name="Mine now")
        assert view_only.value.code == "item_view_only"
        with pytest.raises(NotFoundError):
            lodol.workflows.edit("nope", [{"op": "remove", "path": "/body/0"}])

        server.state.scopes.discard("workflows:write")
        with pytest.raises(PermissionDeniedError) as missing:
            lodol.workflows.validate(program)
        assert missing.value.code == "missing_scope"
        with pytest.raises(PermissionDeniedError):
            lodol.workflows.create("Say hello", program)


def test_a_slow_save_is_waited_for_not_repeated(server: Server) -> None:
    server.state.start_seconds = 1.5
    server.state.in_flight_retry_after = "0.2"
    program = {"kind": "Program", "version": 1, "body": [display("Hello")]}
    with Lodol(api_key=KEY, base_url=address(server), timeout=0.5) as lodol:
        created = lodol.workflows.create("Say hello", program)

    assert created.id == "w1"
    assert [w for w in server.state.workflows if w.startswith("w")] == ["w1"]


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
        listening = [w.id for w in lodol.workflows.list() if w.starts_from_trigger]
        assert listening == ["9b2e"]
        assert lodol.workflows.get("9b2e").starts_from_trigger
        # Lodol names the workflow it started, so the warning can.
        with pytest.warns(UserWarning, match="'New invoices' starts from its own"):
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


def test_finding_runs(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        report = lodol.workflows.run("665f", inputs={"client": "Acme"}, wait=True)
        for workflow_id in ("7a1c", "5c3d"):
            with pytest.raises(RunFailedError):
                lodol.workflows.run(workflow_id, wait=True)
        queued = lodol.workflows.run("665f", inputs={"client": "Globex"})

        def ids(**filters: Any) -> list[str]:
            return [run.id for run in lodol.runs.list(**filters)]

        assert ids(status="failed") == ["r3", "r2"]
        assert ids(status=["failed", RunStatus.SUCCESS], limit=2) == ["r3", "r2"]
        assert ids(status=["failed", RunStatus.SUCCESS]) == ["r3", "r2", report.id]
        assert ids(status="queued") == [queued.id]
        # By a value in its data, or its workflow's name.
        assert ids(search="globex") == [queued.id]
        assert ids(search="Always fails") == ["r2"]
        # By when they started: a run still queued hasn't.
        assert ids(started_from=dt.date(2026, 10, 13)) == ["r3", "r2", report.id]
        assert ids(started_to=dt.date(2026, 10, 13)) == []
        # They started at 09:00:01 UTC, which is 11:00:01 at UTC+2.
        two_hours_ahead = dt.timezone(dt.timedelta(hours=2))
        assert len(ids(started_from=dt.datetime(2026, 10, 13, 9, 0, 1))) == 3
        assert ids(started_from=dt.datetime(2026, 10, 13, 9, 0, 2)) == []
        at_utc_plus_two = dt.datetime(2026, 10, 13, 11, 0, 1, tzinfo=two_hours_ahead)
        assert len(ids(started_to=at_utc_plus_two)) == 0
        assert len(ids(started_from=at_utc_plus_two)) == 3

        # What the SDK refuses, Lodol would have taken as no filter at all.
        unfiltered = lodol.request("GET", "/executions", params={"status": "faild"})
        assert len(unfiltered["executions"]) == 4
        with pytest.raises(ValueError, match="'faild' isn't a status"):
            lodol.runs.list(status="faild")


def test_retrying_a_failed_run(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        with pytest.raises(RunFailedError) as failed:
            lodol.workflows.run("5c3d", wait=True)
        original = failed.value.run
        assert original.error == "The accounting system didn't answer"
        assert original.retry_options == RetryOptions(True, True)

        retry = lodol.runs.retry(original, wait=True)

        assert retry.succeeded and retry.outputs == {"synced": 12}
        assert retry.retry_of == original.id
        assert retry.retry_options == RetryOptions(False, False)
        # A list doesn't say.
        listed = {run.id: run for run in lodol.runs.list()}
        assert listed[retry.id].retry_of is None
        assert listed[retry.id].retry_options is None

        # A run that succeeded can't be retried.
        with pytest.raises(ConflictError) as refused:
            lodol.runs.retry(retry)
        assert refused.value.code == "run_not_retryable"

        # A run from before runs kept where they failed, only from the start.
        with pytest.raises(RunFailedError) as failed:
            lodol.workflows.run("7a1c", wait=True)
        server.state.runs[0]["kept_where_it_failed"] = False
        assert lodol.runs.get(failed.value.run).retry_options == RetryOptions(
            from_start=True, from_failed_step=False
        )
        with pytest.raises(ConflictError, match="only be retried from the start"):
            lodol.runs.retry(failed.value.run)
        again = lodol.runs.retry(failed.value.run, from_start=True)
        assert again.status == "queued" and again.retry_of == failed.value.run.id

        # The same key starts one retry, however often it's asked.
        first = lodol.runs.retry(original, idempotency_key="retry-once")
        second = lodol.runs.retry(original, idempotency_key="retry-once")
        assert first.id == second.id


def test_stopping_a_run_returns_all_of_it(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        queued = lodol.workflows.run("665f", inputs={"client": "Acme"})
        on_a_step = lodol.workflows.run("665f", inputs={"client": "Globex"})
        assert lodol.runs.get(on_a_step).status == "running"

        stopped = lodol.runs.stop(queued)
        stopping = lodol.runs.stop(on_a_step)

        assert stopped.status == "stopped" and stopped.completed_at is not None
        assert stopping.status == "stopping" and stopping.completed_at is None
        for run in (stopped, stopping):
            assert run.workflow_id == "665f" and run.version_id == "v665f"
            assert run.workflow_name == "Monthly report"
            assert run.retry_of is None
            assert run.retry_options == RetryOptions()
        assert lodol.runs.get(on_a_step).status == "stopped"
        # Asked again: returned as it finished.
        assert lodol.runs.stop(on_a_step).status == "stopped"


def test_an_error_names_its_request(server: Server) -> None:
    with Lodol(api_key=KEY, base_url=address(server)) as lodol:
        with pytest.raises(NotFoundError) as missing:
            lodol.runs.get("nope")

    error = missing.value
    assert error.code == "not_found"
    assert error.request_id is not None and error.request_id.startswith("req_")
    assert str(error) == f"Execution not found (request ID: {error.request_id})"
    assert error.message == "Execution not found"
