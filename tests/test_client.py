"""Setting up a client: the key, the address, limits, and the escape hatch."""

from __future__ import annotations

import httpx2
import pytest

import lodol
from lodol import ConfigurationError, Lodol
from lodol.constants import USER_AGENT
from tests.conftest import API, FakeAPI, respond


def test_reads_the_key_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_API_KEY", " sk_live_env\n")

    assert Lodol().api_key == "sk_live_env"


def test_an_explicit_key_wins_over_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LODOL_API_KEY", "sk_live_env")

    assert Lodol(api_key="sk_live_arg").api_key == "sk_live_arg"


def test_a_missing_key_says_where_to_get_one() -> None:
    with pytest.raises(ConfigurationError, match="Settings > API Keys"):
        Lodol()


def test_the_key_never_appears_in_the_repr() -> None:
    client = Lodol(api_key="sk_live_secret")

    assert "sk_live_secret" not in repr(client)
    assert repr(client) == f"Lodol(base_url='{API}')"


def test_defaults_to_lodol_cloud() -> None:
    assert Lodol(api_key="k").base_url == API


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("https://lodol.example.com/api/v1", "https://lodol.example.com/api/v1"),
        ("https://lodol.example.com/api/v1/", "https://lodol.example.com/api/v1"),
        # A bare host is the address people copy; the API's path is implied.
        ("https://lodol.example.com", "https://lodol.example.com/api/v1"),
        ("https://lodol.example.com/", "https://lodol.example.com/api/v1"),
        # Lodol Local serves plain HTTP on this machine.
        ("http://localhost:3901", "http://localhost:3901/api/v1"),
        ("http://127.0.0.1:3901/api/v1", "http://127.0.0.1:3901/api/v1"),
        ("http://[::1]:3901", "http://[::1]:3901/api/v1"),
        ("http://lodol.localhost:3901", "http://lodol.localhost:3901/api/v1"),
        # Lodol Local, from a script in a container on the same machine.
        ("http://host.docker.internal:3901", "http://host.docker.internal:3901/api/v1"),
    ],
)
def test_base_url(given: str, expected: str) -> None:
    assert Lodol(api_key="k", base_url=given).base_url == expected


def test_base_url_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LODOL_BASE_URL", "http://localhost:3901")

    assert Lodol(api_key="k").base_url == "http://localhost:3901/api/v1"


@pytest.mark.parametrize(
    "remote", ["http://lodol.example.com", "http://192.168.1.20:3901"]
)
def test_refuses_to_send_the_key_unencrypted_off_this_machine(remote: str) -> None:
    with pytest.raises(ConfigurationError, match="must use https://"):
        Lodol(api_key="k", base_url=remote)


@pytest.mark.parametrize("bad", ["localhost:3901", "api-prod.lodol.com", "ftp://x"])
def test_refuses_an_address_that_isnt_a_url(bad: str) -> None:
    with pytest.raises(ConfigurationError, match="full address"):
        Lodol(api_key="k", base_url=bad)


@pytest.mark.parametrize("timeout", [0, -1, "30", True])
def test_refuses_a_bad_timeout(timeout: object) -> None:
    with pytest.raises(ConfigurationError, match="timeout"):
        Lodol(api_key="k", timeout=timeout)  # type: ignore[arg-type]


@pytest.mark.parametrize("max_retries", [-1, 1.5, None])
def test_refuses_bad_max_retries(max_retries: object) -> None:
    with pytest.raises(ConfigurationError, match="max_retries"):
        Lodol(api_key="k", max_retries=max_retries)  # type: ignore[arg-type]


def test_every_request_identifies_itself(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"workflows": []}))

    client.workflows.list()

    headers = api.last.headers
    assert headers["Authorization"] == "Bearer sk_live_test"
    assert headers["Accept"] == "application/json"
    assert headers["User-Agent"] == USER_AGENT
    assert USER_AGENT.startswith(f"lodol-python/{lodol.__version__} python/3.")
    # Nothing is sent, so nothing is described.
    assert "Content-Type" not in headers
    assert "Idempotency-Key" not in headers


def test_closing_closes_the_connections_it_opened() -> None:
    client = Lodol(api_key="k")

    with client:
        pass

    assert client._http_client.is_closed


def test_leaves_a_given_http_client_open_for_its_owner() -> None:
    http = httpx2.Client()

    with Lodol(api_key="k", http_client=http):
        pass

    assert not http.is_closed
    http.close()


def test_request_reaches_any_endpoint(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(200, {"approvals": [], "next_before": None}))

    body = client.request(
        "get", "/approvals", params={"status": "all", "limit": 5, "skip": None}
    )

    assert body == {"approvals": [], "next_before": None}
    assert api.sent() == ("GET", "/approvals", {"status": "all", "limit": "5"})


def test_request_spells_booleans_the_way_the_api_reads_them(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(respond(200, {"execution_id": "e1", "status": "success"}))

    client.request("GET", "/executions/e1", params={"include_step_results": True})

    assert api.sent()[2] == {"include_step_results": "true"}


def test_request_sends_json_and_an_idempotency_key(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, {"execution_id": "e1"}))

    client.request(
        "POST", "/workflows/w1/run-async", json={"inputs": {}}, idempotency_key="k1"
    )

    assert api.body() == {"inputs": {}}
    assert api.last.headers["Content-Type"] == "application/json"
    assert api.last.headers["Idempotency-Key"] == "k1"


def test_request_returns_none_for_an_empty_answer(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(204))

    assert client.request("DELETE", "/somewhere") is None
