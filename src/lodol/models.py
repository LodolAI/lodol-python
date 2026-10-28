"""What the API returns, as plain, immutable Python objects.

Models are data only: they hold no connection, so they are safe to log,
compare, cache and pickle, and the same objects come back from ``Lodol`` and
``AsyncLodol``. To act on one, hand it (or its ``id``) to the client, as in
``lodol.runs.wait(run)``.

Every model keeps the response it was built from in ``raw``, so a field the
API adds before the SDK models it is still reachable.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class RunStatus(str, Enum):
    """Where a run is. ``Run.status`` holds one of these values as text, so
    ``run.status == "success"`` and ``run.status == RunStatus.SUCCESS`` agree.
    A status this version doesn't list may still appear, as text."""

    QUEUED = "queued"
    """Waiting for a free slot to start."""
    RUNNING = "running"
    WAITING = "waiting"
    """Set aside by a Wait step until its time comes."""
    AWAITING_APPROVAL = "awaiting_approval"
    """A Request approval step is waiting for someone to answer it."""
    PAUSED = "paused"
    STOPPING = "stopping"
    """Asked to stop, and finishing the step it was on."""
    SUCCESS = "success"
    FAILED = "failed"
    STOPPED = "stopped"

    def __str__(self) -> str:
        return str(self.value)


FINISHED_STATUSES = frozenset(
    {RunStatus.SUCCESS.value, RunStatus.FAILED.value, RunStatus.STOPPED.value}
)


class StepStatus(str, Enum):
    """How a step of a run went. ``Step.status`` holds one of these values as
    text, so ``step.status == "failed"`` and ``step.status ==
    StepStatus.FAILED`` agree. A status this version doesn't list may still
    appear, as text."""

    COMPLETED = "completed"
    FAILED = "failed"
    """It failed. ``Step.error`` says why."""
    CONTINUED = "continued"
    """It failed, but its setting let the run carry on. ``Step.error`` says
    what went wrong."""
    SKIPPED = "skipped"
    """It is turned off, so the run passed over it. ``Step.reason`` says so,
    and it has no ``result``."""

    def __str__(self) -> str:
        return str(self.value)


class ApprovalStatus(str, Enum):
    """Where an approval request is. ``Approval.status`` holds one of these
    values as text."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REPLIED = "replied"
    EXPIRED = "expired"
    """Nobody answered in time, and the step was set to fail."""
    CANCELLED = "cancelled"
    """The run was stopped, or its workflow deleted, before anyone answered."""

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True)
class WorkflowInput:
    """An input a workflow declares, which a run can be given a value for."""

    name: str
    type: str
    """``"text"``, ``"number"``, ``"boolean"``, ``"list"`` or ``"object"``."""
    required: bool = True
    default: Any = field(default=None, hash=False)
    """The value an optional input takes when a run leaves it out."""
    description: str = ""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> WorkflowInput:
        return cls(
            name=str(data.get("name") or ""),
            type=str(data.get("type") or "text"),
            required=bool(data.get("required", True)),
            default=data.get("default"),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class WorkflowOutput:
    """A value a workflow declares it hands back, which a run that succeeds
    has in ``Run.outputs`` under ``name``."""

    name: str
    type: str
    """``"text"``, ``"number"``, ``"boolean"``, ``"list"`` or ``"object"``."""
    description: str = ""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> WorkflowOutput:
        return cls(
            name=str(data.get("name") or ""),
            type=str(data.get("type") or "text"),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class Workflow:
    """A workflow in the workspace.

    ``id`` stays the same when the workflow is edited, so it is safe to store
    and run later. A draft (``is_draft``) can't be run until it is published.

    A workflow just saved (by ``workflows.create``, ``update`` or ``edit``)
    also says which version the save made, where to open it, and what it
    still needs; a workflow looked up or listed has ``None`` for those. A
    note on anything Lodol filled in as it saved is in ``raw["note"]``.
    """

    id: str
    name: str
    description: str = ""
    inputs: list[WorkflowInput] = field(default_factory=list, hash=False)
    outputs: list[WorkflowOutput] = field(default_factory=list, hash=False)
    """What a run that succeeds hands back, in ``Run.outputs``."""
    is_draft: bool = False
    starts_from_trigger: bool = False
    """Whether it starts from its own trigger or schedule. Running it only
    sets it listening (``Run.is_trigger_listener``), and each event it
    catches starts a run of its own."""
    created_by: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    last_run_at: dt.datetime | None = None
    program: dict[str, Any] | None = field(default=None, repr=False, hash=False)
    """The workflow's steps, as Lodol stores them. ``workflows.get`` and a
    save include it; ``workflows.list`` doesn't."""
    version_id: str | None = None
    """The version the save wrote, as ``Run.version_id`` names the version
    that ran: a new one, or, for an edit soon after the last save, the one
    it saved over. Only a save says."""
    url: str | None = None
    """Where to open it in Lodol. Only a save says."""
    apps_to_connect: list[str] | None = field(default=None, hash=False)
    """The apps it uses that the key's member has yet to connect in Lodol,
    which it can't run without. Only a save says."""
    trigger_paused: bool | None = None
    """Whether saving the change paused its trigger: it was listening, and
    listens again, on the new version, once it is run. Only ``update`` and
    ``edit`` say."""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Workflow:
        raw = dict(data)
        program = raw.get("program")
        inputs = raw.get("inputs")
        outputs = raw.get("outputs")
        apps = raw.get("apps_to_connect")
        trigger_paused = raw.get("trigger_paused")
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            inputs=[
                WorkflowInput.from_api(item)
                for item in (inputs if isinstance(inputs, list) else [])
                if isinstance(item, Mapping)
            ],
            outputs=[
                WorkflowOutput.from_api(item)
                for item in (outputs if isinstance(outputs, list) else [])
                if isinstance(item, Mapping)
            ],
            # Only an explicit draft is one: a workflow from before drafts
            # existed has no publish state, and runs.
            is_draft=raw.get("publish_state") == "draft",
            starts_from_trigger=bool(raw.get("starts_from_trigger")),
            created_by=_optional_str(raw.get("created_by")),
            created_at=parse_datetime(raw.get("created_at")),
            updated_at=parse_datetime(raw.get("updated_at")),
            last_run_at=parse_datetime(raw.get("last_run_at")),
            program=program if isinstance(program, dict) else None,
            version_id=_optional_str(raw.get("version_id")),
            url=_optional_str(raw.get("url")),
            apps_to_connect=(
                [str(app) for app in apps] if isinstance(apps, list) else None
            ),
            trigger_paused=(
                trigger_paused if isinstance(trigger_paused, bool) else None
            ),
            raw=raw,
        )


@dataclass(frozen=True)
class WorkflowCheck:
    """What ``workflows.validate`` made of a program: whether Lodol could
    save it, and if not, what to fix (``error``).

    ``raw`` has the rest of what Lodol said about a valid one: a dry run of
    it against mocked apps (``raw["preview"]``), its steps described
    (``raw["steps"]``), and a note on anything it would fill in as it saved
    it (``raw["note"]``).
    """

    valid: bool
    error: str | None = None
    """What to fix, when it isn't valid."""
    warnings: dict[str, Any] = field(default_factory=dict, hash=False)
    """Likely mistakes in a valid program, by kind, such as ``"warning"``
    for one that would do nothing."""
    starts_from_trigger: bool = False
    """Whether it starts from its own trigger or schedule."""
    actions: list[str] = field(default_factory=list, hash=False)
    """The actions it calls, by path, such as
    ``"/actions/library/slack/send-message"``."""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> WorkflowCheck:
        raw = dict(data)
        warnings = raw.get("warnings")
        actions = raw.get("actions")
        return cls(
            valid=raw.get("valid") is True,
            error=_optional_str(raw.get("error")),
            warnings=dict(warnings) if isinstance(warnings, Mapping) else {},
            starts_from_trigger=bool(raw.get("has_trigger")),
            actions=(
                [str(action) for action in actions] if isinstance(actions, list) else []
            ),
            raw=raw,
        )


@dataclass(frozen=True)
class DeletedWorkflow:
    """The workflow a delete removed, with every version of it."""

    id: str
    name: str
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> DeletedWorkflow:
        raw = dict(data)
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            raw=raw,
        )


@dataclass(frozen=True)
class DeletedRun:
    """The run a delete removed, with the runs it started."""

    id: str
    #: The runs it started (its trigger's, or the workflows it called), which
    #: were deleted with it.
    started_runs_deleted: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> DeletedRun:
        raw = dict(data)
        started = raw.get("started_runs_deleted")
        return cls(
            id=str(raw.get("execution_id") or ""),
            started_runs_deleted=started if isinstance(started, int) else 0,
            raw=raw,
        )


@dataclass(frozen=True)
class Step:
    """What one step of a run did."""

    index: int
    status: str
    """One of :class:`StepStatus`, as text. A step inside a loop reports its
    last pass."""
    result: Any = field(default=None, hash=False)
    error: Any = field(default=None, hash=False)
    """What went wrong, for a step that failed or carried on after failing."""
    reason: str | None = None
    """Why a skipped step didn't run."""
    attempts: int | None = None
    """How many tries the step took, when it retried. ``None`` when Lodol
    didn't say."""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Step:
        index = data.get("index")
        attempts = data.get("attempts")
        return cls(
            index=index if isinstance(index, int) else -1,
            status=str(data.get("status") or ""),
            result=data.get("result"),
            error=data.get("error"),
            reason=_optional_str(data.get("reason")),
            attempts=(
                attempts
                if isinstance(attempts, int) and not isinstance(attempts, bool)
                else None
            ),
        )


@dataclass(frozen=True)
class RetryOptions:
    """Which retries a run offers (see ``runs.retry``). A run that didn't
    fail offers neither."""

    from_start: bool = False
    """Whether it can be run again from the top, with the data it started
    with. Any failed run can, except one a run calling its workflow started:
    retry that run instead."""
    from_failed_step: bool = False
    """Whether it can pick up at the step that failed, without running the
    steps before it again. A run that failed before Lodol kept where runs
    fail can't."""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> RetryOptions:
        return cls(
            from_start=bool(data.get("from_start")),
            from_failed_step=bool(data.get("from_failed_step")),
        )


@dataclass(frozen=True)
class Run:
    """One run of a workflow.

    ``workflow_id`` is the workflow's ``id``; ``version_id`` is the version of
    the workflow that ran, which changes as the workflow is edited. ``steps``
    is ``None`` unless step results were asked for (``runs.wait`` asks for
    them by default). ``outputs`` comes with ``runs.get`` and ``runs.wait``
    only, not with ``runs.list``.
    """

    id: str
    status: str
    """One of :class:`RunStatus`, as text."""
    workflow_id: str | None = None
    workflow_name: str | None = None
    version_id: str | None = None
    error: str | None = None
    """Why the run failed, when it did."""
    outputs: dict[str, Any] = field(default_factory=dict, hash=False)
    """What the run handed back, by the names its workflow declares
    (``Workflow.outputs``). Empty until the run succeeds, and for a workflow
    that declares none. An output the run never set is ``None``."""
    is_trigger_listener: bool = False
    """Whether this is a workflow that starts from its own trigger or
    schedule, listening. It never finishes on its own: each event it catches
    starts a run of its own, listed with the rest. Its status reads
    ``"running"`` while it listens."""
    retry_of: str | None = None
    """The id of the run this one retried, when it is a retry. ``None``
    otherwise, and in ``runs.list``, which doesn't say."""
    retry_options: RetryOptions | None = None
    """Which retries this run offers, if it failed. ``None`` when the
    response didn't say: ``runs.get`` and ``runs.wait`` say, ``runs.list``
    doesn't."""
    created_at: dt.datetime | None = None
    started_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    steps: list[Step] | None = field(default=None, repr=False, hash=False)
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def finished(self) -> bool:
        """Whether the run is over: it succeeded, failed or was stopped."""
        return self.status in FINISHED_STATUSES

    @property
    def succeeded(self) -> bool:
        """Whether the run finished successfully."""
        return self.status == RunStatus.SUCCESS.value

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Run:
        raw = dict(data)
        steps = raw.get("steps")
        outputs = raw.get("outputs")
        retry_options = raw.get("retry_options")
        return cls(
            id=str(raw.get("execution_id") or raw.get("id") or ""),
            status=str(raw.get("status") or ""),
            workflow_id=_optional_str(raw.get("workflow_id")),
            workflow_name=_optional_str(raw.get("workflow_name")),
            version_id=_optional_str(raw.get("version_id")),
            error=_optional_str(raw.get("error")),
            outputs=dict(outputs) if isinstance(outputs, Mapping) else {},
            is_trigger_listener=bool(raw.get("is_trigger_listener")),
            retry_of=_optional_str(raw.get("retry_of")),
            retry_options=(
                RetryOptions.from_api(retry_options)
                if isinstance(retry_options, Mapping)
                else None
            ),
            created_at=parse_datetime(raw.get("created_at")),
            started_at=parse_datetime(raw.get("started_at")),
            completed_at=parse_datetime(raw.get("completed_at")),
            steps=(
                [Step.from_api(step) for step in steps if isinstance(step, Mapping)]
                if isinstance(steps, list)
                else None
            ),
            raw=raw,
        )


@dataclass(frozen=True)
class Decision:
    """How an approval request was answered."""

    decision: str
    """``"approved"``, ``"rejected"`` or ``"replied"``."""
    comment: str = ""
    reply: str = ""
    decided_by: str = ""
    """Who answered: a person's name (or email), ``"API key '…'"`` for an
    answer sent through the API, or ``"Lodol"`` when a time limit decided."""
    decided_at: dt.datetime | None = None
    via: str | None = None
    """``"app"``, ``"email"``, ``"api"`` or ``"timeout"``."""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Decision:
        decided_by = data.get("decided_by")
        who = ""
        if isinstance(decided_by, Mapping):
            who = str(decided_by.get("name") or decided_by.get("email") or "")
        return cls(
            decision=str(data.get("decision") or ""),
            comment=str(data.get("comment") or ""),
            reply=str(data.get("reply") or ""),
            decided_by=who,
            decided_at=parse_datetime(data.get("decided_at")),
            via=_optional_str(data.get("via")),
        )


@dataclass(frozen=True)
class Approval:
    """A run asking for an answer before it carries on.

    ``kind`` says what answer it needs: ``"approval"`` is approved or
    rejected, ``"reply"`` is answered in writing. The run waits, with status
    ``awaiting_approval``, until it gets one.
    """

    id: str
    kind: str
    status: str
    """One of :class:`ApprovalStatus`, as text."""
    title: str = ""
    details: str = ""
    workflow_id: str | None = None
    workflow_name: str | None = None
    run_id: str | None = None
    step_index: int | None = None
    created_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    decision: Decision | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def pending(self) -> bool:
        """Whether the request is still waiting for an answer."""
        return self.status == ApprovalStatus.PENDING.value

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Approval:
        raw = dict(data)
        decision = raw.get("decision")
        step_index = raw.get("step_index")
        return cls(
            id=str(raw.get("id") or ""),
            kind=str(raw.get("kind") or "approval"),
            status=str(raw.get("status") or ""),
            title=str(raw.get("title") or ""),
            details=str(raw.get("details") or ""),
            workflow_id=_optional_str(raw.get("workflow_id")),
            workflow_name=_optional_str(raw.get("workflow_name")),
            run_id=_optional_str(raw.get("execution_id")),
            step_index=step_index if isinstance(step_index, int) else None,
            created_at=parse_datetime(raw.get("created_at")),
            expires_at=parse_datetime(raw.get("expires_at")),
            decision=(
                Decision.from_api(decision) if isinstance(decision, Mapping) else None
            ),
            raw=raw,
        )


def parse_datetime(value: Any) -> dt.datetime | None:
    """An API timestamp as an aware ``datetime`` (UTC when it names no zone),
    or ``None`` when there isn't one."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "Zz":
        # Python before 3.11 doesn't read the "Z" shorthand.
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
