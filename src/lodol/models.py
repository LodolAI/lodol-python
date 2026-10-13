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
    WAITING_FOR_TRIGGER = "waiting_for_trigger"
    """A workflow that starts from its own trigger, listening for its next
    event."""
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
class Workflow:
    """A workflow in the workspace.

    ``id`` stays the same when the workflow is edited, so it is safe to store
    and run later. A draft (``is_draft``) can't be run until it is published.
    """

    id: str
    name: str
    description: str = ""
    inputs: list[WorkflowInput] = field(default_factory=list, hash=False)
    is_draft: bool = False
    created_by: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    last_run_at: dt.datetime | None = None
    program: dict[str, Any] | None = field(default=None, repr=False, hash=False)
    """The workflow's steps, as Lodol stores them. Only ``workflows.get``
    includes it."""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Workflow:
        raw = dict(data)
        program = raw.get("program")
        inputs = raw.get("inputs")
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            inputs=[
                WorkflowInput.from_api(item)
                for item in (inputs if isinstance(inputs, list) else [])
                if isinstance(item, Mapping)
            ],
            # Only an explicit draft is one: a workflow from before drafts
            # existed has no publish state, and runs.
            is_draft=raw.get("publish_state") == "draft",
            created_by=_optional_str(raw.get("created_by")),
            created_at=parse_datetime(raw.get("created_at")),
            updated_at=parse_datetime(raw.get("updated_at")),
            last_run_at=parse_datetime(raw.get("last_run_at")),
            program=program if isinstance(program, dict) else None,
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
class Step:
    """What one step of a run did."""

    index: int
    status: str
    """``"completed"`` or ``"failed"``. A step inside a loop reports its last pass."""
    result: Any = field(default=None, hash=False)
    error: Any = field(default=None, hash=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> Step:
        index = data.get("index")
        return cls(
            index=index if isinstance(index, int) else -1,
            status=str(data.get("status") or ""),
            result=data.get("result"),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class Run:
    """One run of a workflow.

    ``workflow_id`` is the workflow's ``id``; ``version_id`` is the version of
    the workflow that ran, which changes as the workflow is edited. ``steps``
    is ``None`` unless step results were asked for (``runs.wait`` asks for
    them by default). ``workflow_name`` comes with ``runs.list`` only, and
    ``error`` with ``runs.get`` and ``runs.wait`` only.
    """

    id: str
    status: str
    """One of :class:`RunStatus`, as text."""
    workflow_id: str | None = None
    workflow_name: str | None = None
    version_id: str | None = None
    error: str | None = None
    """Why the run failed, when it did."""
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
        return cls(
            id=str(raw.get("execution_id") or raw.get("id") or ""),
            status=str(raw.get("status") or ""),
            workflow_id=_optional_str(raw.get("workflow_id")),
            workflow_name=_optional_str(raw.get("workflow_name")),
            version_id=_optional_str(raw.get("version_id")),
            error=_optional_str(raw.get("error")),
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
