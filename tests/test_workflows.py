"""Workflows: listing, looking one up, running one, deleting one."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid

import pytest

from lodol import (
    APIResponseValidationError,
    BadRequestError,
    DeletedWorkflow,
    Lodol,
    NotFoundError,
    PermissionDeniedError,
    Run,
    Workflow,
    WorkflowInput,
)
from tests.conftest import FakeAPI, FakeClock, respond, run_json, workflow_json


def test_list(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            {
                "workflows": [
                    workflow_json(
                        description="Builds a client's report",
                        created_at="2026-01-15T10:30:00+00:00",
                        last_run_at=None,
                        publish_state="published",
                        inputs=[{"name": "client", "type": "text", "required": True}],
                    ),
                    workflow_json(id="7a1c", name="Draft", publish_state="draft"),
                ]
            },
        )
    )

    workflows = client.workflows.list()

    assert api.sent() == ("GET", "/workflows", {})
    report, draft = workflows
    assert report.id == "665f"
    assert report.description == "Builds a client's report"
    assert report.created_at == dt.datetime(2026, 1, 15, 10, 30, tzinfo=dt.timezone.utc)
    assert report.last_run_at is None
    assert report.inputs == [WorkflowInput(name="client", type="text", required=True)]
    assert not report.is_draft
    assert draft.is_draft


def test_a_workflow_from_before_drafts_runs(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"workflows": [workflow_json()]}))

    assert not client.workflows.list()[0].is_draft


@pytest.mark.parametrize(
    ("body", "complaint"),
    [
        (["not", "an", "object"], "GET /workflows returned list, expected object"),
        ({"workflows": "nope"}, "field 'workflows' returned str, expected list"),
        ({"workflows": ["nope"]}, "field 'workflows'[0] returned str, expected object"),
    ],
)
def test_list_refuses_an_unexpected_shape(
    client: Lodol, api: FakeAPI, body: object, complaint: str
) -> None:
    api.queue(respond(200, body))

    with pytest.raises(APIResponseValidationError, match=complaint.replace("[", r"\[")):
        client.workflows.list()


def test_get(client: Lodol, api: FakeAPI) -> None:
    program = {"kind": "Program", "body": []}
    api.queue(respond(200, workflow_json(program=program)))

    workflow = client.workflows.get("665f")

    assert api.sent() == ("GET", "/workflows/665f", {})
    assert workflow.program == program
    # The steps can be thousands of lines: they stay out of the repr.
    assert "Program" not in repr(workflow)


def test_get_takes_the_workflow_itself(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, workflow_json()))

    client.workflows.get(Workflow(id="665f", name="Monthly report"))

    assert api.sent()[1] == "/workflows/665f"


def test_ids_are_escaped(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, workflow_json()))

    client.workflows.get("a/b c")

    assert str(api.last.url).endswith("/workflows/a%2Fb%20c")


def test_a_run_is_not_a_workflow(client: Lodol) -> None:
    with pytest.raises(
        TypeError, match="Expected a workflow id or a Workflow, not Run"
    ):
        client.workflows.get(Run(id="683b", status="success"))  # type: ignore[arg-type]


def test_an_empty_id_is_refused_before_any_request(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="workflow id is empty"):
        client.workflows.get("  ")

    assert api.requests == []


def test_get_unknown(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(404, {"error": "Workflow not found"}))

    with pytest.raises(NotFoundError, match="Workflow not found"):
        client.workflows.get("665f")


def test_run_starts_a_run(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            202,
            run_json(version_id="6683", created_at="2026-01-15T10:30:00+00:00"),
        )
    )

    run = client.workflows.run("665f")

    assert api.sent() == ("POST", "/workflows/665f/run-async", {})
    # A run without inputs sends no body at all.
    assert api.body() is None
    assert api.last.headers["Idempotency-Key"].startswith("lodol-workflow-run-")
    assert run == Run(
        id="683b",
        status="queued",
        workflow_id="665f",
        version_id="6683",
        created_at=dt.datetime(2026, 1, 15, 10, 30, tzinfo=dt.timezone.utc),
    )
    assert not run.finished


def test_every_run_gets_its_own_key(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()), respond(202, run_json()))

    client.workflows.run("665f")
    client.workflows.run("665f")

    first, second = (request.headers["Idempotency-Key"] for request in api.requests)
    assert first != second


def test_run_with_your_own_key(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()))

    client.workflows.run("665f", idempotency_key="monthly-report:acme:2026-05")

    assert api.last.headers["Idempotency-Key"] == "monthly-report:acme:2026-05"


def test_run_sends_inputs(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()))

    client.workflows.run("665f", inputs={"client": "Acme", "count": 2, "tags": ["a"]})

    assert api.body() == {"inputs": {"client": "Acme", "count": 2, "tags": ["a"]}}
    assert api.last.headers["Content-Type"] == "application/json"


def test_run_sends_dates_and_decimals_the_way_lodol_reads_them(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(respond(202, run_json()))
    ident = uuid.UUID("12345678-1234-5678-1234-567812345678")

    client.workflows.run(
        "665f",
        inputs={
            "month": dt.date(2026, 5, 1),
            "due": dt.datetime(2026, 5, 31, 17, 0, tzinfo=dt.timezone.utc),
            "amount": decimal.Decimal("1250.10"),
            "record": ident,
        },
    )

    assert api.body() == {
        "inputs": {
            "month": "2026-05-01",
            "due": "2026-05-31T17:00:00+00:00",
            "amount": "1250.10",
            "record": str(ident),
        }
    }


def test_run_names_an_input_that_cant_be_sent(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(TypeError, match=r"Input 'seen': a set can't be sent to Lodol"):
        client.workflows.run("665f", inputs={"client": "Acme", "seen": {1, 2}})

    assert api.requests == []


def test_run_refuses_inputs_that_arent_a_dict(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(TypeError, match="dict of input names to values, not list"):
        client.workflows.run("665f", inputs=[("client", "Acme")])  # type: ignore[arg-type]

    assert api.requests == []


def test_run_refuses_input_names_that_arent_text(client: Lodol) -> None:
    with pytest.raises(TypeError, match="Input names must be text, not int"):
        client.workflows.run("665f", inputs={1: "Acme"})  # type: ignore[dict-item]


def test_a_timeout_needs_wait(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="needs wait=True"):
        client.workflows.run("665f", timeout=60)

    assert api.requests == []


def test_inputs_the_workflow_refuses_name_the_input(
    client: Lodol, api: FakeAPI
) -> None:
    message = (
        "'Monthly report' has no input named 'clinet'. Its inputs are: client, month."
    )
    api.queue(respond(400, {"error": message}))

    with pytest.raises(BadRequestError) as exc:
        client.workflows.run("665f", inputs={"clinet": "Acme"})

    assert str(exc.value) == message
    assert len(api.requests) == 1


def test_run_and_wait(client: Lodol, api: FakeAPI, clock: FakeClock) -> None:
    steps = [{"index": 0, "status": "completed", "result": "Sent"}]
    api.queue(
        respond(202, run_json()),
        respond(200, run_json(status="running")),
        respond(200, run_json(status="success")),
        respond(200, run_json(status="success", steps=steps)),
    )

    run = client.workflows.run("665f", wait=True, timeout=600)

    assert run.succeeded
    assert run.steps is not None and run.steps[0].result == "Sent"
    assert [api.sent(i)[0:2] for i in range(4)] == [
        ("POST", "/workflows/665f/run-async"),
        ("GET", "/executions/683b"),
        ("GET", "/executions/683b"),
        ("GET", "/executions/683b"),
    ]
    # Polls are light; the step results come once, at the end. A run just
    # started is certainly still queued, so the first check waits a second.
    assert api.sent(1)[2] == {} and api.sent(3)[2] == {"include_step_results": "true"}
    assert clock.sleeps == [1.0, 1.5]


def test_run_takes_the_workflow_itself(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()))

    client.workflows.run(Workflow(id="665f", name="Monthly report"))

    assert api.sent()[1] == "/workflows/665f/run-async"


def test_delete(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"id": "665f", "name": "Old report", "deleted": True}))

    deleted = client.workflows.delete("665f")

    assert deleted == DeletedWorkflow(id="665f", name="Old report")
    assert api.sent() == ("DELETE", "/workflows/665f", {})
    # The API takes no key here: a second delete would just say it's gone.
    assert "Idempotency-Key" not in api.last.headers
    assert api.body() is None


@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (403, "API key lacks required scope(s)", PermissionDeniedError),
        (
            403,
            "The member this API key acts for is not allowed to delete workflows "
            "in this workspace",
            PermissionDeniedError,
        ),
        (404, "Workflow not found", NotFoundError),
    ],
)
def test_delete_refusals_carry_the_servers_words(
    client: Lodol, api: FakeAPI, status: int, message: str, error_type: type[Exception]
) -> None:
    api.queue(respond(status, {"error": message}))

    with pytest.raises(error_type) as exc:
        client.workflows.delete("665f")

    assert str(exc.value) == message


@pytest.mark.parametrize("timeout", [-5, "60"])
def test_a_bad_timeout_is_refused_before_the_run_starts(
    client: Lodol, api: FakeAPI, timeout: object
) -> None:
    with pytest.raises((ValueError, TypeError), match="timeout"):
        client.workflows.run("665f", wait=True, timeout=timeout)  # type: ignore[arg-type]

    assert api.requests == []


def test_a_workflow_that_starts_itself_isnt_waited_for(
    client: Lodol, api: FakeAPI
) -> None:
    """Running it sets it listening, which never finishes: waiting would
    never end, so it returns at once and says why."""
    api.queue(respond(202, run_json(status="running", is_trigger_listener=True)))

    with pytest.warns(
        UserWarning, match=r"no end to wait for: it is listening \(run 683b\)"
    ):
        run = client.workflows.run("665f", wait=True)

    assert run.status == "running"
    assert len(api.requests) == 1
