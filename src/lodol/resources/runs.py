"""Runs: follow your workflows' runs, wait for one to finish, stop one."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from lodol import _transport, constants
from lodol._transport import (
    APIRequest,
    APIResponse,
    BaseClient,
    expect_list,
    expect_object,
    new_idempotency_key,
    path_id,
)
from lodol.exceptions import (
    APIConnectionError,
    APIStatusError,
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

RunRef = str | Run
WorkflowRef = str | Workflow

logger = logging.getLogger("lodol")

# Trouble a wait rides out: it says nothing about the run, only that one look
# at it didn't work out.
_PASSING_TROUBLE = (APIConnectionError, InternalServerError, RateLimitError)

# A run that hasn't finished, as a sentence says it.
_UNFINISHED = {
    "queued": "still waiting to start",
    "running": "still running",
    "stopping": "stopping",
    "paused": "paused",
    "waiting": "paused on a Wait step",
    "awaiting_approval": "waiting for someone to answer an approval request",
    "waiting_for_trigger": "listening for its trigger",
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
        after that many runs. Listed runs leave out ``error`` and ``steps``:
        ``runs.get`` has them.
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
        return _parse_run(response)

    def wait(
        self,
        run: RunRef,
        *,
        timeout: float | None = None,
        include_steps: bool = True,
    ) -> Run:
        """Wait for a run to succeed, and return it with its step results.

        It checks on the run often at first, then less often (at most every
        10 seconds, or as the workspace's rate limit allows), riding out rate
        limits and brief outages along the way. A run that fails or is
        stopped raises ``RunFailedError``, whose ``run`` says what happened.
        ``timeout`` is how many seconds to wait before raising
        ``WaitTimeoutError`` (the run itself carries on); by default it waits
        as long as the run takes.
        """
        run_id = object_id(run, Run, "run")
        known = run if isinstance(run, Run) else None
        waiting = _Waiting(self._client, run_id, timeout, known, include_steps)
        delay = waiting.first_delay()
        while True:
            if delay:
                _transport.sleep(delay)
            try:
                response = self._client._send(
                    _get_request(run_id, waiting.fetching_steps),
                    max_retries=0,
                    timeout=waiting.check_timeout(),
                )
                current = _parse_run(response)
            except _PASSING_TROUBLE as exc:
                next_delay = waiting.failed(exc)
                if next_delay is None:
                    if waiting.finished is None:
                        raise
                    return waiting.without_steps()
                delay = next_delay
            else:
                next_delay = waiting.checked(current)
                if next_delay is None:
                    return waiting.result()
                delay = next_delay

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
        return _parse_run(response)


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
        ``limit`` stops after that many runs. Listed runs leave out ``error``
        and ``steps``: ``runs.get`` has them.
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
        return _parse_run(response)

    async def wait(
        self,
        run: RunRef,
        *,
        timeout: float | None = None,
        include_steps: bool = True,
    ) -> Run:
        """Wait for a run to succeed, and return it with its step results.

        It checks on the run often at first, then less often (at most every
        10 seconds, or as the workspace's rate limit allows), riding out rate
        limits and brief outages along the way. A run that fails or is
        stopped raises ``RunFailedError``, whose ``run`` says what happened.
        ``timeout`` is how many seconds to wait before raising
        ``WaitTimeoutError`` (the run itself carries on); by default it waits
        as long as the run takes.
        """
        run_id = object_id(run, Run, "run")
        known = run if isinstance(run, Run) else None
        waiting = _Waiting(self._client, run_id, timeout, known, include_steps)
        delay = waiting.first_delay()
        while True:
            if delay:
                await _transport.async_sleep(delay)
            try:
                response = await self._client._send(
                    _get_request(run_id, waiting.fetching_steps),
                    max_retries=0,
                    timeout=waiting.check_timeout(),
                )
                current = _parse_run(response)
            except _PASSING_TROUBLE as exc:
                next_delay = waiting.failed(exc)
                if next_delay is None:
                    if waiting.finished is None:
                        raise
                    return waiting.without_steps()
                delay = next_delay
            else:
                next_delay = waiting.checked(current)
                if next_delay is None:
                    return waiting.result()
                delay = next_delay

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
        return _parse_run(response)


class _Waiting:
    """What waiting for one run decides: when to check next, what to ask
    for, and when to stop. The sync and async waits only do what it says.

    A wait has two stages: checking, lightly, until the run has finished;
    then, when step results are wanted, fetching them once. The deadline
    bounds the first. The second only adds the steps to an outcome already
    known, so when it can't, the outcome is returned without them.
    """

    def __init__(
        self,
        client: BaseClient,
        run_id: str,
        timeout: float | None,
        known: Run | None,
        include_steps: bool,
    ) -> None:
        self.run_id = run_id
        self.timeout = check_wait_timeout(timeout)
        self.latest = known
        self.finished: Run | None = None
        self._client = client
        self._include_steps = include_steps
        now = _transport.monotonic()
        self._deadline = None if self.timeout is None else now + self.timeout
        self._interval = constants.INITIAL_POLL_INTERVAL
        self._failing_since: float | None = None
        self._looked = False
        self._last_status = known.status if known is not None else None

    @property
    def fetching_steps(self) -> bool:
        """Whether the next check is the one that fetches the step results."""
        return self.finished is not None

    def first_delay(self) -> float:
        """Seconds before the first check. A run known to be unfinished (one
        just started, say) isn't worth checking at once."""
        if self.latest is not None and not self.latest.finished:
            return self._before_deadline(self._paced(self._next_interval()))
        return 0.0

    def check_timeout(self) -> float:
        """How long the next check may take: the client's timeout, but not
        past the deadline while the run is still going."""
        timeout = self._client.timeout
        if self._deadline is None or self.finished is not None:
            return timeout
        remaining = self._deadline - _transport.monotonic()
        return min(timeout, max(remaining, constants.MIN_CHECK_TIMEOUT))

    def checked(self, run: Run) -> float | None:
        """After a check that worked: seconds until the next one, or
        ``None`` when the wait is over (``result()`` has the answer)."""
        self._failing_since = None
        if self.finished is not None:
            self.latest = run if run.finished else self.finished
            return None
        self.latest = run
        self._looked = True
        if run.finished:
            if not self._include_steps:
                return None
            self.finished = run
            delay = self._paced(0.0, steady=False)
            if self._past_deadline_after(delay):
                logger.warning(
                    "Run %s finished, with no time left before the wait's deadline "
                    "to fetch its step results",
                    self.run_id,
                )
                return None
            return delay
        if run.status != self._last_status:
            self._last_status = run.status
            logger.info("Run %s is %s", self.run_id, _describe(run.status))
        return self._before_deadline(self._paced(self._next_interval()))

    def failed(self, error: LodolError) -> float | None:
        """After a check that failed: seconds until the next try, or ``None``
        to stop trying."""
        now = _transport.monotonic()
        if self._failing_since is None:
            self._failing_since = now
        elif now - self._failing_since >= constants.WAIT_ERROR_TOLERANCE:
            return None
        delay = self._next_interval()
        asked = _retry_after(error)
        if asked is not None:
            # As long as Lodol asks (it's down for maintenance, say), up to a
            # minute at a time.
            delay = max(delay, min(asked, constants.MAX_RETRY_AFTER))
        delay = self._paced(delay, steady=False)
        logger.warning(
            "Couldn't check on run %s (%s); trying again in %.0fs",
            self.run_id,
            error,
            delay,
        )
        if self.finished is not None:
            return None if self._past_deadline_after(delay) else delay
        return self._before_deadline(delay)

    def result(self) -> Run:
        """The finished run, if it succeeded; otherwise why not, raised."""
        assert self.latest is not None
        return _succeeded(self.latest)

    def without_steps(self) -> Run:
        """The finished run as the check that saw it finish got it, when its
        step results can't be fetched."""
        assert self.finished is not None
        logger.warning(
            "Run %s finished, but its step results couldn't be fetched; "
            "runs.get(%r, include_steps=True) has them",
            self.run_id,
            self.run_id,
        )
        return _succeeded(self.finished)

    def _next_interval(self) -> float:
        interval = self._interval
        self._interval = min(
            self._interval * constants.POLL_BACKOFF, constants.MAX_POLL_INTERVAL
        )
        return interval

    def _paced(self, delay: float, *, steady: bool = True) -> float:
        """``delay``, lengthened to stay within the workspace's request
        allowance: checks no more often than it allows a minute (``steady``),
        and none while it is used up."""
        rate_limit = self._client._rate_limit
        if rate_limit is None:
            return delay
        if steady:
            delay = max(delay, 60.0 / rate_limit.limit)
        if rate_limit.remaining == 0:
            delay = max(delay, rate_limit.reset)
        return delay

    def _past_deadline_after(self, delay: float) -> bool:
        if self._deadline is None:
            return False
        return _transport.monotonic() + delay > self._deadline

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


def _retry_after(error: LodolError) -> float | None:
    if isinstance(error, RateLimitError):
        return error.retry_after
    if isinstance(error, APIStatusError) and error.response is not None:
        return _transport.retry_after_seconds(error.response.headers)
    return None


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
    ][:size]
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


def _parse_run(response: APIResponse) -> Run:
    return Run.from_api(expect_object(response, "/executions/{execution_id}"))


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
