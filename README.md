# lodol-python

Python SDK for the Lodol Developer API.

The SDK wraps the Developer API so users can run workflows and inspect executions from Python without hand-writing `curl` or `requests` calls.

## Install

```bash
pip install lodol
```

For local development from this repo:

```bash
pip install -e .[dev]
```

## Configure

`Lodol()` reads `LODOL_API_KEY` from the environment by default.

```bash
export LODOL_API_KEY="sk_live_..."
```

You can also pass the API key explicitly:

```python
from lodol import Lodol

client = Lodol(api_key="sk_live_...")
```

## Run a workflow

```python
from lodol import Lodol

client = Lodol()
execution = client.workflows.run("665f...")

print(execution.id)
print(execution.status)
```

This sends:

```http
POST /api/v1/workflows/{workflow_id}/run-async
Authorization: Bearer sk_live_...
Idempotency-Key: lodol-workflow-run-...
```

The SDK automatically adds an idempotency key for workflow runs and execution stops, so retries do not accidentally duplicate side effects.

A workflow's `id` stays the same when the workflow is edited, so it is safe to store and run later.

## Pass inputs

If a workflow declares inputs, give their values by name, so one workflow can run for a particular client, month, or record:

```python
execution = client.workflows.run(
    "665f...",
    inputs={"client": "Acme", "month": "2026-05"},
)
```

This sends the values as the request body:

```http
POST /api/v1/workflows/{workflow_id}/run-async
Content-Type: application/json

{"inputs": {"client": "Acme", "month": "2026-05"}}
```

`workflow.inputs` lists the inputs a workflow declares, as `WorkflowInput` objects with `name`, `type`, `required`, `default`, and `description`. Values are converted to the declared type, so `"21"` reaches a number input as `21`, and optional inputs you leave out take their default. An input the workflow doesn't declare, a missing required input, or a value of the wrong type raises `BadRequestError` naming the input, and nothing runs. A workflow that starts from its own trigger or schedule takes no inputs.

If you pass your own `idempotency_key`, use a new one for different inputs: reusing a key with a different request raises `UnprocessableEntityError`.

## Wait for completion

```python
execution = client.workflows.run("665f...", wait=True, timeout=120)

if execution.status == "success":
    print("Done")
else:
    print(execution.status, execution.error)
```

Or wait on an existing execution:

```python
execution = client.executions.wait("683b...", timeout=120)
```

`wait()` polls `GET /api/v1/executions/{execution_id}` until the execution reaches a terminal status: `success`, `failed`, or `stopped`.

## Workflows

```python
workflows = client.workflows.list()
workflow = client.workflows.retrieve("665f...")

for workflow in workflows:
    print(workflow.id, workflow.name)
    for workflow_input in workflow.inputs:
        print(" ", workflow_input.name, workflow_input.type, workflow_input.required)

execution = workflow.run(wait=True)
```

## Delete a workflow

```python
deleted = client.workflows.delete("665f...")
print(deleted.id, deleted.name)
```

Or from a workflow you already have:

```python
workflow.delete()
```

This sends `DELETE /api/v1/workflows/{workflow_id}` and removes the workflow and every version of it, as deleting it from the Workflows page does: any run in progress is stopped, and its trigger or schedule stops listening. It cannot be undone. Its past runs stay readable under `client.executions`.

The key needs the `workflows:delete` scope, which is not granted by default: tick **Delete workflows** when creating the key. The member who created the key must also be allowed to delete workflows in the workspace; otherwise the call raises `PermissionDeniedError`. A workflow that doesn't exist, or is already gone, raises `NotFoundError`.

Deletes are not retried. If the request times out, check with `client.workflows.retrieve(...)` before calling again: the delete may have happened.

## Executions

```python
executions = client.executions.list(limit=20)
filtered = client.executions.list(workflow_id="665f...", limit=10)

execution = client.executions.retrieve("683b...", include_step_results=True)
latest = execution.refresh()
stopped = execution.stop()
```

`execution.workflow_id` is the workflow's `id`, and `execution.version_id` is the version of the workflow that ran, which changes as the workflow is edited. Filtering by `workflow_id` returns the runs of every version.

## Retries and errors

The client retries transient failures by default (`408`, `409`, `429`, and `5xx`, plus retryable transport errors). POST retries are only used when an idempotency key is present.

```python
from lodol import Lodol, RateLimitError, NotFoundError

client = Lodol(max_retries=3)

try:
    client.workflows.retrieve("bad-id")
except NotFoundError:
    print("Workflow not found")
except RateLimitError as exc:
    print("Retry after", exc.retry_after)
```

## Low-level requests

For new Developer API endpoints before first-class SDK methods exist:

```python
from lodol import Lodol

client = Lodol()
body = client.request("GET", "/workflows")
```
