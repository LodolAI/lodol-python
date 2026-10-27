# Changelog

## 0.2.0 (unreleased)

The SDK, redesigned for production use, and its first release on PyPI. 0.1 was only ever installable from GitHub; if you used it, see [Upgrading from 0.1](#upgrading-from-01).

### New

- **Outputs.** `run.outputs` holds what a run handed back, by the names its workflow declares, and `workflow.outputs` lists them, so a script reads results instead of digging through step results.
- **`AsyncLodol`**: everything `Lodol` does, for asyncio and trio.
- **Approvals.** `lodol.approvals` lists the approval requests runs are waiting on, and answers them with `approve`, `reject` or `reply`.
- **Deleting runs.** `runs.delete(run)` removes a finished run, and the runs it started, from the history. It needs a key with the new **Delete runs** scope.
- **Retrying a failed run.** `runs.retry(run)` runs it again with the data it started with, picking up at the step that failed without running the steps before it again, or, with `from_start=True`, from the top. `run.retry_options` says which retries a run offers, and a retry's `run.retry_of` names the run it retried.
- **Lists you just loop over.** `runs.list()` and `approvals.list()` fetch pages as you go, newest first; `limit=` stops early.
- **Finding runs.** `runs.list()` narrows to runs in a status or several (`status=`), that started in a time range (`started_from=`, `started_to=`), or whose workflow's name or data matches (`search=`). A status runs can't be listed by raises `ValueError` before anything is sent, since Lodol would ignore it and list every run.
- **Lodol Local**, and any other address: `base_url=` or `LODOL_BASE_URL`.
- **`RunFailedError`.** Waiting for a run that fails or is stopped raises, with the finished run attached, so a script never carries on as if it worked.
- **`RunLimitError`**, a kind of `RateLimitError`, so "this month's runs are used up" (with `resets_at`) can be told apart from "slow down".
- Every `APIStatusError` carries the server's machine-readable `code`, which the SDK itself now goes by, never the wording, and `request_id`, which is also in the error's text, so it comes along when an error is pasted into a support request.
- `Workflow.is_draft`. Times are timezone-aware `datetime`s, step results are `Step` objects, which say why a step was skipped (`reason`) and how many tries it took (`attempts`), and `RunStatus`, `StepStatus` and `ApprovalStatus` list the statuses. A step that failed but was let carry on is `"continued"`, and never taken for the step that failed a run.
- Dates, times, `Decimal`s and UUIDs in `inputs` are sent as text Lodol reads, and an input that can't be sent is named before anything is sent.
- Any text works as an `idempotency_key`, and the same inputs are always sent byte for byte the same way, so a key always recognizes its request.
- A workflow that starts from its own trigger, run with `wait=True` or waited on with `runs.wait`, returns at once with a warning: its run is it listening, and never ends. `run.is_trigger_listener` says so, since its status reads `"running"` the whole time. A failed run says why in `run.error` in `runs.list` too.
- Models can be hashed, and errors can be pickled, so both cross process boundaries (multiprocessing, task queues).
- `LODOL_LOG=info` or `LODOL_LOG=debug` turns on logging.

0.2 needs Lodol as of October 2026, which sends the error codes, outputs, `is_trigger_listener`, `retry_options` and `request_id` the SDK relies on, retries runs, and filters runs by status, start time and search. Update an older Lodol Local before upgrading.

### Fixed

- **A workspace that had used up its month's runs made `workflows.run` sleep until they came back, for up to a month.** The client waited out whatever `Retry-After` the server sent. It now raises `RunLimitError` at once, and never waits out a `Retry-After` longer than 60 seconds by itself.
- **A run retried after a timeout could raise `ConflictError` although it had started.** When the first attempt was still starting the run, the retry was refused as "in progress". The client now waits for that attempt, as the server asks, for up to five minutes, and returns its run.
- **Waiting for a run could end in `RateLimitError`.** It checked every 2 seconds and fetched every step's results each time: 30 requests a minute, six times the Free plan's allowance. It now checks often at first, then every 10 seconds, never faster than the workspace's allowance, and fetches step results once, at the end. It rides out rate limits and brief outages, keeps to its `timeout`, and never turns a finished run into an error because its step results couldn't be fetched.
- Retries back off with jitter, so clients that failed together don't all retry together. A request a rate limit refused is retried whatever it was, since Lodol refuses before doing anything; a certificate that can't be verified fails at once, with a hint.
- Lodol Local is reached directly, never through a proxy from the environment, which would have seen the key in plain HTTP.
- An error from a proxy no longer makes its HTML page the error message; the page stays on `error.body`.
- Printing a `Workflow` no longer prints its whole program.

### Upgrading from 0.1

| 0.1 | 0.2 |
| --- | --- |
| `client.executions` | `client.runs` |
| `Execution`, `execution.execution_id` | `Run`, `run.id` |
| `retrieve(...)` (and its alias `get(...)`) | `get(...)` |
| `include_step_results=` | `include_steps=` |
| `executions.list(workflow_id=..., limit=..., after=...)`, one page | `runs.list(workflow=..., limit=...)`, every page as you loop |
| `workflow.run()`, `workflow.delete()`, `execution.wait()`, `.refresh()`, `.stop()` | `lodol.workflows.run(workflow)`, `lodol.workflows.delete(workflow)`, `lodol.runs.wait(run)`, `lodol.runs.get(run)`, `lodol.runs.stop(run)`. Every method takes an id or the object |
| `wait()` returns a failed or stopped run | It raises `RunFailedError`; the run is `error.run` |
| `wait(poll_interval=...)` | Gone: waiting paces itself |
| `LodolTimeoutError` | `WaitTimeoutError`, which is also a `TimeoutError` |
| `RateLimitError.retry_after` as text | Seconds, as a number |
| `Lodol(session=requests.Session())` | `Lodol(http_client=httpx2.Client(...))` |
| `default_headers=`, `with_options()` | Removed; use `http_client` |
| Timestamps as text | Timezone-aware `datetime`s |
| `DeletedWorkflow.deleted` | Removed: it was always true |
| `executions.stop(idempotency_key=...)` | Removed: every stop gets its own key |
| Python 3.9 | Python 3.10 or later (3.9 reached end of life in October 2025) |
| Built on `requests` | Built on `httpx2` and `anyio` |

Importing `Execution` or `LodolTimeoutError` still works in 0.2, with a `DeprecationWarning`.

### Behind the scenes

- CI tests every supported Python (3.10 to 3.15), the oldest dependency versions the package allows, both async event loops, and every example in the README, which runs as written against a stand-in API that answers as the server's own code does.
- The source distribution includes the tests whole, so they run from it.
- The tests are type-checked, as users' code would be, and the package is built and checked as PyPI checks it on every change.
- The action that publishes to PyPI is pinned to a commit.
