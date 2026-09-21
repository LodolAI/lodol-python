from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from lodol.client import Lodol

# A run in one of these has finished and will not change again.
TERMINAL_STATUSES = frozenset({"success", "failed", "stopped"})

# A run in one of these is parked until a person answers it in Lodol. Waiting
# for it to finish on its own would only ever end in a timeout.
NEEDS_ATTENTION_STATUSES = frozenset({"paused", "awaiting_review", "awaiting_input"})

# The listener a trigger-driven workflow leaves running. It starts a separate
# run per event it receives, so it never finishes itself.
TRIGGER_LISTENER_STATUS = "waiting_for_trigger"


@dataclass(frozen=True)
class WorkflowInput:
    """A value a workflow asks to be given when it is run."""

    name: str
    type: str = "text"
    required: bool = True
    default: Any = None
    description: str = ""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "WorkflowInput":
        return cls(
            name=str(data.get("name", "")),
            type=str(data.get("type", "text")),
            required=data.get("required", True) is not False,
            default=data.get("default"),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class WorkflowOutput:
    """A value a workflow hands back when it finishes."""

    name: str
    type: str = "text"
    description: str = ""

    @classmethod
    def from_api(cls, data: Mapping[str, Any]) -> "WorkflowOutput":
        return cls(
            name=str(data.get("name", "")),
            type=str(data.get("type", "text")),
            description=str(data.get("description") or ""),
        )


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    description: str = ""
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_run_at: str | None = None
    # What this workflow asks for, and what it gives back. Read these to see
    # what to pass to ``run()`` and what to expect in ``execution.outputs``.
    inputs: tuple[WorkflowInput, ...] = ()
    outputs: tuple[WorkflowOutput, ...] = ()
    program: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    _client: "Lodol | None" = field(default=None, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any], *, client: Any = None) -> "Workflow":
        raw = dict(data)
        program = raw.get("program")
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")),
            description=str(raw.get("description", "")),
            created_by=_optional_str(raw.get("created_by")),
            created_at=_optional_str(raw.get("created_at")),
            updated_at=_optional_str(raw.get("updated_at")),
            last_run_at=_optional_str(raw.get("last_run_at")),
            inputs=tuple(
                WorkflowInput.from_api(entry) for entry in _entries(raw.get("inputs"))
            ),
            outputs=tuple(
                WorkflowOutput.from_api(entry) for entry in _entries(raw.get("outputs"))
            ),
            program=program if isinstance(program, dict) else None,
            raw=raw,
            _client=client,
        )

    def run(self, **kwargs: Any) -> "Execution":
        """Run this workflow. Takes the same arguments as ``client.workflows.run``."""
        return self._attached().workflows.run(self.id, **kwargs)

    def _attached(self) -> "Lodol":
        if self._client is None:
            raise RuntimeError("Workflow is not attached to a Lodol client")
        return self._client


@dataclass(frozen=True)
class Execution:
    execution_id: str
    status: str
    workflow_id: str | None = None
    workflow_name: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    # What the workflow handed back, keyed by the output names it declares.
    # Empty until the run finishes, and for a workflow that declares none.
    outputs: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Per-step detail, for looking into how a run reached its result. Only
    # present when it was asked for with ``include_step_results=True``.
    steps: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    _client: "Lodol | None" = field(default=None, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any], *, client: Any = None) -> "Execution":
        raw = dict(data)
        steps = raw.get("steps")
        outputs = raw.get("outputs")
        return cls(
            execution_id=str(raw.get("execution_id", raw.get("id", ""))),
            workflow_id=_optional_str(raw.get("workflow_id")),
            workflow_name=_optional_str(raw.get("workflow_name")),
            status=str(raw.get("status", "")),
            created_at=_optional_str(raw.get("created_at")),
            started_at=_optional_str(raw.get("started_at")),
            completed_at=_optional_str(raw.get("completed_at")),
            outputs=dict(outputs) if isinstance(outputs, dict) else {},
            error=_optional_str(raw.get("error")),
            steps=steps if isinstance(steps, list) else None,
            raw=raw,
            _client=client,
        )

    @property
    def id(self) -> str:
        return self.execution_id

    @property
    def is_terminal(self) -> bool:
        """Whether the run has finished and will not change again."""
        return self.status in TERMINAL_STATUSES

    @property
    def needs_attention(self) -> bool:
        """Whether the run is parked waiting for a person to answer it in Lodol."""
        return self.status in NEEDS_ATTENTION_STATUSES

    @property
    def is_trigger_listener(self) -> bool:
        """Whether this is a trigger-driven workflow's listener rather than one run."""
        return self.status == TRIGGER_LISTENER_STATUS

    def refresh(self, *, include_step_results: bool = False) -> "Execution":
        """Read this run again, as it stands now."""
        return self._attached().executions.retrieve(
            self.execution_id,
            include_step_results=include_step_results,
        )

    def wait(self, **kwargs: Any) -> "Execution":
        """Wait for this run to finish. Takes the same arguments as
        ``client.executions.wait``."""
        return self._attached().executions.wait(self.execution_id, **kwargs)

    def stop(self, **kwargs: Any) -> "Execution":
        """Ask this run to stop."""
        return self._attached().executions.stop(self.execution_id, **kwargs)

    def _attached(self) -> "Lodol":
        if self._client is None:
            raise RuntimeError("Execution is not attached to a Lodol client")
        return self._client


def _entries(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
