"""Workflows: list them, look one up, create or change one, run one, delete one."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from lodol._transport import (
    APIRequest,
    APIResponse,
    encode_json,
    expect_list,
    expect_object,
    idempotency_key_for,
    new_idempotency_key,
    path_id,
)
from lodol.models import DeletedWorkflow, Run, Workflow, WorkflowCheck
from lodol.resources._common import object_id
from lodol.resources.runs import check_wait_options, warn_listening

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

    def validate(self, program: Mapping[str, Any]) -> WorkflowCheck:
        """Check a program without saving it: whether it is valid in the
        workspace, and if not, what to fix (``check.error``).

        ``program`` is a workflow's steps, as Lodol stores them
        (``Workflow.program``). For a valid one, ``check.warnings`` names
        likely mistakes, and ``check.raw["preview"]`` has a dry run of it
        against mocked apps. Nothing is saved or run. Needs a key with the
        ``workflows:write`` scope.

        It doesn't check what the key's member may use (connections, and the
        workflows it calls), as saving does, so a program that passes can
        still be refused by ``create``, ``update`` or ``edit``.
        """
        response = self._client._send(_validate_request(program))
        return _parse_check(response)

    def create(
        self, name: str, program: Mapping[str, Any], *, description: str = ""
    ) -> Workflow:
        """Save a new workflow, published, and private to the key's member
        until they share it in Lodol. Creating it doesn't run it.

        ``program`` is its steps, as Lodol stores them (``Workflow.program``);
        ``workflows.validate`` checks one first. A program Lodol can't save
        is refused with ``BadRequestError`` (code ``"invalid_workflow"``)
        saying what to fix, and nothing is saved. The workflow comes back as
        ``workflows.get`` gives it, with where to open it (``url``) and the
        apps it uses that the key's member has yet to connect
        (``apps_to_connect``), which it can't run without.

        Needs a key with the ``workflows:write`` scope, made by a member
        allowed to create workflows. A retried request never saves it twice.
        """
        response = self._client._send(_create_request(name, program, description))
        return _parse_saved(response, "POST /workflows")

    def update(
        self,
        workflow: WorkflowRef,
        *,
        program: Mapping[str, Any] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Workflow:
        """Replace a workflow's steps (``program``), its name or its
        description. What's left out stays as it is. It is saved as an edit
        in Lodol is: as a new version, or, when the workflow was saved within
        the last few minutes, over that version, whose ``version_id`` then
        stays the same.

        A program Lodol can't save, or one with no steps for a workflow that
        has some, is refused with ``BadRequestError`` (code
        ``"invalid_workflow"``), and nothing changes. Saving pauses the
        trigger of a workflow that was listening (``trigger_paused`` says
        so): run it again to set it listening on the new version.

        Needs a key with the ``workflows:write`` scope, made by a member
        allowed to edit it. A retried request never saves it twice.
        """
        request = _update_request(workflow, program, name, description)
        return _parse_saved(self._client._send(request), "PUT /workflows/{id}")

    def edit(
        self,
        workflow: WorkflowRef,
        operations: Sequence[Mapping[str, Any]],
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Workflow:
        """Change a workflow's steps with JSON Patch ``operations``, applied
        in order to its program as it is now, with a new ``name`` or
        ``description`` if given.

        Each operation is a dict such as ``{"op": "replace", "path":
        "/body/0/...", "value": ...}``, with ``"op"`` ``"replace"``,
        ``"add"`` or ``"remove"``. Operations that can't be applied are
        refused with ``BadRequestError``, as is a result Lodol can't save
        (code ``"invalid_workflow"``), and nothing changes. Otherwise it is
        saved as ``update`` saves.
        """
        request = _edit_request(workflow, operations, name, description)
        return _parse_saved(self._client._send(request), "PATCH /workflows/{id}")

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

        A workflow that starts from its own trigger or schedule
        (``workflow.starts_from_trigger``) doesn't run to an end: running it
        sets it listening, and each event it catches starts a run of its own. With ``wait=True`` it returns at once, with
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
        if not wait or _listening(started, workflow):
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

    async def validate(self, program: Mapping[str, Any]) -> WorkflowCheck:
        """Check a program without saving it: whether it is valid in the
        workspace, and if not, what to fix (``check.error``).

        ``program`` is a workflow's steps, as Lodol stores them
        (``Workflow.program``). For a valid one, ``check.warnings`` names
        likely mistakes, and ``check.raw["preview"]`` has a dry run of it
        against mocked apps. Nothing is saved or run. Needs a key with the
        ``workflows:write`` scope.

        It doesn't check what the key's member may use (connections, and the
        workflows it calls), as saving does, so a program that passes can
        still be refused by ``create``, ``update`` or ``edit``.
        """
        response = await self._client._send(_validate_request(program))
        return _parse_check(response)

    async def create(
        self, name: str, program: Mapping[str, Any], *, description: str = ""
    ) -> Workflow:
        """Save a new workflow, published, and private to the key's member
        until they share it in Lodol. Creating it doesn't run it.

        ``program`` is its steps, as Lodol stores them (``Workflow.program``);
        ``workflows.validate`` checks one first. A program Lodol can't save
        is refused with ``BadRequestError`` (code ``"invalid_workflow"``)
        saying what to fix, and nothing is saved. The workflow comes back as
        ``workflows.get`` gives it, with where to open it (``url``) and the
        apps it uses that the key's member has yet to connect
        (``apps_to_connect``), which it can't run without.

        Needs a key with the ``workflows:write`` scope, made by a member
        allowed to create workflows. A retried request never saves it twice.
        """
        request = _create_request(name, program, description)
        return _parse_saved(await self._client._send(request), "POST /workflows")

    async def update(
        self,
        workflow: WorkflowRef,
        *,
        program: Mapping[str, Any] | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> Workflow:
        """Replace a workflow's steps (``program``), its name or its
        description. What's left out stays as it is. It is saved as an edit
        in Lodol is: as a new version, or, when the workflow was saved within
        the last few minutes, over that version, whose ``version_id`` then
        stays the same.

        A program Lodol can't save, or one with no steps for a workflow that
        has some, is refused with ``BadRequestError`` (code
        ``"invalid_workflow"``), and nothing changes. Saving pauses the
        trigger of a workflow that was listening (``trigger_paused`` says
        so): run it again to set it listening on the new version.

        Needs a key with the ``workflows:write`` scope, made by a member
        allowed to edit it. A retried request never saves it twice.
        """
        request = _update_request(workflow, program, name, description)
        return _parse_saved(await self._client._send(request), "PUT /workflows/{id}")

    async def edit(
        self,
        workflow: WorkflowRef,
        operations: Sequence[Mapping[str, Any]],
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Workflow:
        """Change a workflow's steps with JSON Patch ``operations``, applied
        in order to its program as it is now, with a new ``name`` or
        ``description`` if given.

        Each operation is a dict such as ``{"op": "replace", "path":
        "/body/0/...", "value": ...}``, with ``"op"`` ``"replace"``,
        ``"add"`` or ``"remove"``. Operations that can't be applied are
        refused with ``BadRequestError``, as is a result Lodol can't save
        (code ``"invalid_workflow"``), and nothing changes. Otherwise it is
        saved as ``update`` saves.
        """
        request = _edit_request(workflow, operations, name, description)
        return _parse_saved(await self._client._send(request), "PATCH /workflows/{id}")

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

        A workflow that starts from its own trigger or schedule
        (``workflow.starts_from_trigger``) doesn't run to an end: running it
        sets it listening, and each event it catches starts a run of its own. With ``wait=True`` it returns at once, with
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
        if not wait or _listening(started, workflow):
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


def _validate_request(program: Mapping[str, Any]) -> APIRequest:
    # It saves nothing, so it can be sent again whatever happened to it.
    return APIRequest(
        "POST",
        "/workflows/validate",
        body={"program": _program(program)},
        read_only=True,
    )


def _parse_check(response: APIResponse) -> WorkflowCheck:
    return WorkflowCheck.from_api(expect_object(response, "POST /workflows/validate"))


def _create_request(
    name: str, program: Mapping[str, Any], description: str
) -> APIRequest:
    body = {
        "name": _name(name),
        "description": _description(description),
        "program": _program(program),
    }
    return APIRequest(
        "POST",
        "/workflows",
        body=body,
        idempotency_key=new_idempotency_key("workflow-create"),
    )


def _update_request(
    workflow: WorkflowRef,
    program: Mapping[str, Any] | None,
    name: str | None,
    description: str | None,
) -> APIRequest:
    workflow_id = object_id(workflow, Workflow, "workflow")
    body = _new_name_and_description(name, description)
    if program is not None:
        body["program"] = _program(program)
    if not body:
        raise ValueError("Nothing to change: give a new program, name or description")
    return APIRequest(
        "PUT",
        f"/workflows/{path_id(workflow_id)}",
        body=body,
        idempotency_key=new_idempotency_key("workflow-update"),
    )


def _edit_request(
    workflow: WorkflowRef,
    operations: Sequence[Mapping[str, Any]],
    name: str | None,
    description: str | None,
) -> APIRequest:
    workflow_id = object_id(workflow, Workflow, "workflow")
    if not isinstance(operations, (list, tuple)):
        raise TypeError(
            "operations must be a list of JSON Patch operations, not "
            f"{type(operations).__name__}"
        )
    if not operations:
        raise ValueError("operations is empty: give at least one change to make")
    for index, operation in enumerate(operations):
        if not isinstance(operation, Mapping):
            raise TypeError(
                f"operations[{index}] must be a dict such as "
                '{"op": "replace", "path": "/body/0/...", "value": ...}, not '
                f"{type(operation).__name__}"
            )
    body = _new_name_and_description(name, description)
    body["operations"] = [dict(operation) for operation in operations]
    return APIRequest(
        "PATCH",
        f"/workflows/{path_id(workflow_id)}",
        body=body,
        idempotency_key=new_idempotency_key("workflow-edit"),
    )


def _new_name_and_description(
    name: str | None, description: str | None
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = _name(name)
    if description is not None:
        body["description"] = _description(description)
    return body


def _program(program: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(program, Mapping):
        raise TypeError(
            "program must be a dict of the workflow's steps, as Lodol stores "
            f"them (Workflow.program), not {type(program).__name__}"
        )
    return dict(program)


def _name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError(f"name must be text, not {type(name).__name__}")
    if not name.strip():
        raise ValueError("name is empty")
    return name


def _description(description: str) -> str:
    if not isinstance(description, str):
        raise TypeError(f"description must be text, not {type(description).__name__}")
    return description


def _parse_saved(response: APIResponse, context: str) -> Workflow:
    return Workflow.from_api(expect_object(response, context))


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
    check_wait_options(wait, timeout)
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
    return APIRequest(
        "POST",
        f"/workflows/{path_id(workflow_id)}/run-async",
        body=body,
        idempotency_key=idempotency_key_for("workflow-run", idempotency_key),
    )


def _listening(started: Run, workflow: WorkflowRef) -> bool:
    """Whether a run just started is a workflow's trigger listening, which
    never finishes; warned about, since waiting for it would never end."""
    if not started.is_trigger_listener:
        return False
    if not started.workflow_name and isinstance(workflow, Workflow) and workflow.name:
        # An older Lodol doesn't say the workflow's name when it starts a
        # run; the workflow in hand does.
        started = dataclasses.replace(started, workflow_name=workflow.name)
    warn_listening(started, stacklevel=3)
    return True
