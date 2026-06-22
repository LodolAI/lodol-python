from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from lodol.constants import DEFAULT_POLL_INTERVAL

TERMINAL_STATUSES = {"success", "failed", "stopped"}


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    description: str = ""
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    last_run_at: str | None = None
    program: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    _client: Any = field(default=None, repr=False, compare=False)

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
            program=program if isinstance(program, dict) else None,
            raw=raw,
            _client=client,
        )

    def run(self, **kwargs: Any) -> "Execution":
        if self._client is None:
            raise RuntimeError("Workflow is not attached to a Lodol client")
        return self._client.workflows.run(self.id, **kwargs)


@dataclass(frozen=True)
class Execution:
    execution_id: str
    status: str
    workflow_id: str | None = None
    workflow_name: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    error: str | None = None
    steps: list[dict[str, Any]] | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
    _client: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: Mapping[str, Any], *, client: Any = None) -> "Execution":
        raw = dict(data)
        steps = raw.get("steps")
        return cls(
            execution_id=str(raw.get("execution_id", raw.get("id", ""))),
            workflow_id=_optional_str(raw.get("workflow_id")),
            workflow_name=_optional_str(raw.get("workflow_name")),
            status=str(raw.get("status", "")),
            created_at=_optional_str(raw.get("created_at")),
            started_at=_optional_str(raw.get("started_at")),
            completed_at=_optional_str(raw.get("completed_at")),
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
        return self.status in TERMINAL_STATUSES

    def refresh(self, *, include_step_results: bool = False) -> "Execution":
        if self._client is None:
            raise RuntimeError("Execution is not attached to a Lodol client")
        return self._client.executions.retrieve(
            self.execution_id,
            include_step_results=include_step_results,
        )

    def wait(
        self,
        *,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        timeout: float | None = None,
        include_step_results: bool = True,
    ) -> "Execution":
        if self._client is None:
            raise RuntimeError("Execution is not attached to a Lodol client")
        return self._client.executions.wait(
            self.execution_id,
            poll_interval=poll_interval,
            timeout=timeout,
            include_step_results=include_step_results,
        )

    def stop(self, **kwargs: Any) -> "Execution":
        if self._client is None:
            raise RuntimeError("Execution is not attached to a Lodol client")
        return self._client.executions.stop(self.execution_id, **kwargs)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
