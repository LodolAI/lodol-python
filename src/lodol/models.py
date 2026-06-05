from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

TERMINAL_STATUSES = {"success", "failed", "stopped"}


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    description: str = ""
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    last_run_at: Optional[str] = None
    program: Optional[dict[str, Any]] = None
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
    workflow_id: Optional[str] = None
    workflow_name: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    steps: Optional[list[dict[str, Any]]] = None
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
        poll_interval: float = 2.0,
        timeout: Optional[float] = None,
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


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(value)
