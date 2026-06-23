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

execution = workflow.run(wait=True)
```

## Executions

```python
executions = client.executions.list(limit=20)
filtered = client.executions.list(workflow_id="665f...", limit=10)

execution = client.executions.retrieve("683b...", include_step_results=True)
latest = execution.refresh()
stopped = execution.stop()
```

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
