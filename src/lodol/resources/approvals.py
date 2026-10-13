"""Approvals: see what runs are waiting on, and answer them.

A workflow's Request approval step pauses its run until someone answers:
approves or rejects it, or, for a request that asks for one, replies in
writing. These let another system (a practice-management tool, an ERP, a
script) do the answering. An answer sent with an API key is recorded as that
key's.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lodol._transport import (
    APIRequest,
    APIResponse,
    expect_list,
    expect_object,
    new_idempotency_key,
    path_id,
)
from lodol.models import Approval
from lodol.pagination import AsyncPaginator, Paginator, check_limit
from lodol.resources._common import object_id

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    from lodol.client import AsyncLodol, Lodol

ApprovalRef = str | Approval

_STATUS_FILTERS = ("pending", "decided", "all")


class Approvals:
    """Requests from runs for an approval or a reply."""

    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self, *, status: str = "pending", limit: int | None = None
    ) -> Paginator[Approval]:
        """Approval requests, newest first.

        ``status`` is ``"pending"`` (still waiting for an answer, the
        default), ``"decided"`` (answered, expired or withdrawn) or ``"all"``.
        Loop over the result; pages are fetched as you go.
        """
        status = _check_status(status)
        limit = check_limit(limit)

        def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Approval], str | None]:
            return _parse_page(self._client._send(_list_request(status, cursor, size)))

        return Paginator(fetch_page, limit=limit, noun=f"{status} approval requests")

    def get(self, approval: ApprovalRef) -> Approval:
        """An approval request as it is now."""
        response = self._client._send(_get_request(approval))
        return _parse_one(response)

    def approve(self, approval: ApprovalRef, *, comment: str | None = None) -> Approval:
        """Approve a request, so its run carries on down the approved path."""
        request = _decision_request(approval, "approved", comment)
        return _parse_one(self._client._send(request))

    def reject(self, approval: ApprovalRef, *, comment: str | None = None) -> Approval:
        """Reject a request, so its run carries on down the rejected path."""
        request = _decision_request(approval, "rejected", comment)
        return _parse_one(self._client._send(request))

    def reply(self, approval: ApprovalRef, text: str) -> Approval:
        """Answer a request that asks for a written reply. The run carries on
        with ``text`` as the answer."""
        return _parse_one(self._client._send(_reply_request(approval, text)))


class AsyncApprovals:
    """Requests from runs for an approval or a reply."""

    def __init__(self, client: AsyncLodol) -> None:
        self._client = client

    def list(
        self, *, status: str = "pending", limit: int | None = None
    ) -> AsyncPaginator[Approval]:
        """Approval requests, newest first.

        ``status`` is ``"pending"`` (still waiting for an answer, the
        default), ``"decided"`` (answered, expired or withdrawn) or ``"all"``.
        Loop over the result with ``async for``; pages are fetched as you go.
        """
        status = _check_status(status)
        limit = check_limit(limit)

        async def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Approval], str | None]:
            return _parse_page(
                await self._client._send(_list_request(status, cursor, size))
            )

        return AsyncPaginator(
            fetch_page, limit=limit, noun=f"{status} approval requests"
        )

    async def get(self, approval: ApprovalRef) -> Approval:
        """An approval request as it is now."""
        return _parse_one(await self._client._send(_get_request(approval)))

    async def approve(
        self, approval: ApprovalRef, *, comment: str | None = None
    ) -> Approval:
        """Approve a request, so its run carries on down the approved path."""
        request = _decision_request(approval, "approved", comment)
        return _parse_one(await self._client._send(request))

    async def reject(
        self, approval: ApprovalRef, *, comment: str | None = None
    ) -> Approval:
        """Reject a request, so its run carries on down the rejected path."""
        request = _decision_request(approval, "rejected", comment)
        return _parse_one(await self._client._send(request))

    async def reply(self, approval: ApprovalRef, text: str) -> Approval:
        """Answer a request that asks for a written reply. The run carries on
        with ``text`` as the answer."""
        return _parse_one(await self._client._send(_reply_request(approval, text)))


def _check_status(status: str) -> str:
    if status not in _STATUS_FILTERS:
        raise ValueError(
            f"status must be one of {', '.join(map(repr, _STATUS_FILTERS))}, "
            f"not {status!r}"
        )
    return status


def _list_request(status: str, cursor: str | None, size: int) -> APIRequest:
    params = {"status": status, "limit": str(size)}
    if cursor is not None:
        params["before"] = cursor
    return APIRequest("GET", "/approvals", params=params)


def _parse_page(response: APIResponse) -> tuple[list[Approval], str | None]:
    body = expect_object(response, "GET /approvals")
    items = [
        Approval.from_api(item)
        for item in expect_list(
            body.get("approvals"), "GET /approvals field 'approvals'", response
        )
    ]
    next_before = body.get("next_before")
    return items, str(next_before) if next_before else None


def _get_request(approval: ApprovalRef) -> APIRequest:
    approval_id = object_id(approval, Approval, "approval request")
    return APIRequest("GET", f"/approvals/{path_id(approval_id)}")


def _parse_one(response: APIResponse) -> Approval:
    return Approval.from_api(expect_object(response, "/approvals/{id}"))


def _decision_request(
    approval: ApprovalRef, decision: str, comment: str | None
) -> APIRequest:
    if isinstance(approval, Approval) and approval.kind == "reply":
        raise ValueError(
            "This request asks for a written reply, not a decision: answer it "
            "with approvals.reply(approval, text)"
        )
    body: dict[str, Any] = {"decision": decision}
    if comment is not None:
        body["comment"] = comment
    return _answer(approval, body)


def _reply_request(approval: ApprovalRef, text: str) -> APIRequest:
    if isinstance(approval, Approval) and approval.kind != "reply":
        raise ValueError(
            "This request asks for a decision, not a reply: answer it with "
            "approvals.approve() or approvals.reject()"
        )
    if not isinstance(text, str):
        raise TypeError(f"The reply must be text, not {type(text).__name__}")
    return _answer(approval, {"reply": text})


def _answer(approval: ApprovalRef, body: dict[str, Any]) -> APIRequest:
    approval_id = object_id(approval, Approval, "approval request")
    # Keyed, so a retry after a lost response returns the first answer
    # instead of a conflict with it.
    return APIRequest(
        "POST",
        f"/approvals/{path_id(approval_id)}/decision",
        body=body,
        idempotency_key=new_idempotency_key("approval-answer"),
    )
