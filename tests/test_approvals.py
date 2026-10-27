"""Approvals: seeing what runs are waiting on, and answering them."""

from __future__ import annotations

import datetime as dt

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
)
from tests.conftest import ApprovalsServer, FakeAPI, approval_json, respond


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
