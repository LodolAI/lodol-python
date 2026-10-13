"""Runs: follow your workflows' runs, wait for one to finish, stop one."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional, Union

from lodol import _transport, constants
from lodol._transport import (
    APIRequest,
    APIResponse,
    expect_list,
    expect_object,
    new_idempotency_key,
    path_id,
)
from lodol.exceptions import (
    APIConnectionError,
    BadRequestError,
    InternalServerError,
    LodolError,
    RateLimitError,
    RunFailedError,
    WaitTimeoutError,
)
from lodol.models import Run, Workflow
from lodol.pagination import AsyncPaginator, Paginator, check_limit
from lodol.resources._common import object_id

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    from lodol.client import AsyncLodol, Lodol

RunRef = Union[str, Run]
WorkflowRef = Union[str, Workflow]

logger = logging.getLogger("lodol")

# Trouble a wait rides out: it says nothing about the run, only that one look
# at it didn't work out.
_PASSING_TROUBLE = (APIConnectionError, InternalServerError, RateLimitError)

# A run that hasn't finished, as a sentence says it.
_UNFINISHED = {
    "queued": "still waiting to start",
    "running": "still running",
    "waiting": "paused on a Wait step",
    "awaiting_approval": "waiting for someone to answer an approval request",
}


class Runs:
    """The runs of the workspace's workflows."""

    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self, *, workflow: WorkflowRef | None = None, limit: int | None = None
    ) -> Paginator[Run]:
        """Runs, newest first: all of the workspace's, or one workflow's
        (every version of it).

        Loop over the result; pages are fetched as you go. ``limit`` stops
        after that many runs.
        """
        workflow_id = _workflow_filter(workflow)
        limit = check_limit(limit)

        def fetch_page(cursor: str | None, size: int) -> tuple[list[Run], str | None]:
            response = self._client._send(_list_request(workflow_id, cursor, size))
            return _parse_page(response, size)

        return Paginator(fetch_page, limit=limit, noun="runs")

    def get(self, run: RunRef, *, include_steps: bool = False) -> Run:
        """A run as it is now. ``include_steps`` adds what each step did."""
        run_id = object_id(run, Run, "run")
        response = self._client._send(_get_request(run_id, include_steps))
        return Run.from_api(expect_object(response, "GET /executions/{execution_id}"))

    def wait(
        self,
        run: RunRef,
        *,
        timeout: float | None = None,
        include_steps: bool = True,
    ) -> Run:
        """Wait for a run to succeed, and return it with its step results.

        It checks on the run often at first, then less often (at most every
        10 seconds), riding out rate limits and brief outages along the way.
        A run that fails or is stopped raises ``RunFailedError``, whose
        ``run`` says what happened. ``timeout`` is how many seconds to wait
        before raising ``WaitTimeoutError`` (the run itself carries on); by
        default it waits as long as the run takes.
        """
        run_id = object_id(run, Run, "run")
        waiting = _Waiting(run_id, timeout, run if isinstance(run, Run) else None)
        while True:
            try:
                current = self.get(run_id)
            except _PASSING_TROUBLE as exc:
                delay = waiting.failed(exc)
                if delay is None:
                    raise
            else:
                if current.finished:
                    if include_steps:
                        current = self.get(run_id, include_steps=True)
                    return _succeeded(current)
                delay = waiting.checked(current)
            _transport.sleep(delay)

    def stop(self, run: RunRef) -> Run:
        """Stop a run. Steps it already finished aren't undone.

        Returns the run with status ``"stopping"`` or ``"stopped"``, or, if it
        had already finished, as it finished.
        """
        run_id = object_id(run, Run, "run")
        try:
            response = self._client._send(_stop_request(run_id))
        except BadRequestError as exc:
            if not _already_finished(exc):
                raise
            return self.get(run_id)
        return Run.from_api(
            expect_object(response, "POST /executions/{execution_id}/stop")
        )


class AsyncRuns:
    """The runs of the workspace's workflows."""

    def __init__(self, client: AsyncLodol) -> None:
        self._client = client

    def list(
        self, *, workflow: WorkflowRef | None = None, limit: int | None = None
    ) -> AsyncPaginator[Run]:
        """Runs, newest first: all of the workspace's, or one workflow's
        (every version of it).

        Loop over the result with ``async for``; pages are fetched as you go.
        ``limit`` stops after that many runs.
        """
        workflow_id = _workflow_filter(workflow)
        limit = check_limit(limit)

        async def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Run], str | None]:
            response = await self._client._send(
                _list_request(workflow_id, cursor, size)
            )
            return _parse_page(response, size)

        return AsyncPaginator(fetch_page, limit=limit, noun="runs")

    async def get(self, run: RunRef, *, include_steps: bool = False) -> Run:
        """A run as it is now. ``include_steps`` adds what each step did."""
        run_id = object_id(run, Run, "run")
        response = await self._client._send(_get_request(run_id, include_steps))
        return Run.from_api(expect_object(response, "GET /executions/{execution_id}"))

    async def wait(
        self,
        run: RunRef,
        *,
        timeout: float | None = None,
        include_steps: bool = True,
    ) -> Run:
        """Wait for a run to succeed, and return it with its step results.

        It checks on the run often at first, then less often (at most every
        10 seconds), riding out rate limits and brief outages along the way.
        A run that fails or is stopped raises ``RunFailedError``, whose
        ``run`` says what happened. ``timeout`` is how many seconds to wait
        before raising ``WaitTimeoutError`` (the run itself carries on); by
        default it waits as long as the run takes.
        """
        run_id = object_id(run, Run, "run")
        waiting = _Waiting(run_id, timeout, run if isinstance(run, Run) else None)
        while True:
            try:
                current = await self.get(run_id)
            except _PASSING_TROUBLE as exc:
                delay = waiting.failed(exc)
                if delay is None:
                    raise
            else:
                if current.finished:
                    if include_steps:
                        current = await self.get(run_id, include_steps=True)
                    return _succeeded(current)
                delay = waiting.checked(current)
            await _transport.async_sleep(delay)

    async def stop(self, run: RunRef) -> Run:
        """Stop a run. Steps it already finished aren't undone.

        Returns the run with status ``"stopping"`` or ``"stopped"``, or, if it
        had already finished, as it finished.
        """
        run_id = object_id(run, Run, "run")
        try:
            response = await self._client._send(_stop_request(run_id))
        except BadRequestError as exc:
            if not _already_finished(exc):
                raise
            return await self.get(run_id)
        return Run.from_api(
            expect_object(response, "POST /executions/{execution_id}/stop")
        )


class _Waiting:
    """The bookkeeping of waiting for one run: when to look next, and when to
    stop looking."""

    def __init__(self, run_id: str, timeout: float | None, latest: Run | None) -> None:
        self.run_id = run_id
        self.timeout = check_wait_timeout(timeout)
        self.latest = latest
        self._looked = False
        now = _transport.monotonic()
        self._deadline = None if timeout is None else now + timeout
        self._interval = constants.INITIAL_POLL_INTERVAL
        self._failing_since: Optional[float] = None
        self._last_status = latest.status if latest is not None else None

    def checked(self, run: Run) -> float:
        """Seconds until the next look, after one that worked."""
        self.latest = run
        self._looked = True
        self._failing_since = None
        if run.status != self._last_status:
            self._last_status = run.status
            logger.info("Run %s is %s", self.run_id, _describe(run.status))
        return self._before_deadline(self._next_interval())

    def failed(self, error: LodolError) -> float | None:
        """Seconds until the next look, after one that failed; or ``None``
        to give up, when looking has failed for too long."""
        now = _transport.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        elif now - self._failing_since >= constants.WAIT_ERROR_TOLERANCE:
            return None
        delay = self._next_interval()
        if isinstance(error, RateLimitError) and error.retry_after is not None:
            delay = max(delay, error.retry_after)
        logger.warning(
            "Couldn't check on run %s (%s); looking again in %.0fs",
            self.run_id,
            error,
            delay,
        )
        return self._before_deadline(delay)

    def _next_interval(self) -> float:
        interval = self._interval
        self._interval = min(
            self._interval * constants.POLL_BACKOFF, constants.MAX_POLL_INTERVAL
        )
        return interval

    def _before_deadline(self, delay: float) -> float:
        if self._deadline is None:
            return delay
        remaining = self._deadline - _transport.monotonic()
        if remaining <= 0:
            raise self._timed_out()
        return min(delay, remaining)

    def _timed_out(self) -> WaitTimeoutError:
        if self.latest is None or not self._looked:
            where = "it couldn't be checked"
        else:
            where = f"it is {_describe(self.latest.status)}"
        return WaitTimeoutError(
            f"Stopped waiting for run {self.run_id} after {self.timeout:g} seconds: "
            f"{where}. The run carries on; wait for it again, or stop it with "
            "runs.stop().",
            run=self.latest,
        )


def check_wait_timeout(timeout: float | None) -> float | None:
    """A wait's ``timeout``, checked before anything is sent."""
    if timeout is None:
        return None
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("timeout must be a number of seconds, or None")
    if timeout < 0:
        raise ValueError("timeout must be 0 or more seconds, or None")
    return timeout


def _succeeded(run: Run) -> Run:
    """The finished run, if it succeeded; otherwise why not, raised."""
    if run.succeeded:
        return run
    if run.status == "stopped":
        raise RunFailedError(f"Run {run.id} was stopped before it finished", run=run)
    failed_step = next(
        (step for step in run.steps or [] if step.status == "failed"), None
    )
    reason = run.error or (failed_step.error if failed_step else None)
    raise RunFailedError(
        f"Run {run.id} failed: {reason or 'Lodol gave no reason'}", run=run
    )


def _describe(status: str) -> str:
    return _UNFINISHED.get(status, status)


def _workflow_filter(workflow: WorkflowRef | None) -> str | None:
    if workflow is None:
        return None
    return object_id(workflow, Workflow, "workflow")


def _list_request(workflow_id: str | None, cursor: str | None, size: int) -> APIRequest:
    params = {"limit": str(size)}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    if cursor is not None:
        params["after"] = cursor
    return APIRequest("GET", "/executions", params=params)


def _parse_page(response: APIResponse, size: int) -> tuple[list[Run], str | None]:
    body = expect_object(response, "GET /executions")
    runs = [
        Run.from_api(item)
        for item in expect_list(
            body.get("executions"), "GET /executions field 'executions'", response
        )
    ]
    # The API doesn't say whether there is more. A full page may have more
    # after it: the next page starts after its last run.
    more = len(runs) >= size and bool(runs[-1].id)
    return runs, runs[-1].id if more else None


def _get_request(run_id: str, include_steps: bool) -> APIRequest:
    return APIRequest(
        "GET",
        f"/executions/{path_id(run_id)}",
        params={"include_step_results": "true"} if include_steps else None,
    )


def _stop_request(run_id: str) -> APIRequest:
    return APIRequest(
        "POST",
        f"/executions/{path_id(run_id)}/stop",
        idempotency_key=new_idempotency_key("run-stop"),
    )


def _already_finished(error: BadRequestError) -> bool:
    # How the API refuses to stop a finished run: 400 "Execution already
    # success" (or failed, or stopped).
    return error.message.startswith("Execution already")
