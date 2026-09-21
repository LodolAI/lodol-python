# lodol-python

Python SDK for the Lodol Developer API.

Run your Lodol workflows from your own code: give a workflow the values it
asks for, wait for it, and read what it handed back.

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

execution = client.workflows.run(
    "665f...",
    inputs={"email": "ada@example.com"},
    wait=True,
)

print(execution.outputs)   # {"summary": "Sent the welcome email."}
```

`outputs` holds the values the workflow declares it hands back, keyed by name.
A workflow that declares none returns `{}`.

## What a workflow asks for

A workflow states the values it accepts and the values it returns, so you can
read them instead of guessing:

```python
workflow = client.workflows.retrieve("665f...")

for declared in workflow.inputs:
    print(declared.name, declared.type, "required" if declared.required else "optional")

for declared in workflow.outputs:
    print(declared.name, declared.type, declared.description)
```

Passing a name the workflow doesn't ask for, or leaving out one it requires,
raises `BadRequestError` naming the input — the run never starts.

## Start now, read later

Without `wait`, `run()` returns as soon as the run is queued:

```python
execution = client.workflows.run("665f...")
print(execution.id, execution.status)     # "683b...", "queued"

# ...later
execution = client.executions.retrieve("683b...")
if execution.status == "success":
    print(execution.outputs)
else:
    print(execution.status, execution.error)
```

Or wait on a run you already started:

```python
execution = client.executions.wait("683b...")
```

## Waiting

`wait()` polls until the run reaches `success`, `failed`, or `stopped`. It
starts at one second and eases off to eight, so a short run comes back
promptly and a long one costs your workspace about eight requests a minute —
API rate limits are per workspace and shared across all of its keys, so a
tighter poll would spend budget your own code needs.

Two situations end the wait early, because neither resolves on its own:

```python
from lodol import (
    ExecutionNeedsAttentionError,
    ExecutionNotFinishedError,
    ExecutionTimeoutError,
    TriggerWorkflowError,
)

try:
    execution = client.workflows.run("665f...", wait=True, timeout=120)
except ExecutionNeedsAttentionError as exc:
    # Parked until someone responds in Lodol.
    print("Waiting on a person:", exc.execution.status)
except TriggerWorkflowError:
    # Starts from its own trigger: one run per event, so there is no
    # single finish. List its runs instead.
    for run in client.executions.list(workflow_id="665f...", limit=10):
        print(run.id, run.status)
except ExecutionTimeoutError as exc:
    print("Still going:", exc.execution.id)
```

All three are `ExecutionNotFinishedError`, so catch that to handle every way a
wait can end without a finished run. Each carries the run on `.execution`.

Waiting is capped at five minutes by default. Pass `timeout=None` to wait
without a limit.

## Looking into a run

`outputs` is the result. For per-step detail — what each step did on the way
there — ask for it:

```python
execution = client.executions.retrieve("683b...", include_step_results=True)
for step in execution.steps or []:
    print(step["index"], step["result"])
```

## Workflows and runs

```python
workflows = client.workflows.list()
for workflow in workflows:
    print(workflow.id, workflow.name)

execution = workflows[0].run(inputs={"email": "ada@example.com"}, wait=True)

runs = client.executions.list(limit=20)
filtered = client.executions.list(workflow_id="665f...", limit=10)

latest = execution.refresh()
stopped = execution.stop()
```

`executions.list()` is a page, newest first. For the next page, pass the last
run's id as `after`.

## Retries and errors

The client retries transient failures by default — `408`, `409`, `429` and
`5xx`, plus retryable transport errors — honouring `Retry-After` up to a
minute. POST retries are only used when an idempotency key is present, and the
SDK adds one automatically for workflow runs and stops, so a retry can never
start a second run.

```python
from lodol import Lodol, ConcurrencyLimitError, NotFoundError, RateLimitError

client = Lodol(max_retries=3)

try:
    client.workflows.run("665f...")
except NotFoundError:
    print("Workflow not found")
except ConcurrencyLimitError:
    # Your plan's simultaneous runs are all in use. Waiting a moment does
    # not clear this — a running workflow finishing does.
    print("Already at the limit; try once something finishes")
except RateLimitError as exc:
    print("Too many requests; retry in", exc.retry_after, "seconds")
```

`ConcurrencyLimitError` and `RateLimitError` are both `429` but are different
problems, so they are different exceptions and only the second is retried.

Every API error carries `status_code`, `error_code`, `request_id` and the
parsed `body`. Quote `request_id` when reporting a problem:

```python
except APIStatusError as exc:
    print(exc.status_code, exc.error_code, exc.request_id)
```

## Rate limits

After any call, `client.rate_limit` reflects what the API last reported:

```python
print(client.rate_limit.limit, client.rate_limit.remaining)
```

`wait()` reads it too, and stretches its polling when the workspace is nearly
out of budget.

## Pointing somewhere else

The client talks to `https://api-prod.lodol.com/api/v1` unless told otherwise,
by argument or by `LODOL_BASE_URL`:

```python
client = Lodol(base_url="https://api-dev.lodol.com/api/v1")
```

HTTPS is required, except against `localhost`.

## Low-level requests

For new Developer API endpoints before first-class SDK methods exist:

```python
client = Lodol()
body = client.request("GET", "/workflows")
```
