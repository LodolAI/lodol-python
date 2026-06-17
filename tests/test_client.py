from __future__ import annotations

from typing import Any

import pytest
import requests

import lodol
import lodol.constants as constants
from lodol import (
    APIConnectionError,
    APIResponseValidationError,
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    Lodol,
    LodolTimeoutError,
    NotFoundError,
    RateLimitError,
)
from lodol.client import USER_AGENT


class FakeResponse:
    def __init__(self, status_code: int, body: Any = None, headers: dict[str, str] | None = None):
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
    return Lodol(
        api_key="sk_live_test",
        session=FakeSession(responses),  # type: ignore[arg-type]
        max_retries=0,
        **kwargs,
    )


def test_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LODOL_API_KEY", raising=False)

    with pytest.raises(ConfigurationError):
        Lodol()


def test_reads_lodol_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_API_KEY", "sk_live_env")
    client = Lodol(session=FakeSession([]))  # type: ignore[arg-type]

    assert client.api_key == "sk_live_env"


def test_uses_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_API_KEY", "sk_live_env")
    monkeypatch.setattr(constants, "DEFAULT_BASE_URL", "https://example.test/api/v1/")

    client = Lodol(session=FakeSession([]))  # type: ignore[arg-type]

    assert client.base_url == "https://example.test/api/v1"


def test_rejects_http_default_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "DEFAULT_BASE_URL", "http://example.test/api/v1")

    with pytest.raises(ConfigurationError, match="must use HTTPS"):
        Lodol(api_key="sk_live_test", session=FakeSession([]))  # type: ignore[arg-type]


def test_rejects_http_default_base_url_at_request_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_client([FakeResponse(200, {"workflows": []})])
    monkeypatch.setattr(constants, "DEFAULT_BASE_URL", "http://example.test/api/v1")

    with pytest.raises(ConfigurationError, match="must use HTTPS"):
        client.workflows.list()


def test_list_workflows() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "workflows": [
                        {
                            "id": "665f",
                            "name": "Daily report",
                            "description": "Send a report",
                        }
                    ]
                },
            )
        ]
    )

    workflows = client.workflows.list()

    assert workflows[0].id == "665f"
    assert workflows[0].name == "Daily report"
    method, url, kwargs = client._session.requests[0]  # type: ignore[attr-defined]
    assert method == "GET"
    assert url == f"{constants.DEFAULT_BASE_URL}/workflows"
    assert kwargs["headers"]["Authorization"] == "Bearer sk_live_test"
    assert kwargs["headers"]["User-Agent"] == f"lodol-python/{lodol.__version__}"
    assert kwargs["headers"]["User-Agent"] == USER_AGENT


def test_list_workflows_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["not", "an", "object"])])

    with pytest.raises(APIResponseValidationError) as exc:
        client.workflows.list()

    assert "GET /workflows returned list, expected object" in str(exc.value)


def test_list_workflows_rejects_non_list_workflows_field() -> None:
    client = make_client([FakeResponse(200, {"workflows": "not-a-list"})])

    with pytest.raises(APIResponseValidationError) as exc:
        client.workflows.list()

    assert "field 'workflows' returned str, expected list" in str(exc.value)


def test_list_workflows_rejects_non_object_items() -> None:
    client = make_client([FakeResponse(200, {"workflows": ["not-an-object"]})])

    with pytest.raises(APIResponseValidationError) as exc:
        client.workflows.list()

    assert "field 'workflows'[0] returned str, expected object" in str(exc.value)


def test_retrieve_workflow_aliases_get() -> None:
    client = make_client(
        [FakeResponse(200, {"id": "665f", "name": "Daily report", "program": {"steps": []}})]
    )

    workflow = client.workflows.get("665f")

    assert workflow.program == {"steps": []}


def test_retrieve_workflow_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["not-an-object"])])

    with pytest.raises(APIResponseValidationError) as exc:
        client.workflows.retrieve("665f")

    assert "GET /workflows/{workflow_id} returned list, expected object" in str(exc.value)


def test_run_workflow_adds_idempotency_key() -> None:
    client = make_client(
        [
            FakeResponse(
                202,
                {
                    "execution_id": "683b",
                    "workflow_id": "665f",
                    "status": "running",
                },
            )
        ]
    )

    execution = client.workflows.run("665f")

    assert execution.execution_id == "683b"
    headers = client._session.requests[0][2]["headers"]  # type: ignore[attr-defined]
    assert headers["Idempotency-Key"].startswith("lodol-workflow-run-")


def test_run_workflow_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(202, ["not-an-object"])])

    with pytest.raises(APIResponseValidationError) as exc:
        client.workflows.run("665f")

    assert (
        "POST /workflows/{workflow_id}/run-async returned list, expected object"
        in str(exc.value)
    )


def test_run_workflow_uses_custom_idempotency_key() -> None:
    client = make_client(
        [
            FakeResponse(
                202,
                {
                    "execution_id": "683b",
                    "workflow_id": "665f",
                    "status": "running",
                },
            )
        ]
    )

    client.workflows.run("665f", idempotency_key="job-123")

    headers = client._session.requests[0][2]["headers"]  # type: ignore[attr-defined]
    assert headers["Idempotency-Key"] == "job-123"


def test_get_execution_with_step_results() -> None:
    client = make_client(
        [
            FakeResponse(
                200,
                {
                    "execution_id": "683b",
                    "workflow_id": "665f",
                    "status": "success",
                    "steps": [{"index": 0, "status": "completed"}],
                },
            )
        ]
    )

    execution = client.executions.retrieve("683b", include_step_results=True)

    assert execution.status == "success"
    assert execution.steps == [{"index": 0, "status": "completed"}]
    assert client._session.requests[0][2]["params"] == {  # type: ignore[attr-defined]
        "include_step_results": "true"
    }


def test_list_executions_with_filters() -> None:
    client = make_client([FakeResponse(200, {"executions": [{"execution_id": "e1", "status": "success"}]})])

    executions = client.executions.list(workflow_id="w1", limit=5, after="e0")

    assert executions[0].id == "e1"
    assert client._session.requests[0][2]["params"] == {  # type: ignore[attr-defined]
        "workflow_id": "w1",
        "limit": 5,
        "after": "e0",
    }


def test_list_executions_rejects_non_object_items() -> None:
    client = make_client([FakeResponse(200, {"executions": ["not-an-object"]})])

    with pytest.raises(APIResponseValidationError) as exc:
        client.executions.list()

    assert "field 'executions'[0] returned str, expected object" in str(exc.value)


def test_list_executions_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, ["not-an-object"])])

    with pytest.raises(APIResponseValidationError) as exc:
        client.executions.list()

    assert "GET /executions returned list, expected object" in str(exc.value)


def test_retrieve_execution_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, "not-an-object")])

    with pytest.raises(APIResponseValidationError) as exc:
        client.executions.retrieve("683b")

    assert "GET /executions/{execution_id} returned str, expected object" in str(exc.value)


def test_stop_execution_adds_idempotency_key() -> None:
    client = make_client([FakeResponse(200, {"execution_id": "e1", "status": "stopping"})])

    execution = client.executions.stop("e1")

    assert execution.status == "stopping"
    headers = client._session.requests[0][2]["headers"]  # type: ignore[attr-defined]
    assert headers["Idempotency-Key"].startswith("lodol-execution-stop-")


def test_stop_execution_rejects_non_object_response() -> None:
    client = make_client([FakeResponse(200, "not-an-object")])

    with pytest.raises(APIResponseValidationError) as exc:
        client.executions.stop("e1")

    assert (
        "POST /executions/{execution_id}/stop returned str, expected object"
        in str(exc.value)
    )


def test_wait_polls_until_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(
        [
            FakeResponse(200, {"execution_id": "683b", "status": "running"}),
            FakeResponse(200, {"execution_id": "683b", "status": "success"}),
        ]
    )
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)

    execution = client.executions.wait("683b", poll_interval=0.01)

    assert execution.status == "success"
    assert len(client._session.requests) == 2  # type: ignore[attr-defined]


def test_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    client = make_client(
        [
            FakeResponse(200, {"execution_id": "683b", "status": "running"}),
        ]
    )
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)

    with pytest.raises(LodolTimeoutError):
        client.executions.wait("683b", poll_interval=1, timeout=0)


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, AuthenticationError),
        (404, NotFoundError),
        (409, ConflictError),
        (429, RateLimitError),
    ],
)
def test_error_mapping(status_code: int, error_type: type[Exception]) -> None:
    client = make_client(
        [
            FakeResponse(
                status_code,
                {"error": "API failed"},
                headers={"Retry-After": "5"},
            )
        ]
    )

    with pytest.raises(error_type) as exc:
        client.workflows.list()

    assert getattr(exc.value, "status_code") == status_code
    assert str(exc.value) == "API failed"


def test_invalid_success_json_raises_validation_error() -> None:
    client = make_client([FakeResponse(200, "<invalid-json>")])

    with pytest.raises(APIResponseValidationError):
        client.workflows.list()


def test_retries_get_transient_response(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([
        FakeResponse(500, {"error": "temporary"}),
        FakeResponse(200, {"workflows": []}),
    ])
    client = Lodol(
        api_key="sk_live_test",
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)

    assert client.workflows.list() == []
    assert len(session.requests) == 2


def test_does_not_retry_conflict_response(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([
        FakeResponse(409, {"error": "Idempotency key conflict"}),
        FakeResponse(200, {"workflows": []}),
    ])
    client = Lodol(
        api_key="sk_live_test",
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)

    with pytest.raises(ConflictError):
        client.workflows.list()

    assert len(session.requests) == 1


def test_retries_idempotent_post_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession([
        requests.ConnectionError("down"),
        FakeResponse(202, {"execution_id": "e1", "workflow_id": "w1", "status": "running"}),
    ])
    client = Lodol(
        api_key="sk_live_test",
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )
    monkeypatch.setattr("lodol.client.time.sleep", lambda _seconds: None)

    execution = client.workflows.run("w1", idempotency_key="same-job")

    assert execution.id == "e1"
    assert len(session.requests) == 2


def test_non_idempotent_post_network_error_not_retried() -> None:
    session = FakeSession([requests.ConnectionError("down")])
    client = Lodol(
        api_key="sk_live_test",
        session=session,  # type: ignore[arg-type]
        max_retries=1,
    )

    with pytest.raises(APIConnectionError):
        client.request("POST", "/custom")

    assert len(session.requests) == 1


def test_with_options_merges_headers() -> None:
    client = Lodol(
        api_key="sk_live_test",
        default_headers={"X-A": "1"},
    )

    other = client.with_options(default_headers={"X-B": "2"}, timeout=5)

    assert other.timeout == 5
    assert other.default_headers == {"X-A": "1", "X-B": "2"}
