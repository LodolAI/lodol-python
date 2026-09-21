from __future__ import annotations

import json as jsonlib
from typing import Any

import pytest
import requests

import lodol
import lodol.constants as constants
from lodol import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    ConcurrencyLimitError,
    ConfigurationError,
    ConflictError,
    ExecutionNeedsAttentionError,
    ExecutionNotFinishedError,
    ExecutionTimeoutError,
    Lodol,
    NotFoundError,
    RateLimitError,
    TriggerWorkflowError,
)
from lodol.client import USER_AGENT


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}
        self.content = b"" if body is None else b"{}"
        self.text = "" if body is None else str(body)

    def json(self) -> Any:
        if self._body == "<invalid-json>":
            raise ValueError("invalid json")
        return self._body


class FakeSession:
    def __init__(self, responses: list[Any]):
        self.responses = list(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.closed = False

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.requests.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response queued")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self) -> None:
        self.closed = True


def make_client(responses: list[Any], **kwargs: Any) -> Lodol:
    kwargs.setdefault("max_retries", 0)
    return Lodol(
        api_key="sk_live_test",
        session=FakeSession(responses),  # type: ignore[arg-type]
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_sleeping(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests assert on what is requested, never on how long it waited."""
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LODOL_API_KEY", raising=False)
    with pytest.raises(ConfigurationError):
        Lodol()


def test_reads_lodol_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_API_KEY", "sk_live_env")
    assert Lodol().api_key == "sk_live_env"


def test_uses_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LODOL_BASE_URL", raising=False)
    client = Lodol(api_key="sk_live_test")
    assert client.base_url == constants.DEFAULT_BASE_URL


def test_base_url_argument_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_BASE_URL", "https://from-env.example.com/api/v1")
    client = Lodol(
        api_key="sk_live_test", base_url="https://explicit.example.com/api/v1"
    )
    assert client.base_url == "https://explicit.example.com/api/v1"


def test_base_url_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_BASE_URL", "https://api-dev.lodol.com/api/v1/")
    assert Lodol(api_key="sk_live_test").base_url == "https://api-dev.lodol.com/api/v1"


def test_rejects_plain_http_base_url() -> None:
    with pytest.raises(ConfigurationError, match="HTTPS"):
        Lodol(api_key="sk_live_test", base_url="http://api.example.com/api/v1")


def test_allows_http_for_localhost() -> None:
    """Running against your own machine, there is no network to leak a key onto."""
    client = Lodol(api_key="sk_live_test", base_url="http://localhost:8000/api/v1")
    assert client.base_url == "http://localhost:8000/api/v1"


def test_requests_use_client_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(
        [FakeResponse(200, {"workflows": []})],
        base_url="https://api-dev.lodol.com/api/v1",
    )
    client.workflows.list()
    _, url, _ = client._session.requests[0]  # type: ignore[attr-defined]
    assert url == "https://api-dev.lodol.com/api/v1/workflows"


def test_with_options_merges_headers_and_carries_base_url() -> None:
    client = make_client([], default_headers={"X-A": "1"})
    derived = client.with_options(default_headers={"X-B": "2"})
    assert derived.default_headers == {"X-A": "1", "X-B": "2"}
    assert derived.base_url == client.base_url


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


def test_list_workflows() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "workflows": [
                        {"id": "w1", "name": "One", "description": "First"},
                        {"id": "w2", "name": "Two"},
                    ]
                },
            )
        ]
    )
    workflows = client.workflows.list()
    assert [w.id for w in workflows] == ["w1", "w2"]
    assert workflows[0].description == "First"


def test_workflow_declares_its_inputs_and_outputs() -> None:
    """Reading a workflow is how a caller learns what to pass to run()."""
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "id": "w1",
                    "name": "Welcome Email",
                    "inputs": [
                        {
                            "name": "email",
                            "type": "text",
                            "required": True,
                            "description": "Who to write to",
                        },
                        {
                            "name": "count",
                            "type": "number",
                            "required": False,
                            "default": 1,
                        },
                    ],
                    "outputs": [{"name": "summary", "type": "text"}],
                },
            )
        ]
    )
    workflow = client.workflows.retrieve("w1")

    assert [i.name for i in workflow.inputs] == ["email", "count"]
    assert workflow.inputs[0].required is True
    assert workflow.inputs[0].description == "Who to write to"
    assert workflow.inputs[1].required is False
    assert workflow.inputs[1].default == 1
    assert [o.name for o in workflow.outputs] == ["summary"]


def test_workflow_without_declarations_reads_as_empty() -> None:
    client = make_client([FakeResponse(200, {"id": "w1", "name": "Plain"})])
    workflow = client.workflows.retrieve("w1")
    assert workflow.inputs == ()
    assert workflow.outputs == ()


def test_retrieve_workflow_aliases_get() -> None:
    client = make_client(
        [
            FakeResponse(200, {"id": "w1", "name": "One"}),
            FakeResponse(200, {"id": "w1", "name": "One"}),
        ]
    )
    assert client.workflows.retrieve("w1") == client.workflows.get("w1")


@pytest.mark.parametrize(
    "body, context",
    [
        (["not", "a", "dict"], "GET /workflows"),
        ({"workflows": "not a list"}, "expected list"),
        ({"workflows": ["not an object"]}, "expected object"),
    ],
)
def test_list_workflows_rejects_unexpected_shapes(body: Any, context: str) -> None:
    client = make_client([FakeResponse(200, body)])
    with pytest.raises(APIResponseValidationError):
        client.workflows.list()


def test_retrieve_workflow_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["nope"])])
    with pytest.raises(APIResponseValidationError):
        client.workflows.retrieve("w1")


# ---------------------------------------------------------------------------
# Running a workflow with its inputs
# ---------------------------------------------------------------------------


def test_run_sends_declared_inputs() -> None:
    client = make_client(
        [FakeResponse(202, {"execution_id": "e1", "status": "queued"})]
    )

    client.workflows.run("w1", inputs={"email": "ada@example.com"})

    method, url, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert method == "POST"
    assert url.endswith("/workflows/w1/run-async")
    assert kwargs["json"] == {"inputs": {"email": "ada@example.com"}}


def test_run_without_inputs_sends_no_body() -> None:
    """A workflow that asks for nothing is started with nothing."""
    client = make_client(
        [FakeResponse(202, {"execution_id": "e1", "status": "queued"})]
    )

    client.workflows.run("w1")

    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["json"] is None


def test_run_adds_idempotency_key() -> None:
    client = make_client(
        [FakeResponse(202, {"execution_id": "e1", "status": "queued"})]
    )
    client.workflows.run("w1")
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["headers"]["Idempotency-Key"].startswith("lodol-workflow-run-")
    assert kwargs["headers"]["User-Agent"] == USER_AGENT


def test_run_uses_custom_idempotency_key() -> None:
    client = make_client(
        [FakeResponse(202, {"execution_id": "e1", "status": "queued"})]
    )
    client.workflows.run("w1", idempotency_key="mine")
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["headers"]["Idempotency-Key"] == "mine"


def test_unknown_input_surfaces_as_bad_request() -> None:
    client = make_client(
        [
            FakeResponse(
                400,
                {
                    "error": "'Welcome Email' has no input named 'emial'. Its inputs are: email."
                },
            )
        ]
    )
    with pytest.raises(BadRequestError, match="emial"):
        client.workflows.run("w1", inputs={"emial": "ada@example.com"})


def test_run_workflow_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(202, ["nope"])])
    with pytest.raises(APIResponseValidationError):
        client.workflows.run("w1")


# ---------------------------------------------------------------------------
# Reading a run
# ---------------------------------------------------------------------------


def test_outputs_are_what_the_workflow_handed_back() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "execution_id": "e1",
                    "status": "success",
                    "outputs": {"summary": "Sent the welcome email."},
                },
            )
        ]
    )
    execution = client.executions.retrieve("e1")
    assert execution.outputs == {"summary": "Sent the welcome email."}


def test_missing_outputs_read_as_empty() -> None:
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "success"})]
    )
    assert client.executions.retrieve("e1").outputs == {}


def test_retrieve_is_lightweight_by_default() -> None:
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "success"})]
    )
    client.executions.retrieve("e1")
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["params"] == {"include_step_results": "false"}


def test_step_results_are_asked_for_explicitly() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "execution_id": "e1",
                    "status": "success",
                    "steps": [{"index": 0, "result": "ok"}],
                },
            )
        ]
    )
    execution = client.executions.retrieve("e1", include_step_results=True)
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["params"] == {"include_step_results": "true"}
    assert execution.steps == [{"index": 0, "result": "ok"}]


def test_list_executions_with_filters() -> None:
    client = make_client([FakeResponse(200, {"executions": [{"execution_id": "e1"}]})])
    client.executions.list(workflow_id="w1", limit=5, after="e0")
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["params"] == {"limit": 5, "workflow_id": "w1", "after": "e0"}


@pytest.mark.parametrize(
    "body",
    [["not a dict"], {"executions": "not a list"}, {"executions": ["not an object"]}],
)
def test_list_executions_rejects_unexpected_shapes(body: Any) -> None:
    client = make_client([FakeResponse(200, body)])
    with pytest.raises(APIResponseValidationError):
        client.executions.list()


def test_retrieve_execution_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["nope"])])
    with pytest.raises(APIResponseValidationError):
        client.executions.retrieve("e1")


def test_stop_execution_adds_idempotency_key() -> None:
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "stopping"})]
    )
    client.executions.stop("e1")
    _, _, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert kwargs["headers"]["Idempotency-Key"].startswith("lodol-execution-stop-")


def test_stop_execution_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["nope"])])
    with pytest.raises(APIResponseValidationError):
        client.executions.stop("e1")


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def test_wait_polls_until_finished() -> None:
    client = make_client(
        [
            FakeResponse(200, {"execution_id": "e1", "status": "queued"}),
            FakeResponse(200, {"execution_id": "e1", "status": "running"}),
            FakeResponse(
                200,
                {
                    "execution_id": "e1",
                    "status": "success",
                    "outputs": {"summary": "done"},
                },
            ),
        ]
    )
    execution = client.executions.wait("e1")
    assert execution.status == "success"
    assert execution.outputs == {"summary": "done"}
    assert len(client._session.requests) == 3  # type: ignore[attr-defined]


def test_wait_never_asks_for_step_results() -> None:
    """outputs is the result; step detail is a separate, explicit request."""
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "success"})]
    )
    client.executions.wait("e1")
    for _, _, kwargs in client._session.requests:  # type: ignore[attr-defined]
        assert kwargs["params"] == {"include_step_results": "false"}


def test_run_with_wait_matches_a_bare_wait() -> None:
    """The same intent must not produce two different requests."""
    run_client = make_client(
        [
            FakeResponse(202, {"execution_id": "e1", "status": "queued"}),
            FakeResponse(200, {"execution_id": "e1", "status": "success"}),
        ]
    )
    run_client.workflows.run("w1", wait=True)
    run_poll = run_client._session.requests[1][2]["params"]  # type: ignore[attr-defined]

    wait_client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "success"})]
    )
    wait_client.executions.wait("e1")
    bare_poll = wait_client._session.requests[0][2]["params"]  # type: ignore[attr-defined]

    assert run_poll == bare_poll


def test_wait_times_out() -> None:
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": "running"})] * 5
    )
    with pytest.raises(ExecutionTimeoutError) as exc:
        client.executions.wait("e1", timeout=0)
    assert exc.value.execution is not None
    assert exc.value.execution.status == "running"


@pytest.mark.parametrize("status", ["paused", "awaiting_review", "awaiting_input"])
def test_wait_stops_on_a_run_waiting_for_a_person(status: str) -> None:
    """Waiting these out would only ever end in a timeout."""
    client = make_client([FakeResponse(200, {"execution_id": "e1", "status": status})])
    with pytest.raises(ExecutionNeedsAttentionError) as exc:
        client.executions.wait("e1")
    assert exc.value.execution.status == status
    assert len(client._session.requests) == 1  # type: ignore[attr-defined]


def test_wait_stops_on_a_trigger_driven_workflow() -> None:
    """A trigger listener starts a run per event; it never finishes itself."""
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "execution_id": "e1",
                    "status": "waiting_for_trigger",
                    "workflow_name": "Nightly Sync",
                },
            )
        ]
    )
    with pytest.raises(TriggerWorkflowError, match="Nightly Sync") as exc:
        client.executions.wait("e1")
    assert exc.value.execution.is_trigger_listener


@pytest.mark.parametrize(
    "status, expected",
    [
        ("running", ExecutionTimeoutError),
        ("awaiting_input", ExecutionNeedsAttentionError),
        ("waiting_for_trigger", TriggerWorkflowError),
    ],
)
def test_every_unfinished_wait_is_one_exception_to_catch(
    status: str, expected: type[Exception]
) -> None:
    client = make_client(
        [FakeResponse(200, {"execution_id": "e1", "status": status})] * 3
    )
    with pytest.raises(ExecutionNotFinishedError) as exc:
        client.executions.wait("e1", timeout=0)
    assert isinstance(exc.value, expected)


def test_wait_rejects_negative_timeout() -> None:
    client = make_client([])
    with pytest.raises(ConfigurationError):
        client.executions.wait("e1", timeout=-1)


def test_polling_eases_off_instead_of_hammering() -> None:
    """A fixed short interval would spend the workspace's whole budget."""
    client = make_client([])
    nominal = []
    delays = []
    interval = constants.POLL_INTERVAL_START_SECONDS
    for _ in range(5):
        nominal.append(interval)
        delays.append(client._poll_delay(interval))
        interval = min(
            constants.POLL_INTERVAL_MAX_SECONDS,
            interval * constants.POLL_INTERVAL_MULTIPLIER,
        )

    assert nominal == [1.0, 2.0, 4.0, 8.0, 8.0]
    # Each delay is its rung plus a little jitter, never less and never a
    # different rung — the ladder is what paces the polling, not the jitter.
    for base, delay in zip(nominal, delays):
        assert base <= delay <= base * (1 + constants.POLL_JITTER_RATIO)
    # Five polls cover more than twenty seconds, so a minute of waiting costs
    # far less than the tightest paid plan's per-minute budget.
    assert sum(delays) > 20


def test_polling_backs_off_when_the_budget_is_nearly_spent() -> None:
    client = make_client([])
    client.rate_limit = lodol.RateLimit(limit=15, remaining=0, reset_seconds=42.0)
    assert client._poll_delay(constants.POLL_INTERVAL_START_SECONDS) >= 42.0


def test_rate_limit_is_read_from_every_response() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {"workflows": []},
                headers={
                    "X-RateLimit-Limit": "25",
                    "X-RateLimit-Remaining": "24",
                    "X-RateLimit-Reset": "37",
                },
            )
        ]
    )
    client.workflows.list()
    assert client.rate_limit.limit == 25
    assert client.rate_limit.remaining == 24
    assert client.rate_limit.reset_seconds == 37.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_code, error_type",
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
        (500, lodol.InternalServerError),
        (418, APIStatusError),
    ],
)
def test_error_mapping(status_code: int, error_type: type[Exception]) -> None:
    client = make_client([FakeResponse(status_code, {"error": "nope"})])
    with pytest.raises(error_type):
        client.workflows.list()


def test_concurrency_limit_is_not_a_rate_limit() -> None:
    """Waiting clears one and not the other, so they are different exceptions."""
    client = make_client(
        [
            FakeResponse(
                429,
                {
                    "error": "Concurrent execution limit reached.",
                    "error_code": "concurrency_limit",
                },
            )
        ]
    )
    with pytest.raises(ConcurrencyLimitError) as exc:
        client.workflows.run("w1")
    assert not isinstance(exc.value, RateLimitError)
    assert exc.value.error_code == "concurrency_limit"


def test_rate_limit_error_parses_retry_after() -> None:
    client = make_client(
        [FakeResponse(429, {"error": "slow down"}, headers={"Retry-After": "12"})]
    )
    with pytest.raises(RateLimitError) as exc:
        client.workflows.list()
    assert exc.value.retry_after == 12.0


def test_errors_carry_the_request_id() -> None:
    client = make_client(
        [
            FakeResponse(
                404, {"error": "Workflow not found"}, headers={"X-Request-Id": "abc123"}
            )
        ]
    )
    with pytest.raises(NotFoundError) as exc:
        client.workflows.retrieve("w1")
    assert exc.value.request_id == "abc123"
    assert "abc123" in str(exc.value)


def test_invalid_success_json_raises_validation_error() -> None:
    client = make_client([FakeResponse(200, "<invalid-json>")])
    with pytest.raises(APIResponseValidationError):
        client.workflows.list()


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_retries_get_transient_response() -> None:
    client = make_client(
        [
            FakeResponse(500, {"error": "temporary"}),
            FakeResponse(200, {"workflows": []}),
        ],
        max_retries=1,
    )
    assert client.workflows.list() == []
    assert len(client._session.requests) == 2  # type: ignore[attr-defined]


def test_retries_conflict_because_the_api_asks_us_to() -> None:
    """409 means an identical request is still in flight, with a Retry-After."""
    client = make_client(
        [
            FakeResponse(
                409, {"error": "still processing"}, headers={"Retry-After": "5"}
            ),
            FakeResponse(200, {"workflows": []}),
        ],
        max_retries=1,
    )
    assert client.workflows.list() == []
    assert len(client._session.requests) == 2  # type: ignore[attr-defined]


def test_does_not_retry_a_concurrency_limit() -> None:
    """A running workflow has to finish first; retrying only spends budget."""
    client = make_client(
        [
            FakeResponse(
                429, {"error": "at the limit", "error_code": "concurrency_limit"}
            ),
            FakeResponse(202, {"execution_id": "e1", "status": "queued"}),
        ],
        max_retries=2,
    )
    with pytest.raises(ConcurrencyLimitError):
        client.workflows.run("w1")
    assert len(client._session.requests) == 1  # type: ignore[attr-defined]


def test_retries_a_plain_rate_limit() -> None:
    client = make_client(
        [
            FakeResponse(429, {"error": "slow down"}),
            FakeResponse(200, {"workflows": []}),
        ],
        max_retries=1,
    )
    assert client.workflows.list() == []
    assert len(client._session.requests) == 2  # type: ignore[attr-defined]


def test_retry_after_is_capped() -> None:
    """Honouring an unbounded wait would park the call with nothing to show."""
    from lodol.client import _retry_after_seconds

    response = FakeResponse(429, {}, headers={"Retry-After": "3600"})
    assert _retry_after_seconds(response) == constants.RETRY_AFTER_MAX_SECONDS


def test_retry_after_accepts_an_http_date() -> None:
    from lodol.client import _retry_after_seconds

    response = FakeResponse(
        429, {}, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
    )
    assert _retry_after_seconds(response) == 0.0


def test_retries_idempotent_post_network_error() -> None:
    client = make_client(
        [
            requests.ConnectionError("boom"),
            FakeResponse(202, {"execution_id": "e1", "status": "queued"}),
        ],
        max_retries=1,
    )
    assert client.workflows.run("w1").id == "e1"
    assert len(client._session.requests) == 2  # type: ignore[attr-defined]


def test_non_idempotent_post_network_error_not_retried() -> None:
    client = make_client([requests.ConnectionError("boom")], max_retries=2)
    with pytest.raises(APIConnectionError):
        client.request("POST", "/anything")
    assert len(client._session.requests) == 1  # type: ignore[attr-defined]
