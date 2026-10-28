"""Runs: follow your workflows' runs, wait for one to finish, retry, stop or
delete one."""

from __future__ import annotations

import datetime as dt
import logging
import warnings
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Generic, Iterable, TypeVar

from lodol import _transport, constants
from lodol._transport import (
    APIRequest,
    APIResponse,
    BaseClient,
    expect_list,
    expect_object,
    idempotency_key_for,
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
from lodol.models import DeletedRun, Run, RunStatus, StepStatus, Workflow
from lodol.pagination import AsyncPaginator, Paginator, check_limit
from lodol.resources._common import object_id

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    from lodol.client import AsyncLodol, Lodol

RunRef = str | Run
WorkflowRef = str | Workflow
# A list's query, beside its page size and cursor.
Filters = dict[str, str | list[str]]
# What a wait ends with.
T = TypeVar("T")

logger = logging.getLogger("lodol")

# Trouble a wait rides out: it says nothing about the run, only that one look
# at it didn't work out.
_PASSING_TROUBLE = (APIConnectionError, InternalServerError, RateLimitError)

# The statuses runs can be listed by: every one a run is kept in. A run reads
# "stopping" only in what stopping it returns; Lodol lists it as running until
# it has stopped, and would ignore "stopping" as a filter, listing every run.
_LISTED_STATUSES = tuple(
    status.value for status in RunStatus if status is not RunStatus.STOPPING
)

# A run that hasn't finished, as a sentence says it.
_UNFINISHED = {
    "queued": "still waiting to start",
    "running": "still running",
    "stopping": "stopping",
    "paused": "paused",
    "waiting": "paused on a Wait step",
    "awaiting_approval": "waiting for someone to answer an approval request",
}


class Runs:
    """The runs of the workspace's workflows."""

    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(
        self,
        *,
        workflow: WorkflowRef | None = None,
        status: str | Iterable[str] | None = None,
        started_from: dt.datetime | dt.date | None = None,
        started_to: dt.datetime | dt.date | None = None,
        search: str | None = None,
        limit: int | None = None,
    ) -> Paginator[Run]:
        """Runs, newest first: all of the workspace's, or those that match.

        - ``workflow``: one workflow's runs (every version of it).
        - ``status``: runs in a status, or in any of several, given as
          :class:`RunStatus` members or their text (``"failed"``). Any but
          ``"stopping"``, which a run reads only as it is stopped, can be
          given; anything else raises ``ValueError`` before anything is sent,
          so a typo can't make the list every run.
        - ``started_from`` and ``started_to``: runs that started at or after
          ``started_from``, and before ``started_to``. A ``date`` is the start
          of that day, UTC. A ``datetime`` without a timezone is sent as it
          is, and Lodol reads it as UTC.
        - ``search``: runs whose workflow's name, or a value in whose data
          (what they started with, handed back or failed with), matches.

        Loop over the result; pages are fetched as you go. ``limit`` stops
        after that many runs. Listed runs leave out ``outputs`` and ``steps``:
        ``runs.get`` has them.
        """
        filters = _list_filters(workflow, status, started_from, started_to, search)
        limit = check_limit(limit)

        def fetch_page(cursor: str | None, size: int) -> tuple[list[Run], str | None]:
            response = self._client._send(_list_request(filters, cursor, size))
            return _parse_page(response, size)

        return Paginator(fetch_page, limit=limit, noun="runs")

    def get(self, run: RunRef, *, include_steps: bool = False) -> Run:
        """A run as it is now. ``include_steps`` adds what each step did."""
        run_id = object_id(run, Run, "run")
        response = self._client._send(_get_request(run_id, include_steps))
        return parse_run(response)

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
        as long as the run takes. A workflow listening for its trigger
        (``run.is_trigger_listener``) never finishes, so it is returned at
        once, with a warning.
        """
        run_id = object_id(run, Run, "run")
        known = run if isinstance(run, Run) else None
        waiting = WaitingForRun(self._client, run_id, timeout, known, include_steps)
        return poll(self._client, waiting)

    def stop(self, run: RunRef) -> Run:
        """Stop a run. Steps it already finished aren't undone.

        Returns the run as ``runs.get`` gives it, with status ``"stopping"``
        while it finishes the step it is on, or ``"stopped"``; or, if it had
        already finished, as it finished. Any approval request it was
        waiting on is withdrawn.
        """
        run_id = object_id(run, Run, "run")
        try:
            response = self._client._send(_stop_request(run_id))
        except BadRequestError as exc:
            if not _already_finished(exc):
                raise
            return self.get(run_id)
        return parse_run(response)

    def delete(self, run: RunRef) -> DeletedRun:
        """Delete a finished run, and the runs it started. This can't be undone.

        The runs it started (its trigger's, or the workflows it called) go
        with it, as when it is deleted in the app. A run still going is
        refused with ``ConflictError`` (code ``"run_not_finished"``): stop it,
        or wait for it, first. Needs a key with the ``executions:delete``
        scope, made by a member allowed to delete runs. Never retried: if the
        request times out, check with ``runs.get`` before trying again.
        """
        response = self._client._send(_delete_request(object_id(run, Run, "run")))
        return _parse_deleted(response)

    def retry(
        self,
        run: RunRef,
        *,
        from_start: bool = False,
        wait: bool = False,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        """Retry a failed run, as a new run with the data it started with.

        By default the retry picks up at the step that failed, without
        running the steps before it again. ``from_start=True`` runs the
        workflow again from the top instead. Either way it starts with the
        failed run's data (its inputs, or the event its trigger caught), and
        the new run's ``retry_of`` names the one it retries.

        A run that can't be retried that way is refused with
        ``ConflictError`` (code ``"run_not_retryable"``): one that didn't
        fail, one a run calling its workflow started (retry that run
        instead), or, from the failed step, one that failed before Lodol kept
        where runs fail (retry it from the start). ``runs.get(run)`` says
        which retries a run offers, in ``retry_options``.

        ``wait``, ``timeout`` and ``idempotency_key`` work as they do for
        ``workflows.run``: by default this returns as soon as the retry is
        queued, and a retried request never starts a second retry.
        """
        request = _retry_request(run, from_start, wait, timeout, idempotency_key)
        retry = _parse_retry(self._client._send(request))
        if not wait:
            return retry
        return self.wait(retry, timeout=timeout)


class AsyncRuns:
    """The runs of the workspace's workflows."""

    def __init__(self, client: AsyncLodol) -> None:
        self._client = client

    def list(
        self,
        *,
        workflow: WorkflowRef | None = None,
        status: str | Iterable[str] | None = None,
        started_from: dt.datetime | dt.date | None = None,
        started_to: dt.datetime | dt.date | None = None,
        search: str | None = None,
        limit: int | None = None,
    ) -> AsyncPaginator[Run]:
        """Runs, newest first: all of the workspace's, or those that match.

        - ``workflow``: one workflow's runs (every version of it).
        - ``status``: runs in a status, or in any of several, given as
          :class:`RunStatus` members or their text (``"failed"``). Any but
          ``"stopping"``, which a run reads only as it is stopped, can be
          given; anything else raises ``ValueError`` before anything is sent,
          so a typo can't make the list every run.
        - ``started_from`` and ``started_to``: runs that started at or after
          ``started_from``, and before ``started_to``. A ``date`` is the start
          of that day, UTC. A ``datetime`` without a timezone is sent as it
          is, and Lodol reads it as UTC.
        - ``search``: runs whose workflow's name, or a value in whose data
          (what they started with, handed back or failed with), matches.

        Loop over the result with ``async for``; pages are fetched as you go.
        ``limit`` stops after that many runs. Listed runs leave out
        ``outputs`` and ``steps``: ``runs.get`` has them.
        """
        filters = _list_filters(workflow, status, started_from, started_to, search)
        limit = check_limit(limit)

        async def fetch_page(
            cursor: str | None, size: int
        ) -> tuple[list[Run], str | None]:
            response = await self._client._send(_list_request(filters, cursor, size))
            return _parse_page(response, size)

        return AsyncPaginator(fetch_page, limit=limit, noun="runs")

    async def get(self, run: RunRef, *, include_steps: bool = False) -> Run:
        """A run as it is now. ``include_steps`` adds what each step did."""
        run_id = object_id(run, Run, "run")
        response = await self._client._send(_get_request(run_id, include_steps))
        return parse_run(response)

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
        as long as the run takes. A workflow listening for its trigger
        (``run.is_trigger_listener``) never finishes, so it is returned at
        once, with a warning.
        """
        run_id = object_id(run, Run, "run")
        known = run if isinstance(run, Run) else None
        waiting = WaitingForRun(self._client, run_id, timeout, known, include_steps)
        return await apoll(self._client, waiting)

    async def stop(self, run: RunRef) -> Run:
        """Stop a run. Steps it already finished aren't undone.

        Returns the run as ``runs.get`` gives it, with status ``"stopping"``
        while it finishes the step it is on, or ``"stopped"``; or, if it had
        already finished, as it finished. Any approval request it was
        waiting on is withdrawn.
        """
        run_id = object_id(run, Run, "run")
        try:
            response = await self._client._send(_stop_request(run_id))
        except BadRequestError as exc:
            if not _already_finished(exc):
                raise
            return await self.get(run_id)
        return parse_run(response)

    async def delete(self, run: RunRef) -> DeletedRun:
        """Delete a finished run, and the runs it started. This can't be undone.

        The runs it started (its trigger's, or the workflows it called) go
        with it, as when it is deleted in the app. A run still going is
        refused with ``ConflictError`` (code ``"run_not_finished"``): stop it,
        or wait for it, first. Needs a key with the ``executions:delete``
        scope, made by a member allowed to delete runs. Never retried: if the
        request times out, check with ``runs.get`` before trying again.
        """
        response = await self._client._send(_delete_request(object_id(run, Run, "run")))
        return _parse_deleted(response)

    async def retry(
        self,
        run: RunRef,
        *,
        from_start: bool = False,
        wait: bool = False,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        """Retry a failed run, as a new run with the data it started with.

        By default the retry picks up at the step that failed, without
        running the steps before it again. ``from_start=True`` runs the
        workflow again from the top instead. Either way it starts with the
        failed run's data (its inputs, or the event its trigger caught), and
        the new run's ``retry_of`` names the one it retries.

        A run that can't be retried that way is refused with
        ``ConflictError`` (code ``"run_not_retryable"``): one that didn't
        fail, one a run calling its workflow started (retry that run
        instead), or, from the failed step, one that failed before Lodol kept
        where runs fail (retry it from the start). ``runs.get(run)`` says
        which retries a run offers, in ``retry_options``.

        ``wait``, ``timeout`` and ``idempotency_key`` work as they do for
        ``workflows.run``: by default this returns as soon as the retry is
        queued, and a retried request never starts a second retry.
        """
        request = _retry_request(run, from_start, wait, timeout, idempotency_key)
        retry = _parse_retry(await self._client._send(request))
        if not wait:
            return retry
        return await self.wait(retry, timeout=timeout)


class Waiting(ABC, Generic[T]):
    """What waiting on one run decides: what to ask Lodol for next, when,
    and when to stop. The sync and async loops (``poll`` and ``apoll``) only
    do what it says, so every wait paces itself, rides out passing trouble
    and keeps to its deadline the same way.

    A wait checks on the run, lightly, until it has finished; then, when
    step results are wanted, fetches them once. The deadline bounds the
    first stage. The second only adds the steps to an outcome already known,
    so when it can't, the outcome is returned without them. What the wait
    ends with, and whether it ends sooner, is up to each kind of wait.
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
        known = self.latest
        if known is not None and not known.finished and not known.is_trigger_listener:
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

    def next_request(self) -> APIRequest:
        """What the next check asks Lodol for."""
        return _get_request(self.run_id, self.fetching_steps)

    def checked(self, response: APIResponse) -> float | None:
        """After a check that worked: seconds until the next one, or
        ``None`` when the wait is over (``result()`` has the answer)."""
        return self._checked_run(parse_run(response))

    @abstractmethod
    def result(self) -> T:
        """What the wait ends with, or why not, raised."""

    @abstractmethod
    def without_steps(self) -> T:
        """What the wait ends with when the finished run's step results
        can't be fetched."""

    def _checked_run(self, run: Run) -> float | None:
        self._failing_since = None
        if self.finished is not None:
            self.latest = run if run.finished else self.finished
            return None
        self.latest = run
        self._looked = True
        if run.is_trigger_listener and not run.finished:
            # Called from checked(), from the loop, from the wait the
            # caller called.
            warn_listening(run, stacklevel=5)
            return None
        if run.finished:
            if not self._wants_steps(run):
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
        return self._still_going(run)

    def _wants_steps(self, run: Run) -> bool:
        """Whether to fetch the step results of a run that has finished."""
        return self._include_steps

    def _still_going(self, run: Run) -> float:
        """Seconds until the next check on a run that hasn't finished."""
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

    def _outcome(self) -> Run:
        """The finished run, if it succeeded; otherwise why not, raised. A
        workflow listening for its trigger is returned as it is."""
        assert self.latest is not None
        if self.latest.is_trigger_listener and not self.latest.finished:
            return self.latest
        return _succeeded(self.latest)

    def _outcome_without_steps(self) -> Run:
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


class WaitingForRun(Waiting[Run]):
    """Waiting for a run to finish (``runs.wait``)."""

    def result(self) -> Run:
        return self._outcome()

    def without_steps(self) -> Run:
        return self._outcome_without_steps()


def poll(client: Lodol, waiting: Waiting[T]) -> T:
    """Check on a run, as ``waiting`` says, until it says the wait is over."""
    delay = waiting.first_delay()
    while True:
        if delay:
            _transport.sleep(delay)
        try:
            response = client._send(
                waiting.next_request(),
                max_retries=0,
                timeout=waiting.check_timeout(),
            )
        except _PASSING_TROUBLE as exc:
            next_delay = waiting.failed(exc)
            if next_delay is None:
                if waiting.finished is None:
                    raise
                return waiting.without_steps()
            delay = next_delay
        else:
            next_delay = waiting.checked(response)
            if next_delay is None:
                return waiting.result()
            delay = next_delay


async def apoll(client: AsyncLodol, waiting: Waiting[T]) -> T:
    """Check on a run, as ``waiting`` says, until it says the wait is over."""
    delay = waiting.first_delay()
    while True:
        if delay:
            await _transport.async_sleep(delay)
        try:
            response = await client._send(
                waiting.next_request(),
                max_retries=0,
                timeout=waiting.check_timeout(),
            )
        except _PASSING_TROUBLE as exc:
            next_delay = waiting.failed(exc)
            if next_delay is None:
                if waiting.finished is None:
                    raise
                return waiting.without_steps()
            delay = next_delay
        else:
            next_delay = waiting.checked(response)
            if next_delay is None:
                return waiting.result()
            delay = next_delay


def _retry_after(error: LodolError) -> float | None:
    if isinstance(error, RateLimitError):
        return error.retry_after
    if isinstance(error, APIStatusError) and error.response is not None:
        return _transport.retry_after_seconds(error.response.headers)
    return None


def warn_listening(run: Run, *, stacklevel: int) -> None:
    """Warn that a run is a workflow listening for its trigger, which no wait
    would ever see finish. ``stacklevel`` is what the caller would pass to
    ``warnings.warn`` itself, to point at the user's own line."""
    name = f"'{run.workflow_name}'" if run.workflow_name else "This workflow"
    warnings.warn(
        f"{name} starts from its own trigger or schedule, so there's no end to "
        f"wait for: it is listening (run {run.id}), and each event it catches "
        "starts a run of its own, which runs.list() lists.",
        stacklevel=stacklevel + 1,
    )


def check_wait_options(wait: bool, timeout: float | None) -> None:
    """The ``wait`` and ``timeout`` of a call that starts a run, checked
    before anything is sent."""
    if timeout is not None and not wait:
        raise ValueError(
            "timeout is how long to wait for the run to finish, so it needs wait=True"
        )
    check_wait_timeout(timeout)


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
    # The step that failed it: not one that failed and was let carry on.
    failed_step = next(
        (step for step in run.steps or [] if step.status == StepStatus.FAILED), None
    )
    reason = run.error or (failed_step.error if failed_step else None)
    raise RunFailedError(
        f"Run {run.id} failed: {reason or 'Lodol gave no reason'}", run=run
    )


def _describe(status: str) -> str:
    return _UNFINISHED.get(status, status)


def _list_filters(
    workflow: WorkflowRef | None,
    status: str | Iterable[str] | None,
    started_from: dt.datetime | dt.date | None,
    started_to: dt.datetime | dt.date | None,
    search: str | None,
) -> Filters:
    """What ``runs.list`` narrows the list to, as its query, checked before
    anything is sent: Lodol ignores a filter it can't use, and would list
    every run instead."""
    filters: Filters = {}
    if workflow is not None:
        filters["workflow_id"] = object_id(workflow, Workflow, "workflow")
    statuses = _status_filter(status)
    if statuses:
        filters["status"] = statuses
    start = _start_bound(started_from, "started_from")
    end = _start_bound(started_to, "started_to")
    if start is not None and end is not None and _as_utc(end) <= _as_utc(start):
        raise ValueError(
            f"started_to ({end.isoformat()}) must be after started_from "
            f"({start.isoformat()}): runs that started at started_to aren't listed"
        )
    if start is not None:
        filters["from"] = start.isoformat()
    if end is not None:
        filters["to"] = end.isoformat()
    if search is not None:
        if not isinstance(search, str):
            raise TypeError(f"search must be text, not {type(search).__name__}")
        if not search.strip():
            raise ValueError(
                "search is empty: give the text to look for, or leave it out "
                "to list every run"
            )
        filters["search"] = search.strip()
    return filters


def _status_filter(status: str | Iterable[str] | None) -> list[str]:
    if status is None:
        return []
    if isinstance(status, str):
        given = [status]
    else:
        try:
            given = list(status)
        except TypeError:
            raise TypeError(
                "status must be a status, or a list of them, not "
                f"{type(status).__name__}"
            ) from None
        if not given:
            raise ValueError(
                "status is an empty list: name at least one status, or leave it "
                "out to list runs in any status"
            )
    statuses: list[str] = []
    for item in given:
        if not isinstance(item, str):
            raise TypeError(
                f"Each status must be text or a RunStatus, not {type(item).__name__}"
            )
        value = item.value if isinstance(item, RunStatus) else item
        if value not in _LISTED_STATUSES:
            why = (
                ": a run that is stopping is listed as 'running' until it has stopped"
                if value == RunStatus.STOPPING.value
                else ""
            )
            raise ValueError(
                f"{value!r} isn't a status runs can be listed by{why}. Use one of "
                f"{', '.join(map(repr, _LISTED_STATUSES))}."
            )
        if value not in statuses:
            statuses.append(value)
    return statuses


def _start_bound(
    value: dt.datetime | dt.date | None, name: str
) -> dt.datetime | dt.date | None:
    if value is None or isinstance(value, dt.date):
        return value
    raise TypeError(f"{name} must be a datetime or a date, not {type(value).__name__}")


def _as_utc(value: dt.date) -> dt.datetime:
    """A bound as Lodol reads it: a date is the start of that day, and a time
    without a timezone is UTC."""
    if not isinstance(value, dt.datetime):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if value.utcoffset() is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def _list_request(filters: Filters, cursor: str | None, size: int) -> APIRequest:
    params: Filters = {"limit": str(size), **filters}
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


def parse_run(response: APIResponse) -> Run:
    return Run.from_api(expect_object(response, "/executions/{execution_id}"))


def _stop_request(run_id: str) -> APIRequest:
    return APIRequest(
        "POST",
        f"/executions/{path_id(run_id)}/stop",
        idempotency_key=new_idempotency_key("run-stop"),
    )


def _retry_request(
    run: RunRef,
    from_start: bool,
    wait: bool,
    timeout: float | None,
    idempotency_key: str | None,
) -> APIRequest:
    run_id = object_id(run, Run, "run")
    check_wait_options(wait, timeout)
    return APIRequest(
        "POST",
        f"/executions/{path_id(run_id)}/retry",
        body={"from": "start" if from_start else "failed_step"},
        idempotency_key=idempotency_key_for("run-retry", idempotency_key),
    )


def _parse_retry(response: APIResponse) -> Run:
    return Run.from_api(
        expect_object(response, "POST /executions/{execution_id}/retry")
    )


def _delete_request(run_id: str) -> APIRequest:
    # No Idempotency-Key: the API doesn't take one here, so a delete is
    # never sent twice (a second one would say the run is gone).
    return APIRequest("DELETE", f"/executions/{path_id(run_id)}")


def _parse_deleted(response: APIResponse) -> DeletedRun:
    return DeletedRun.from_api(
        expect_object(response, "DELETE /executions/{execution_id}")
    )


def _already_finished(error: BadRequestError) -> bool:
    """Whether a stop was refused because the run had already finished."""
    return error.code == "run_already_finished"
