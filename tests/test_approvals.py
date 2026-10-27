"""Approvals: seeing what runs are waiting on, and answering them."""

from __future__ import annotations

import datetime as dt

import httpx2
import pytest

from lodol import (
    Approval,
    ApprovalStatus,
    BadRequestError,
    ConflictError,
    Decision,
    Lodol,
    NotFoundError,
    PermissionDeniedError,
    Run,
    RunFailedError,
    WaitTimeoutError,
    Workflow,
)
from tests.conftest import (
    ApprovalsServer,
    FakeAPI,
    FakeClock,
    approval_json,
    respond,
    run_json,
)


def test_list_pending_by_default(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            {
                "approvals": [
                    approval_json(
                        details="Invoice #4471 for $1,250.10",
                        step_index=3,
                        created_at="2026-10-13T09:00:00+00:00",
                        expires_at="2026-10-14T09:00:00+00:00",
                        approvers=[{"type": "user", "email": "kim@acme.test"}],
                    )
                ],
                "next_before": None,
            },
        )
    )

    (approval,) = client.approvals.list()

    assert api.sent() == ("GET", "/approvals", {"status": "pending", "limit": "100"})
    assert approval.id == "a1"
    assert approval.kind == "approval"
    assert approval.pending
    assert approval.title == "Send the invoice?"
    assert approval.details == "Invoice #4471 for $1,250.10"
    assert approval.run_id == "683b"
    assert approval.step_index == 3
    assert approval.expires_at == dt.datetime(2026, 10, 14, 9, tzinfo=dt.timezone.utc)
    assert approval.decision is None
    # What the SDK doesn't model is still there.
    assert approval.raw["approvers"] == [{"type": "user", "email": "kim@acme.test"}]


def test_list_pages_with_the_cursor_the_api_gives(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, {"approvals": [approval_json(id="a2")], "next_before": "a2"}),
        respond(200, {"approvals": [approval_json(id="a1")], "next_before": None}),
    )

    ids = [approval.id for approval in client.approvals.list(status="all")]

    assert ids == ["a2", "a1"]
    assert api.sent(1)[2] == {"status": "all", "limit": "100", "before": "a2"}


def test_list_carries_on_past_a_page_the_key_cant_see(
    client: Lodol, api: FakeAPI
) -> None:
    """Lodol leaves out requests from workflows the key's member can't see
    after reading a page, so a page can come back empty and not be the last."""
    api.queue(
        respond(200, {"approvals": [approval_json(id="a4")], "next_before": "a3"}),
        respond(200, {"approvals": [], "next_before": "a2"}),
        respond(200, {"approvals": [approval_json(id="a1")], "next_before": None}),
    )

    ids = [approval.id for approval in client.approvals.list()]

    assert ids == ["a4", "a1"]
    assert api.sent(2)[2]["before"] == "a2"
    assert len(api.requests) == 3


def hidden_and_shown() -> ApprovalsServer:
    # Newest first; the requests from 7a1c are from a workflow the key's
    # member can't see.
    return ApprovalsServer(
        [
            approval_json(id="a6"),
            approval_json(id="a5", workflow_id="7a1c"),
            approval_json(id="a4"),
            approval_json(id="a3"),
            approval_json(id="a2", workflow_id="7a1c"),
            approval_json(id="a1"),
        ],
        hidden_workflows={"7a1c"},
    )


def test_a_limit_counts_only_what_the_key_can_see(client: Lodol, api: FakeAPI) -> None:
    """With a limit, each page asks for only as many as are still wanted, so
    the last page asked for can be one the key can't see at all: listing
    carries on past it rather than coming up short."""
    api.queue(*[hidden_and_shown()] * 5)

    ids = [approval.id for approval in client.approvals.list(limit=4)]

    assert ids == ["a6", "a4", "a3", "a1"]
    assert [api.sent(i)[2].get("limit") for i in range(len(api.requests))] == [
        "4",
        "1",
        "1",
    ]


def test_every_request_the_key_can_see_is_listed(client: Lodol, api: FakeAPI) -> None:
    api.queue(*[hidden_and_shown()] * 5)

    ids = [approval.id for approval in client.approvals.list(status="all")]

    assert ids == ["a6", "a4", "a3", "a1"]


def test_list_refuses_an_unknown_status(client: Lodol, api: FakeAPI) -> None:
    with pytest.raises(ValueError, match="'pending', 'decided', 'all'"):
        client.approvals.list(status="approved")

    assert api.requests == []


def test_get_a_decided_request(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            approval_json(
                status="approved",
                decision={
                    "decision": "approved",
                    "comment": "Checked against the PO",
                    "reply": "",
                    "decided_by": {"type": "api", "name": "API key 'Billing sync'"},
                    "decided_at": "2026-10-13T09:05:00+00:00",
                    "via": "api",
                },
            ),
        )
    )

    approval = client.approvals.get("a1")

    assert api.sent() == ("GET", "/approvals/a1", {})
    assert approval.status == ApprovalStatus.APPROVED
    assert not approval.pending
    assert approval.decision == Decision(
        decision="approved",
        comment="Checked against the PO",
        decided_by="API key 'Billing sync'",
        decided_at=dt.datetime(2026, 10, 13, 9, 5, tzinfo=dt.timezone.utc),
        via="api",
    )


def test_get_unknown(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(404, {"error": "Approval request not found"}))

    with pytest.raises(NotFoundError):
        client.approvals.get("a1")


def test_approve(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, approval_json(status="approved")))

    approval = client.approvals.approve("a1", comment="Checked against the PO")

    assert api.sent() == ("POST", "/approvals/a1/decision", {})
    assert api.body() == {"decision": "approved", "comment": "Checked against the PO"}
    assert api.last.headers["Idempotency-Key"].startswith("lodol-approval-answer-")
    assert approval.status == "approved"


def test_reject_without_a_comment(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, approval_json(status="rejected")))

    client.approvals.reject(Approval(id="a1", kind="approval", status="pending"))

    assert api.body() == {"decision": "rejected"}


def test_reply(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, approval_json(kind="reply", status="replied")))

    client.approvals.reply("a1", "The PO number is 4471")

    assert api.body() == {"reply": "The PO number is 4471"}


def test_a_reply_request_cant_be_approved(client: Lodol, api: FakeAPI) -> None:
    request = Approval(id="a1", kind="reply", status="pending")

    with pytest.raises(ValueError, match=r"asks for a written reply.*approvals.reply"):
        client.approvals.approve(request)

    assert api.requests == []


def test_a_decision_request_cant_be_replied_to(client: Lodol, api: FakeAPI) -> None:
    request = Approval(id="a1", kind="approval", status="pending")

    with pytest.raises(ValueError, match=r"asks for a decision.*approve\(\)"):
        client.approvals.reply(request, "Sure")

    assert api.requests == []


def test_a_reply_must_be_text(client: Lodol) -> None:
    with pytest.raises(TypeError, match="must be text, not int"):
        client.approvals.reply("a1", 4471)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("approval_already_answered", "This request has already been decided."),
        ("approval_expired", "Nobody decided this request in time, so it has closed."),
        (
            "approval_expired",
            "Nobody decided this request in time, so it was rejected automatically.",
        ),
        ("approval_withdrawn", "This request was withdrawn. The run was stopped."),
    ],
)
def test_answering_a_settled_request_says_what_happened_to_it(
    client: Lodol, api: FakeAPI, code: str, message: str
) -> None:
    api.queue(respond(409, {"error": message, "error_code": code}))

    with pytest.raises(ConflictError) as exc:
        client.approvals.approve("a1")

    assert exc.value.code == code
    assert str(exc.value) == message
    # Nothing about the request changes by asking again.
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (
            403,
            "This API key belongs to someone who prepared the item, so it can't decide it.",
            PermissionDeniedError,
        ),
        (400, "Write a reply before sending it.", BadRequestError),
    ],
)
def test_refusals_carry_the_servers_words(
    client: Lodol, api: FakeAPI, status: int, message: str, error_type: type[Exception]
) -> None:
    api.queue(respond(status, {"error": message}))

    with pytest.raises(error_type, match=message):
        client.approvals.approve("a1")


# ── One run's or one workflow's ──────────────────────────────────────────


def test_list_one_runs_requests(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"approvals": [approval_json()], "next_before": None}))

    (approval,) = client.approvals.list(run=Run(id="683b", status="awaiting_approval"))

    assert api.sent()[2] == {
        "status": "pending",
        "execution_id": "683b",
        "limit": "100",
    }
    assert approval.run_id == "683b"


def test_list_one_workflows_requests_of_any_status(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"approvals": [approval_json()], "next_before": None}))

    list(client.approvals.list(workflow=Workflow(id="665f", name="x"), status="all"))

    assert api.sent()[2] == {"status": "all", "workflow_id": "665f", "limit": "100"}


def test_an_older_lodol_that_lists_every_runs_requests(
    client: Lodol, api: FakeAPI
) -> None:
    """It ignores the run asked for, so the SDK keeps only that run's. It
    can't do the same for a workflow, which may be named by a version's id."""
    page = {
        "approvals": [
            approval_json(id="a3", execution_id="684c"),
            approval_json(id="a2", execution_id="683b"),
            approval_json(id="a1", execution_id="684c", workflow_id="7a1c"),
        ],
        "next_before": None,
    }
    api.queue(respond(200, page), respond(200, page))

    assert [a.id for a in client.approvals.list(run="683b")] == ["a2"]
    assert len(list(client.approvals.list(workflow="v665f"))) == 3


def test_a_filter_is_checked_before_anything_is_sent(
    client: Lodol, api: FakeAPI
) -> None:
    with pytest.raises(TypeError, match="Expected a run id or a Run, not Workflow"):
        client.approvals.list(run=Workflow(id="665f", name="x"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="workflow id is empty"):
        client.approvals.list(workflow=" ")

    assert api.requests == []


def test_a_filtered_list_says_what_it_is(client: Lodol) -> None:
    assert repr(client.approvals.list(run="683b", workflow="665f", limit=2)) == (
        "<Paginator: up to 2 pending approval requests from run 683b from "
        "workflow 665f, newest first; loop over it to fetch them>"
    )


# ── Waiting for a run to ask ─────────────────────────────────────────────


def asking(
    *items: dict[str, object], next_before: str | None = None
) -> httpx2.Response:
    return respond(200, {"approvals": list(items), "next_before": next_before})


def test_wait_for_returns_the_request_once_the_run_asks(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(200, run_json(status="running")),
        respond(200, run_json(status="awaiting_approval")),
        asking(approval_json()),
    )

    approval = client.approvals.wait_for("683b")

    assert approval is not None and approval.id == "a1" and approval.pending
    assert [api.sent(i)[1] for i in range(3)] == [
        "/executions/683b",
        "/executions/683b",
        "/approvals",
    ]
    assert api.sent(2)[2] == {
        "status": "pending",
        "execution_id": "683b",
        "limit": "100",
    }
    # Checked as runs.wait checks; the run's requests looked for right after.
    assert clock.sleeps == [1.0]


def test_wait_for_carries_on_until_the_next_request_is_there(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """Right after an answer, the run still reads awaiting_approval for a
    moment, and its next request isn't there yet."""
    api.queue(
        respond(200, run_json(status="awaiting_approval")),
        asking(),
        respond(200, run_json(status="running")),
        respond(200, run_json(status="awaiting_approval")),
        asking(approval_json(id="a2", title="Send the receipt?")),
    )

    approval = client.approvals.wait_for("683b")

    assert approval is not None and approval.id == "a2"
    # The checks on the run back off as runs.wait's do.
    assert clock.sleeps == [1.0, 1.5]


def test_wait_for_keeps_to_the_runs_own_requests(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """An older Lodol lists every run's requests, a page at a time."""
    api.queue(
        respond(200, run_json(status="awaiting_approval")),
        asking(approval_json(id="a9", execution_id="684c"), next_before="a9"),
        asking(approval_json(id="a8", execution_id="683b")),
    )

    approval = client.approvals.wait_for("683b")

    assert approval is not None and approval.id == "a8"
    assert api.sent(2)[2]["before"] == "a9"


def test_wait_for_a_run_that_succeeds_without_asking(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(respond(200, run_json(status="success")))

    assert client.approvals.wait_for("683b") is None
    # Nothing more: not its step results, nor its requests.
    assert len(api.requests) == 1


def test_wait_for_a_run_that_fails_first(client: Lodol, api: FakeAPI) -> None:
    steps = [{"index": 0, "status": "failed", "error": "Sheet not found"}]
    api.queue(
        respond(200, run_json(status="failed", error="Sheet not found")),
        respond(200, run_json(status="failed", error="Sheet not found", steps=steps)),
    )

    with pytest.raises(RunFailedError, match="Run 683b failed: Sheet not found") as exc:
        client.approvals.wait_for("683b")

    # With its step results, as runs.wait gives a failed run.
    assert exc.value.run.steps is not None
    assert api.sent(1)[2] == {"include_step_results": "true"}


def test_wait_for_a_run_stopped_first(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(200, run_json(status="stopped")),
        *[httpx2.ConnectError("refused")] * 100,
    )

    with pytest.raises(RunFailedError, match="was stopped before it finished"):
        client.approvals.wait_for("683b")


def test_wait_for_gives_up_at_its_timeout(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        *[respond(200, run_json(status="awaiting_approval")), asking()] * 5,
    )

    with pytest.raises(WaitTimeoutError) as exc:
        client.approvals.wait_for(Run(id="683b", status="queued"), timeout=3)

    assert "after 3 seconds: it is waiting for someone to answer" in str(exc.value)
    assert sum(clock.sleeps) == pytest.approx(3.0)


def test_wait_for_rides_out_trouble_when_looking(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    limited = respond(429, {"error": "Rate limit exceeded."}, {"Retry-After": "30"})
    api.queue(
        respond(200, run_json(status="awaiting_approval")),
        httpx2.ConnectError("refused"),
        limited,
        asking(approval_json()),
    )

    approval = client.approvals.wait_for("683b")

    assert approval is not None
    # The failed look is tried again, not the run's check.
    assert [api.sent(i)[1] for i in range(1, 4)] == ["/approvals"] * 3
    assert 30.0 in clock.sleeps


def test_wait_for_paces_every_request_to_the_allowance(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """On the Free plan's 5 requests a minute, no request comes sooner than
    12 seconds after the one before, looking for the run's request too."""
    headers = {
        "X-RateLimit-Limit": "5",
        "X-RateLimit-Remaining": "4",
        "X-RateLimit-Reset": "50",
    }
    api.queue(
        respond(200, run_json(status="running"), headers),
        respond(200, run_json(status="awaiting_approval"), headers),
        respond(200, {"approvals": [], "next_before": None}, headers),
        respond(200, run_json(status="awaiting_approval"), headers),
        respond(200, {"approvals": [approval_json()], "next_before": None}, headers),
    )

    assert client.approvals.wait_for("683b") is not None

    assert clock.sleeps == [12.0, 12.0, 12.0, 12.0]


def test_wait_for_a_listening_workflow_returns_none(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(
        respond(
            200,
            run_json(
                status="running", is_trigger_listener=True, workflow_name="New invoices"
            ),
        )
    )

    with pytest.warns(UserWarning, match="'New invoices' starts from its own") as w:
        assert client.approvals.wait_for("683b") is None

    assert len(api.requests) == 1
    # Pointed at the caller's own line.
    assert w[0].filename == __file__


def test_wait_for_is_checked_before_anything_is_sent(
    client: Lodol, api: FakeAPI
) -> None:
    with pytest.raises(TypeError, match="Expected a run id or a Run, not Approval"):
        client.approvals.wait_for(Approval(id="a1", kind="approval", status="pending"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout must be 0 or more"):
        client.approvals.wait_for("683b", timeout=-1)

    assert api.requests == []
