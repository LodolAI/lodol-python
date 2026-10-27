"""Workflows: listing, looking one up, running one, deleting one."""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Callable

import httpx2
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
    WorkflowCheck,
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


def test_a_workflow_says_whether_it_starts_from_its_own_trigger(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(
        respond(
            200,
            {
                "workflows": [
                    workflow_json(id="9b2e", starts_from_trigger=True),
                    workflow_json(starts_from_trigger=False),
                    # From a Lodol that doesn't say.
                    workflow_json(id="7a1c"),
                ]
            },
        ),
        respond(200, workflow_json(id="9b2e", starts_from_trigger=True)),
    )

    listener, report, older = client.workflows.list()

    assert listener.starts_from_trigger
    assert not report.starts_from_trigger and not older.starts_from_trigger
    assert client.workflows.get("9b2e").starts_from_trigger


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


@pytest.mark.parametrize(
    ("workflow", "workflow_name", "named"),
    [
        # Lodol names it.
        ("665f", "New invoices", "'New invoices'"),
        # An older Lodol doesn't; the workflow in hand does.
        (Workflow(id="665f", name="Invoices"), None, "'Invoices'"),
        # Lodol's name is the current one.
        (Workflow(id="665f", name="Invoices"), "New invoices", "'New invoices'"),
        # Nothing names it.
        ("665f", None, "This workflow"),
        (Workflow(id="665f", name=""), None, "This workflow"),
    ],
)
def test_the_listening_warning_names_the_workflow(
    client: Lodol,
    api: FakeAPI,
    workflow: str | Workflow,
    workflow_name: str | None,
    named: str,
) -> None:
    started = run_json(status="running", is_trigger_listener=True)
    if workflow_name is not None:
        started["workflow_name"] = workflow_name
    api.queue(respond(202, started))

    with pytest.warns(UserWarning) as warned:
        run = client.workflows.run(workflow, wait=True)

    assert str(warned[0].message).startswith(f"{named} starts from its own trigger")
    # Pointed at the caller's own line.
    assert warned[0].filename == __file__
    # The run is as Lodol gave it.
    assert run.workflow_name == workflow_name


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


# ── Creating and changing workflows ──────────────────────────────────────

SAY_HELLO = {
    "kind": "Program",
    "version": 1,
    "body": [
        {
            "kind": "ExprStmt",
            "expr": {
                "kind": "DisplayBuiltIn",
                "value": {"kind": "Literal", "value": "Hello"},
            },
        }
    ],
}


def saved_json(**fields: object) -> dict[str, object]:
    # As a save answers: the workflow as reading it gives it, and more.
    saved: dict[str, object] = {
        "name": "Say hello",
        "description": "",
        "program": SAY_HELLO,
        "publish_state": "published",
        "starts_from_trigger": False,
        "inputs": [],
        "outputs": [],
        "version_id": "6690",
        "url": "https://app.lodol.com/create?programId=665f",
        "apps_to_connect": [],
    }
    return workflow_json(**{**saved, **fields})


def test_validate(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            {
                "valid": True,
                "preview": {"steps": [{"index": 0}]},
                "actions": ["/actions/library/slack/send-message"],
                "has_trigger": True,
                "steps": [{"index": 0, "summary": "Show Hello"}],
                "warnings": {"missing_start": "It never starts."},
            },
        )
    )

    check = client.workflows.validate(SAY_HELLO)

    assert api.sent() == ("POST", "/workflows/validate", {})
    assert api.body() == {"program": SAY_HELLO}
    # It saves nothing, so it needs no key.
    assert "Idempotency-Key" not in api.last.headers
    assert check == WorkflowCheck(
        valid=True,
        warnings={"missing_start": "It never starts."},
        starts_from_trigger=True,
        actions=["/actions/library/slack/send-message"],
    )
    assert check.raw["preview"] == {"steps": [{"index": 0}]}


def test_validate_a_program_that_cant_be_saved(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"valid": False, "error": "Step 1 has no action."}))

    check = client.workflows.validate({"kind": "Program", "body": [{}]})

    assert not check.valid and check.error == "Step 1 has no action."
    assert check.warnings == {} and check.actions == []


def test_validate_is_sent_again_after_passing_trouble(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """It changes nothing, so it's sent again as a read would be."""
    api.queue(
        httpx2.ReadTimeout("slow"),
        respond(502),
        respond(200, {"valid": False, "error": "x"}),
    )

    assert not client.workflows.validate(SAY_HELLO).valid
    assert len(api.requests) == 3


def test_create(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(201, saved_json(apps_to_connect=["Gmail"], note="Filled in")))

    workflow = client.workflows.create(
        "Say hello", SAY_HELLO, description="A first workflow"
    )

    assert api.sent() == ("POST", "/workflows", {})
    assert api.body() == {
        "name": "Say hello",
        "description": "A first workflow",
        "program": SAY_HELLO,
    }
    assert api.last.headers["Idempotency-Key"].startswith("lodol-workflow-create-")
    assert workflow.id == "665f" and workflow.program == SAY_HELLO
    assert workflow.version_id == "6690"
    assert workflow.url == "https://app.lodol.com/create?programId=665f"
    assert workflow.apps_to_connect == ["Gmail"]
    # Only an edit says whether it paused a trigger.
    assert workflow.trigger_paused is None
    assert workflow.raw["note"] == "Filled in"


def test_a_create_is_sent_again_with_the_same_key(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(httpx2.ReadTimeout("slow"), respond(201, saved_json()))

    client.workflows.create("Say hello", SAY_HELLO)

    first, second = api.requests
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert first.content == second.content


def test_a_program_lodol_cant_save_is_refused(client: Lodol, api: FakeAPI) -> None:
    message = (
        "The workflow is invalid and was not saved: Step 1 has no action. POST "
        "/api/v1/workflows/validate checks a program without saving it."
    )
    api.queue(respond(400, {"error": message, "error_code": "invalid_workflow"}))

    with pytest.raises(BadRequestError) as exc:
        client.workflows.create("Say hello", SAY_HELLO)

    assert exc.value.code == "invalid_workflow"
    assert str(exc.value) == message
    assert len(api.requests) == 1


def test_update_sends_only_what_changes(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, saved_json(name="Say hi", trigger_paused=False)),
        respond(200, saved_json(trigger_paused=True)),
    )

    renamed = client.workflows.update(Workflow(id="665f", name="x"), name="Say hi")
    replaced = client.workflows.update(
        "665f", program=SAY_HELLO, description="Says hello"
    )

    assert api.sent(0) == ("PUT", "/workflows/665f", {})
    assert api.body(0) == {"name": "Say hi"}
    assert api.body(1) == {"program": SAY_HELLO, "description": "Says hello"}
    assert (
        api.requests[0].headers["Idempotency-Key"].startswith("lodol-workflow-update-")
    )
    assert renamed.name == "Say hi" and renamed.trigger_paused is False
    assert replaced.trigger_paused is True


def test_update_with_nothing_to_change_is_refused(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="Nothing to change"):
        client.workflows.update("665f")

    assert api.requests == []


def test_edit(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, saved_json(name="Say hello again", trigger_paused=False)))
    operations = [
        {"op": "replace", "path": "/body/0/expr/value/value", "value": "Hi"},
        {"op": "remove", "path": "/body/1"},
    ]

    workflow = client.workflows.edit("665f", operations, name="Say hello again")

    assert api.sent() == ("PATCH", "/workflows/665f", {})
    assert api.body() == {"operations": operations, "name": "Say hello again"}
    assert api.last.headers["Idempotency-Key"].startswith("lodol-workflow-edit-")
    assert workflow.name == "Say hello again"


@pytest.mark.parametrize("method", ["PUT", "PATCH"])
def test_a_change_is_sent_again_with_the_same_key(
    client: Lodol, api: FakeAPI, clock: FakeClock, method: str
) -> None:
    """Lodol takes a key on these, so a retry can't save twice."""
    api.queue(respond(503), respond(200, saved_json()))

    if method == "PUT":
        client.workflows.update("665f", name="Say hello")
    else:
        client.workflows.edit("665f", [{"op": "remove", "path": "/body/1"}])

    first, second = api.requests
    assert first.method == second.method == method
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]


@pytest.mark.parametrize(
    ("call", "error", "complaint"),
    [
        (
            lambda c: c.workflows.validate([SAY_HELLO]),
            TypeError,
            r"program must be a dict .*, not list",
        ),
        (
            lambda c: c.workflows.create("Say hello", "steps"),
            TypeError,
            "program must be a dict",
        ),
        (lambda c: c.workflows.create(42, SAY_HELLO), TypeError, "name must be text"),
        (lambda c: c.workflows.create("  ", SAY_HELLO), ValueError, "name is empty"),
        (
            lambda c: c.workflows.create("x", SAY_HELLO, description=None),
            TypeError,
            "description must be text, not NoneType",
        ),
        (
            lambda c: c.workflows.update("665f", program=[SAY_HELLO]),
            TypeError,
            "program must be a dict",
        ),
        (
            lambda c: c.workflows.update(Run(id="r", status="x"), name="x"),
            TypeError,
            "Expected a workflow id or a Workflow, not Run",
        ),
        (
            lambda c: c.workflows.edit("665f", {"op": "remove", "path": "/body/0"}),
            TypeError,
            "operations must be a list of JSON Patch operations, not dict",
        ),
        (lambda c: c.workflows.edit("665f", []), ValueError, "operations is empty"),
        (
            lambda c: c.workflows.edit("665f", [{"op": "remove"}, "/body/0"]),
            TypeError,
            r"operations\[1\] must be a dict",
        ),
        (
            lambda c: c.workflows.edit("665f", [{"op": "remove"}], name=""),
            ValueError,
            "name is empty",
        ),
    ],
)
def test_a_save_is_checked_before_anything_is_sent(
    client: Lodol,
    api: FakeAPI,
    call: Callable[[Lodol], object],
    error: type[Exception],
    complaint: str,
) -> None:
    with pytest.raises(error, match=complaint):
        call(client)

    assert api.requests == []


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("API key lacks required scope(s)", "missing_scope"),
        ("You have view-only access to this workflow", "item_view_only"),
    ],
)
def test_a_key_that_may_not_change_it(
    client: Lodol, api: FakeAPI, message: str, code: str
) -> None:
    api.queue(respond(403, {"error": message, "error_code": code}))

    with pytest.raises(PermissionDeniedError) as exc:
        client.workflows.update("665f", name="x")

    assert exc.value.code == code and str(exc.value) == message
