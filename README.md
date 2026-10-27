# Lodol for Python

Run your Lodol workflows from Python: start runs, wait for them, follow their history, and answer the approvals they ask for.

```bash
pip install lodol
```

Python 3.10 or later.

## Quickstart

Create an API key in Lodol under **Settings > API Keys**, then:

```bash
export LODOL_API_KEY="sk_live_..."
```

```python
from lodol import Lodol

lodol = Lodol()

run = lodol.workflows.run("665f...", inputs={"client": "Acme"}, wait=True)
print(run.outputs)  # {"total": 1250}
```

That starts the workflow, waits for it to finish, and gives you what it handed back: `run.outputs` holds each output the workflow declares, by name. If the run fails or is stopped, it raises `RunFailedError` instead, so a script never carries on as if it worked.

Make one `Lodol()` and reuse it. It keeps connections open between calls and can be shared between threads.

## Run a workflow

```python
workflow = lodol.workflows.get("665f...")

for workflow_input in workflow.inputs:
    print(workflow_input.name, workflow_input.type, workflow_input.required)
for output in workflow.outputs:
    print(output.name, output.type)  # what run.outputs will hold

run = lodol.workflows.run(workflow, inputs={"client": "Acme", "month": "2026-05"})
print(run.id, run.status)  # "683b...", "queued"
```

Without `wait=True`, `run` returns as soon as the run is queued. A run started while the workspace already has as many going as its plan allows comes back `queued` too, and starts when one of them finishes. A workflow's `id` stays the same when it's edited, so it's safe to store.

**Inputs** are given by name and converted to each input's type, so `"21"` reaches a number input as `21`, and an optional input you leave out takes its default. Dates, times and `Decimal`s are sent as text Lodol reads (`date(2026, 5, 1)` becomes `"2026-05-01"`). An input the workflow doesn't declare, a missing required one, or a value of the wrong type raises `BadRequestError` naming the input, and nothing runs.

**Running something exactly once.** A retried request never starts a second run. To make sure a run happens once even across separate attempts, such as a nightly job that may be restarted, pass the same `idempotency_key` each time:

```python
lodol.workflows.run(
    "665f...",
    inputs={"client": "Acme", "month": "2026-05"},
    idempotency_key="monthly-report:acme:2026-05",
)
```

Within 24 hours, the same key with the same inputs returns the run it already started, even if the first attempt is still under way. The same key with different inputs raises `UnprocessableEntityError`. Any text up to 256 characters works as a key.

**Workflows that start themselves.** A workflow that starts from its own trigger or schedule doesn't run to an end: running it sets it listening, and each event it catches starts a run of its own, which `runs.list` lists. The listening run has `run.is_trigger_listener` set, and its status reads `"running"` for as long as it listens. Waiting on it, with `wait=True` or `runs.wait`, returns it at once, with a warning, since there's no end to wait for.

## Wait for a run

```python
from lodol import RunFailedError, WaitTimeoutError

try:
    run = lodol.runs.wait("683b...", timeout=600)
except RunFailedError as error:
    print("It didn't work:", error.run.error)
except WaitTimeoutError:
    print("Still going after 10 minutes. It carries on in Lodol.")
else:
    print(run.outputs)
    for step in run.steps or []:
        print(step.index, step.status, step.result)
```

`wait` checks on the run often at first, then less often, at most every 10 seconds, and never faster than your plan's API allowance (the Free plan's 5 requests a minute means a check every 12 seconds). Rate limits and brief outages don't end a wait: it slows down and keeps going, and gives up only when `timeout` passes, or after five minutes without a successful check.

The finished run comes with its outputs (`run.outputs`) and its step results (`run.steps`). An output the run never set is `None`. `RunFailedError` carries the run too, as `error.run`, so you can see which step failed. If the run has finished but its step results can't be fetched in time, you get the run without them rather than an error. Without a `timeout`, `wait` waits as long as the run takes, including while it waits for someone to answer an approval.

## Follow runs

```python
for run in lodol.runs.list(workflow="665f...", limit=20):
    print(run.created_at, run.status)
```

Runs come newest first. Loop over the result and pages are fetched as you go, so stopping early costs nothing extra. Leave out `limit` to go through all of them, and `workflow` to see every workflow's runs. Listed runs leave out `outputs` and `steps`; `runs.get` has them. A failed run says why in `run.error`, listed or not.

```python
run = lodol.runs.get("683b...", include_steps=True)
lodol.runs.stop(run)
```

`run.workflow_id` is the workflow's `id`. `run.version_id` is the version of the workflow that ran, which changes as the workflow is edited. Times are timezone-aware `datetime`s.

Stopping a run doesn't undo the steps it already finished. If the run had already finished, `stop` returns it as it finished rather than raising.

```python
run = lodol.runs.get("683b...")
lodol.runs.stop(run)
lodol.runs.delete(run)
```

Deleting a run removes it from the history, as deleting it in Lodol does, along with the runs it started: its trigger's, or the workflows it called. It can't be undone. A run still going has to be stopped first, or deleting it raises `ConflictError` with `code` `"run_not_finished"`. It needs a key with the **Delete runs** scope, which isn't ticked by default, made by a member who's allowed to delete runs. Like a workflow delete, it's never sent twice.

## Answer approvals

A workflow's **Request approval** step pauses its run until someone answers. Your own system can answer instead of a person in Lodol:

```python
for approval in lodol.approvals.list():  # pending ones, newest first
    print(approval.title, approval.details, approval.workflow_name)

    if approval.kind == "reply":
        lodol.approvals.reply(approval, "The PO number is 4471")
    else:
        lodol.approvals.approve(approval, comment="Matches the purchase order")
        # or lodol.approvals.reject(approval, comment="Wrong amount")
```

The run carries on as soon as it's answered. An answer sent with an API key is recorded as that key's. If someone else answered first, or the request expired, you get `ConflictError`. `approvals.list(status="all")` includes answered requests, with `approval.decision` saying who answered, when and how.

## Workflows

```python
workflows = lodol.workflows.list()
drafts = [workflow for workflow in workflows if workflow.is_draft]

deleted = lodol.workflows.delete("665f...")
```

Deleting removes the workflow and every version of it, as deleting it in Lodol does: runs in progress stop, and its trigger or schedule stops listening. It can't be undone. Its past runs stay readable. It needs a key with the **Delete workflows** scope, which isn't ticked by default, made by a member who's allowed to delete workflows.

A delete is never sent twice. If it times out, check with `lodol.workflows.get(...)` before trying again, since it may have happened.

## Errors

When Lodol or the network says no, the SDK raises a `LodolError`. Errors from the API carry Lodol's own explanation as their message, and `code`, a stable name for what went wrong (such as `"run_already_finished"`): decide what to do from `code`, since the wording may change. `request_id` names the request; it's also in the error's text, so quote that when you contact Lodol's support. A call that can't be made as written (inputs that aren't a dict, a timeout that isn't a number) raises `TypeError` or `ValueError` before anything is sent.

| Exception | When | What to do |
| --- | --- | --- |
| `RunFailedError` | A run you waited for failed or was stopped | Look at `error.run` |
| `WaitTimeoutError` | A run wasn't finished within `timeout` | Wait again, or stop it. The run carries on |
| `BadRequestError` | The request was refused, such as inputs that don't fit | Read the message: it names the problem |
| `AuthenticationError` | The key is missing, wrong, revoked or expired | Check `LODOL_API_KEY` |
| `PermissionDeniedError` | The key's scopes, or its creator's role, don't allow this | Give the key the scope, or use another key |
| `NotFoundError` | No such workflow, run or approval in this workspace | Check the id |
| `ConflictError` | An approval was already answered; or Lodol was still working on an earlier attempt of a request after five minutes | For an approval, nothing. Otherwise check before trying again: the message has the key to retry with |
| `RunLimitError` | This month's runs are used up | Runs come back at `error.resets_at` |
| `RateLimitError` | Too many requests, for longer than the SDK waits on its own | Wait `error.retry_after` seconds |
| `APIConnectionError`, `APITimeoutError` | Lodol couldn't be reached, didn't answer, or had a certificate that couldn't be verified | Try again later; for a certificate, see the message |

## What happens automatically

- **Retries.** A request that fails for a passing reason (a dropped connection, a server error, a rate limit that clears within a minute) is sent again, up to `max_retries` times, with growing pauses. Only requests that can't take effect twice are retried: reads; runs, stops and approval answers, which carry an idempotency key; and anything a rate limit refused, since Lodol refuses before doing anything. A delete that fails otherwise, and limits that last longer, are reported at once.
- **Idempotency keys.** Every run, stop and approval answer gets one, so a retry can never start a second run or answer twice. If a retry finds Lodol still working on the first attempt, it waits for that, up to five minutes, rather than starting again.
- **Timeouts.** Each request may take 30 seconds (`timeout`). Waiting for a run has its own `timeout`.
- **Security.** Your key is only sent over HTTPS, except to Lodol Local on your own machine, which is reached directly, never through a proxy. It never appears in logs or reprs.

## Async

```python
from lodol import AsyncLodol

async with AsyncLodol() as lodol:
    run = await lodol.workflows.run("665f...", inputs={"client": "Acme"}, wait=True)

    async for approval in lodol.approvals.list():
        print(approval.title)
```

`AsyncLodol` does everything `Lodol` does, with every call awaited and lists looped over with `async for`. It works with asyncio and trio.

## Lodol Local

Point the client at your own installation:

```python
from lodol import Lodol

lodol = Lodol(base_url="http://localhost:3901")
```

or set `LODOL_BASE_URL`. Plain `http://` is allowed only for this machine (`localhost`, or `host.docker.internal` from a container); anything else must be `https://`.

## Settings

| Argument | Environment variable | Default |
| --- | --- | --- |
| `api_key` | `LODOL_API_KEY` | (required) |
| `base_url` | `LODOL_BASE_URL` | `https://api-prod.lodol.com/api/v1` |
| `timeout` | | `30` seconds per request |
| `max_retries` | | `2` |
| `http_client` | | Your own `httpx2.Client` (or `httpx2.AsyncClient`) for proxies, custom certificates or tests |

Proxies and custom certificates are also picked up from the usual environment variables (`HTTPS_PROXY`, `SSL_CERT_FILE`).

## Logging

Set `LODOL_LOG=info` to see retries and what runs you're waiting for are doing, or `LODOL_LOG=debug` to see every request too. Or configure the `lodol` logger with Python's `logging`. Requests are logged by method, path and status only, never with your key or the inputs you send.

## Testing your code

Give the client an `httpx2` mock transport and answer as Lodol would:

```python
import httpx2
from lodol import Lodol


def fake_lodol(request: httpx2.Request) -> httpx2.Response:
    if request.url.path.endswith("/run-async"):
        return httpx2.Response(202, json={"execution_id": "683b", "status": "queued"})
    return httpx2.Response(404, json={"error": "Workflow not found"})


lodol = Lodol(
    api_key="sk_live_test",
    http_client=httpx2.Client(transport=httpx2.MockTransport(fake_lodol)),
)
```

Models are plain data, so you can also build them directly: `Run(id="683b", status="success")`. They can be compared, hashed and pickled, and so can the SDK's errors.

## Endpoints the SDK doesn't cover yet

```python
body = lodol.request("GET", "/approvals", params={"status": "all"})
```

`request` reaches any Developer API endpoint and returns its JSON. Errors raise as they do everywhere else. A write is retried only when you give it an `idempotency_key`.

## Upgrading from 0.1

0.2 renames executions to runs, to match Lodol, and changes a few behaviors. See [CHANGELOG.md](CHANGELOG.md) for the full list.
