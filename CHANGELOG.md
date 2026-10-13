# Changelog

## 0.2.0 (unreleased)

The SDK, redesigned for production use, and its first release on PyPI. 0.1 was only ever installable from GitHub; if you used it, see [Upgrading from 0.1](#upgrading-from-01).

### New

- **`AsyncLodol`**: everything `Lodol` does, for asyncio and trio.
- **Approvals.** `lodol.approvals` lists the approval requests runs are waiting on, and answers them with `approve`, `reject` or `reply`.
- **Lists you just loop over.** `runs.list()` and `approvals.list()` fetch pages as you go, newest first; `limit=` stops early.
- **Lodol Local**, and any other address: `base_url=` or `LODOL_BASE_URL`.
- **`RunFailedError`.** Waiting for a run that fails or is stopped raises, with the finished run attached, so a script never carries on as if it worked.
- **`ConcurrencyLimitError` and `RunLimitError`**, both kinds of `RateLimitError`, so "too many runs at once" and "this month's runs are used up" (with `resets_at`) can be told apart from "slow down".
- Every `APIStatusError` carries the server's machine-readable `code`.
- `Workflow.is_draft`. Times are timezone-aware `datetime`s, step results are `Step` objects, and `RunStatus` and `ApprovalStatus` list the statuses.
- Dates, times, `Decimal`s and UUIDs in `inputs` are sent as text Lodol reads, and an input that can't be sent is named before anything is sent.
- `LODOL_LOG=info` or `LODOL_LOG=debug` turns on logging.

### Fixed

- **A workspace that had used up its month's runs made `workflows.run` sleep until they came back, for up to a month.** The client waited out whatever `Retry-After` the server sent. It now raises `RunLimitError` at once, and never waits out a `Retry-After` longer than 60 seconds by itself.
- **A run retried after a timeout could raise `ConflictError` although it had started.** When the first attempt was still starting the run, the retry was refused as "in progress". The client now waits as the server asks and returns that run.
- **Waiting for a run could end in `RateLimitError`.** It checked every 2 seconds and fetched every step's results each time, enough to use up a Pro workspace's 15 requests a minute on its own. It now checks often at first and then every 10 seconds, fetches step results once, at the end, and rides out rate limits and brief outages instead of failing.
- Retries back off with jitter, so clients that failed together don't all retry together.
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
| `APIStatusError.request_id` | Removed: the API doesn't send one |
| `Lodol(session=requests.Session())` | `Lodol(http_client=httpx2.Client(...))` |
| `default_headers=`, `with_options()` | Removed; use `http_client` |
| Timestamps as text | Timezone-aware `datetime`s |
| `DeletedWorkflow.deleted` | Removed: it was always true |
| `executions.stop(idempotency_key=...)` | Removed: every stop gets its own key |
| Python 3.9 | Python 3.10 or later (3.9 reached end of life in October 2025) |
| Built on `requests` | Built on `httpx2` and `anyio` |

Importing `Execution` or `LodolTimeoutError` still works in 0.2, with a `DeprecationWarning`.

### Behind the scenes

- CI tests every supported Python (3.10 to 3.15), the oldest dependency versions the package allows, both async event loops, and every example in the README, which runs as written against a stand-in API.
- The tests are type-checked, as users' code would be, and the package is built and checked as PyPI checks it on every change.
- The action that publishes to PyPI is pinned to a commit.
