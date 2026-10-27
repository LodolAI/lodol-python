"""Workflows: list them, look one up, run one, delete one."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Mapping

from lodol._transport import (
    APIRequest,
    APIResponse,
    encode_json,
    expect_list,
    expect_object,
    idempotency_key_header,
    new_idempotency_key,
    path_id,
)
from lodol.models import DeletedWorkflow, Run, Workflow
from lodol.resources._common import object_id
from lodol.resources.runs import check_wait_timeout, warn_listening

if TYPE_CHECKING:  # pragma: no cover - only for type hints
    from lodol.client import AsyncLodol, Lodol

WorkflowRef = str | Workflow


class Workflows:
    """The workspace's workflows."""

    def __init__(self, client: Lodol) -> None:
        self._client = client

    def list(self) -> list[Workflow]:
        """Every workflow in the workspace."""
        return _parse_list(self._client._send(APIRequest("GET", "/workflows")))

    def get(self, workflow: WorkflowRef) -> Workflow:
        """A workflow as it is now, including its steps (``program``).

        Takes the workflow's ``id``, or the id of any version of it.
        """
        response = self._client._send(_get_request(workflow))
        return Workflow.from_api(expect_object(response, "GET /workflows/{id}"))

    def run(
        self,
        workflow: WorkflowRef,
        *,
        inputs: Mapping[str, Any] | None = None,
        wait: bool = False,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        """Start a run of a workflow, as it is now.

        ``inputs`` gives values for the inputs the workflow declares, by name
        (``workflow.inputs`` lists them). A value Lodol can't use for its
        input refuses the run with ``BadRequestError`` naming the input, and
        nothing runs.

        By default this returns as soon as the run is queued. A run started
        while the workspace already has as many going as its plan allows
        stays ``queued`` until one of them finishes. ``wait=True`` returns
        once it has succeeded, with its step results, and raises
        ``RunFailedError`` if it fails or is stopped; ``timeout`` then bounds
        the wait in seconds (see ``runs.wait``).

        A workflow that starts from its own trigger or schedule doesn't run
        to an end: running it sets it listening, and each event it catches
        starts a run of its own. With ``wait=True`` it returns at once, with
        a warning, since there is no end to wait for.

        A retried request never starts a second run. To make sure a run
        happens once across separate calls, such as a job that may run
        again after a crash, pass the same ``idempotency_key`` each time,
        e.g. ``f"monthly-report:{client_id}:{month}"``. Lodol remembers a key
        for 24 hours.
        """
        request = _run_request(workflow, inputs, wait, timeout, idempotency_key)
        started = Run.from_api(
            expect_object(self._client._send(request), "POST /workflows/{id}/run-async")
        )
        if not wait or _listening(started):
            return started
        return self._client.runs.wait(started, timeout=timeout)

    def delete(self, workflow: WorkflowRef) -> DeletedWorkflow:
        """Delete a workflow and every version of it. This can't be undone.

        Runs in progress are stopped and its trigger or schedule stops
        listening, as when it is deleted in the app; its past runs stay
        readable. Needs a key with the ``workflows:delete`` scope, made by a
        member allowed to delete workflows. Never retried: if the request
        times out, check with ``workflows.get`` before trying again.
        """
        response = self._client._send(_delete_request(workflow))
        return DeletedWorkflow.from_api(
            expect_object(response, "DELETE /workflows/{id}")
        )


class AsyncWorkflows:
    """The workspace's workflows."""

    def __init__(self, client: AsyncLodol) -> None:
        self._client = client

    async def list(self) -> list[Workflow]:
        """Every workflow in the workspace."""
        return _parse_list(await self._client._send(APIRequest("GET", "/workflows")))

    async def get(self, workflow: WorkflowRef) -> Workflow:
        """A workflow as it is now, including its steps (``program``).

        Takes the workflow's ``id``, or the id of any version of it.
        """
        response = await self._client._send(_get_request(workflow))
        return Workflow.from_api(expect_object(response, "GET /workflows/{id}"))

    async def run(
        self,
        workflow: WorkflowRef,
        *,
        inputs: Mapping[str, Any] | None = None,
        wait: bool = False,
        timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> Run:
        """Start a run of a workflow, as it is now.

        ``inputs`` gives values for the inputs the workflow declares, by name
        (``workflow.inputs`` lists them). A value Lodol can't use for its
        input refuses the run with ``BadRequestError`` naming the input, and
        nothing runs.

        By default this returns as soon as the run is queued. A run started
        while the workspace already has as many going as its plan allows
        stays ``queued`` until one of them finishes. ``wait=True`` returns
        once it has succeeded, with its step results, and raises
        ``RunFailedError`` if it fails or is stopped; ``timeout`` then bounds
        the wait in seconds (see ``runs.wait``).

        A workflow that starts from its own trigger or schedule doesn't run
        to an end: running it sets it listening, and each event it catches
        starts a run of its own. With ``wait=True`` it returns at once, with
        a warning, since there is no end to wait for.

        A retried request never starts a second run. To make sure a run
        happens once across separate calls, such as a job that may run
        again after a crash, pass the same ``idempotency_key`` each time,
        e.g. ``f"monthly-report:{client_id}:{month}"``. Lodol remembers a key
        for 24 hours.
        """
        request = _run_request(workflow, inputs, wait, timeout, idempotency_key)
        started = Run.from_api(
            expect_object(
                await self._client._send(request), "POST /workflows/{id}/run-async"
            )
        )
        if not wait or _listening(started):
            return started
        return await self._client.runs.wait(started, timeout=timeout)

    async def delete(self, workflow: WorkflowRef) -> DeletedWorkflow:
        """Delete a workflow and every version of it. This can't be undone.

        Runs in progress are stopped and its trigger or schedule stops
        listening, as when it is deleted in the app; its past runs stay
        readable. Needs a key with the ``workflows:delete`` scope, made by a
        member allowed to delete workflows. Never retried: if the request
        times out, check with ``workflows.get`` before trying again.
        """
        response = await self._client._send(_delete_request(workflow))
        return DeletedWorkflow.from_api(
            expect_object(response, "DELETE /workflows/{id}")
        )


def _parse_list(response: APIResponse) -> list[Workflow]:
    body = expect_object(response, "GET /workflows")
    items = expect_list(
        body.get("workflows"), "GET /workflows field 'workflows'", response
    )
    return [Workflow.from_api(item) for item in items]


def _get_request(workflow: WorkflowRef) -> APIRequest:
    workflow_id = object_id(workflow, Workflow, "workflow")
    return APIRequest("GET", f"/workflows/{path_id(workflow_id)}")


def _delete_request(workflow: WorkflowRef) -> APIRequest:
    # No Idempotency-Key: the API doesn't take one here, so a delete is
    # never sent twice (a second one would say the workflow is gone).
    workflow_id = object_id(workflow, Workflow, "workflow")
    return APIRequest("DELETE", f"/workflows/{path_id(workflow_id)}")


def _run_request(
    workflow: WorkflowRef,
    inputs: Mapping[str, Any] | None,
    wait: bool,
    timeout: float | None,
    idempotency_key: str | None,
) -> APIRequest:
    workflow_id = object_id(workflow, Workflow, "workflow")
    if timeout is not None and not wait:
        raise ValueError(
            "timeout is how long to wait for the run to finish, so it needs wait=True"
        )
    check_wait_timeout(timeout)
    body = None
    if inputs is not None:
        if not isinstance(inputs, Mapping):
            raise TypeError(
                "inputs must be a dict of input names to values, "
                f"not {type(inputs).__name__}"
            )
        for name, value in inputs.items():
            if not isinstance(name, str):
                raise TypeError(
                    f"Input names must be text, not {type(name).__name__} ({name!r})"
                )
            try:
                encode_json(value)
            except (TypeError, ValueError) as exc:
                raise TypeError(f"Input {name!r}: {exc}") from None
        body = {"inputs": dict(inputs)}
    if idempotency_key is None:
        idempotency_key = new_idempotency_key("workflow-run")
    else:
        idempotency_key_header(idempotency_key)
    return APIRequest(
        "POST",
        f"/workflows/{path_id(workflow_id)}/run-async",
        body=body,
        idempotency_key=idempotency_key,
    )


def _listening(started: Run) -> bool:
    """Whether a run just started is a workflow's trigger listening, which
    never finishes; warned about, since waiting for it would never end."""
    if not started.is_trigger_listener:
        return False
    warn_listening(started, stacklevel=3)
    return True
