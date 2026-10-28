"""Approvals: see what runs are waiting on, and answer them.

A workflow's Request approval step pauses its run until someone answers:
approves or rejects it, or, for a request that asks for one, replies in
writing. These let another system (a practice-management tool, an ERP, a
script) do the answering. An answer sent with an API key is recorded as that
key's. Answering a request that is no longer pending raises
``ConflictError``, whose ``code`` says what happened to it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lodol import constants
from lodol._transport import (
    APIRequest,
    APIResponse,
    BaseClient,
    expect_list,
    expect_object,
    new_idempotency_key,
    path_id,
)
from lodol.models import Approval, Run, RunStatus, Workflow
from lodol.pagination import AsyncPaginator, Paginator, advance, check_limit
from lodol.resources._common import object_id
from lodol.resources.runs import (
    RunRef,
    Waiting,
    WorkflowRef,
    apoll,
    parse_run,
    poll,
)

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    from lodol.client import AsyncLodol, Lodol

ApprovalRef = str | Approval

_STATUS_FILTERS = ("pending", "decided", "all")


class Approvals:
    """Requests from runs for an approval or a reply."""

    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self,
        *,
        run: RunRef | None = None,
        workflow: WorkflowRef | None = None,
        status: str = "pending",
        limit: int | None = None,
    ) -> Paginator[Approval]:
        """Approval requests, newest first: all of the workspace's, or one
        run's or one workflow's.

        ``run`` keeps the requests one run asked; ``workflow`` those of one
        workflow, given its ``id`` or the id of any version of it. ``status``
        is ``"pending"`` (still waiting for an answer, the default),
        ``"decided"`` (answered, expired or withdrawn) or ``"all"``.
        Loop over the result; pages are fetched as you go.
        """
        filters = _list_filters(run, workflow, status)
        limit = check_limit(limit)

        def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Approval], str | None]:
            response = self._client._send(_list_request(filters, cursor, size))
            return _parse_page(response, filters.get("execution_id"))

        return Paginator(fetch_page, limit=limit, noun=_noun(filters))

    def wait_for(self, run: RunRef, *, timeout: float | None = None) -> Approval | None:
        """Wait until a run asks for an answer, and return the request it is
        waiting on.

        It checks on the run as ``runs.wait`` does (often at first, then
        less often, as the workspace's rate limit allows, riding out rate
        limits and brief outages along the way), and each time it finds the
        run waiting for an answer, looks for the run's pending request. A
        run can ask more than once: after answering, call this again for the
        next request.

        Returns ``None`` if the run succeeds without asking. A run that fails
        or is stopped first raises ``RunFailedError``, whose ``run`` says
        what happened. ``timeout`` is how many seconds to wait before raising
        ``WaitTimeoutError`` (the run itself carries on); by default it waits
        as long as it takes. A workflow listening for its trigger
        (``run.is_trigger_listener``) never asks itself (each event it
        catches starts a run of its own, which may), so this returns
        ``None`` for it at once, with the warning ``runs.wait`` gives.
        """
        return poll(self._client, _waiting_for(self._client, run, timeout))

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
        self,
        *,
        run: RunRef | None = None,
        workflow: WorkflowRef | None = None,
        status: str = "pending",
        limit: int | None = None,
    ) -> AsyncPaginator[Approval]:
        """Approval requests, newest first: all of the workspace's, or one
        run's or one workflow's.

        ``run`` keeps the requests one run asked; ``workflow`` those of one
        workflow, given its ``id`` or the id of any version of it. ``status``
        is ``"pending"`` (still waiting for an answer, the default),
        ``"decided"`` (answered, expired or withdrawn) or ``"all"``.
        Loop over the result with ``async for``; pages are fetched as you go.
        """
        filters = _list_filters(run, workflow, status)
        limit = check_limit(limit)

        async def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Approval], str | None]:
            response = await self._client._send(_list_request(filters, cursor, size))
            return _parse_page(response, filters.get("execution_id"))

        return AsyncPaginator(fetch_page, limit=limit, noun=_noun(filters))

    async def wait_for(
        self, run: RunRef, *, timeout: float | None = None
    ) -> Approval | None:
        """Wait until a run asks for an answer, and return the request it is
        waiting on.

        It checks on the run as ``runs.wait`` does (often at first, then
        less often, as the workspace's rate limit allows, riding out rate
        limits and brief outages along the way), and each time it finds the
        run waiting for an answer, looks for the run's pending request. A
        run can ask more than once: after answering, call this again for the
        next request.

        Returns ``None`` if the run succeeds without asking. A run that fails
        or is stopped first raises ``RunFailedError``, whose ``run`` says
        what happened. ``timeout`` is how many seconds to wait before raising
        ``WaitTimeoutError`` (the run itself carries on); by default it waits
        as long as it takes. A workflow listening for its trigger
        (``run.is_trigger_listener``) never asks itself (each event it
        catches starts a run of its own, which may), so this returns
        ``None`` for it at once, with the warning ``runs.wait`` gives.
        """
        return await apoll(self._client, _waiting_for(self._client, run, timeout))

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


class WaitingForApproval(Waiting[Approval | None]):
    """Waiting for a run to ask for an answer (``approvals.wait_for``).

    It makes the checks ``runs.wait`` makes, and each time one finds the
    run waiting for an answer, lists the run's pending requests right
    after, as soon as the workspace's allowance lets it. It
    stops when one is listed. Right after an answer, the run can still read
    ``awaiting_approval`` before its next request is there, so a look that
    finds none carries on checking the run.
    """

    def __init__(
        self,
        client: BaseClient,
        run_id: str,
        timeout: float | None,
        known: Run | None,
    ) -> None:
        super().__init__(client, run_id, timeout, known, include_steps=True)
        self.approval: Approval | None = None
        # Whether the next check lists the run's pending requests, and from
        # where, for an older Lodol that lists every run's.
        self._looking = False
        self._cursor: str | None = None

    def next_request(self) -> APIRequest:
        if not self._looking:
            return super().next_request()
        filters = {"status": "pending", "execution_id": self.run_id}
        return _list_request(filters, self._cursor, constants.MAX_PAGE_SIZE)

    def checked(self, response: APIResponse) -> float | None:
        if not self._looking:
            # Not through super().checked(), which would put a frame between
            # the warning about a listening run and the caller's own line.
            return self._checked_run(parse_run(response))
        self._failing_since = None
        requests, next_before = _parse_page(response, self.run_id)
        self.approval = next((item for item in requests if item.pending), None)
        if self.approval is not None:
            return None
        if next_before is not None:
            # An older Lodol, listing every run's: on to the next page.
            self._cursor = advance(self._cursor, next_before)
            return self._before_deadline(self._paced(0.0))
        self._looking, self._cursor = False, None
        assert self.latest is not None
        return super()._still_going(self.latest)

    def result(self) -> Approval | None:
        if self.approval is not None:
            return self.approval
        # It succeeded without asking, or is listening; or why not, raised.
        self._outcome()
        return None

    def without_steps(self) -> Approval | None:
        # Only a run that failed or was stopped has its steps fetched, so
        # this raises RunFailedError, without them.
        self._outcome_without_steps()
        return None  # pragma: no cover - never reached

    def _wants_steps(self, run: Run) -> bool:
        # Only to say, with the run, why it failed: a run that succeeded
        # without asking is all the wait needs to know.
        return not run.succeeded

    def _still_going(self, run: Run) -> float:
        if run.status == RunStatus.AWAITING_APPROVAL.value:
            self._looking = True
            return self._before_deadline(self._paced(0.0))
        return super()._still_going(run)


def _waiting_for(
    client: BaseClient, run: RunRef, timeout: float | None
) -> WaitingForApproval:
    run_id = object_id(run, Run, "run")
    known = run if isinstance(run, Run) else None
    return WaitingForApproval(client, run_id, timeout, known)


def _list_filters(
    run: RunRef | None, workflow: WorkflowRef | None, status: str
) -> dict[str, str]:
    if status not in _STATUS_FILTERS:
        raise ValueError(
            f"status must be one of {', '.join(map(repr, _STATUS_FILTERS))}, "
            f"not {status!r}"
        )
    filters = {"status": status}
    if run is not None:
        filters["execution_id"] = object_id(run, Run, "run")
    if workflow is not None:
        filters["workflow_id"] = object_id(workflow, Workflow, "workflow")
    return filters


def _noun(filters: dict[str, str]) -> str:
    noun = f"{filters['status']} approval requests"
    if "execution_id" in filters:
        noun += f" from run {filters['execution_id']}"
    if "workflow_id" in filters:
        noun += f" from workflow {filters['workflow_id']}"
    return noun


def _list_request(filters: dict[str, str], cursor: str | None, size: int) -> APIRequest:
    params = {**filters, "limit": str(size)}
    if cursor is not None:
        params["before"] = cursor
    return APIRequest("GET", "/approvals", params=params)


def _parse_page(
    response: APIResponse, run_id: str | None
) -> tuple[list[Approval], str | None]:
    body = expect_object(response, "GET /approvals")
    items = [
        Approval.from_api(item)
        for item in expect_list(
            body.get("approvals"), "GET /approvals field 'approvals'", response
        )
    ]
    if run_id is not None:
        # An older Lodol ignores the run asked for, and lists every run's. A
        # workflow it ignores too can't be told apart here: the request
        # names the workflow's id, and the filter may be a version's.
        items = [item for item in items if item.run_id == run_id]
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
