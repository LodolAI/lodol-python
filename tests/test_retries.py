"""When a request is sent again, and when the answer is final.

Each rule follows how the Developer API behaves: which failures pass, which
requests can't take effect twice, and what Retry-After means on each status.
"""

from __future__ import annotations

import datetime as dt
import logging
import ssl
from email.utils import format_datetime

import httpx2
import pytest

from lodol import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    ConcurrencyLimitError,
    ConflictError,
    InternalServerError,
    Lodol,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    RunLimitError,
)
from lodol import _transport
from tests.conftest import FakeAPI, FakeClock, respond, run_json

RUN_STARTED = respond(202, run_json())


def test_a_read_that_hits_a_server_error_is_retried(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(respond(500, {"error": "temporary"}), respond(200, {"workflows": []}))

    assert client.workflows.list() == []

    assert len(api.requests) == 2
    # The first backoff: half a second, less up to a quarter for jitter.
    assert len(clock.sleeps) == 1 and 0.375 <= clock.sleeps[0] <= 0.5


def test_gives_up_after_max_retries(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[respond(503, {"error": "Down for a moment"})] * 3)

    with pytest.raises(InternalServerError, match="Down for a moment") as exc:
        client.workflows.list()

    assert exc.value.status_code == 503
    assert len(api.requests) == 3
    assert len(clock.sleeps) == 2
    assert 0.75 <= clock.sleeps[1] <= 1.0


def test_max_retries_zero_sends_once(api: FakeAPI, clock: FakeClock) -> None:
    http = httpx2.Client(transport=httpx2.MockTransport(api.handle))
    client = Lodol(api_key="k", http_client=http, max_retries=0)
    api.queue(respond(500, {"error": "temporary"}))

    with pytest.raises(InternalServerError):
        client.workflows.list()

    assert len(api.requests) == 1


def test_a_run_is_retried_with_the_same_key_and_inputs(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(respond(502), RUN_STARTED)

    client.workflows.run("665f", inputs={"client": "Acme"})

    first, second = api.requests
    assert first.headers["Idempotency-Key"] == second.headers["Idempotency-Key"]
    assert first.content == second.content


def test_a_write_without_a_key_is_never_sent_twice(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(500, {"error": "temporary"}))

    with pytest.raises(InternalServerError):
        client.request("POST", "/custom")

    assert len(api.requests) == 1


def test_any_request_is_retried_when_it_never_left(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.ConnectError("refused"), respond(200, {"ok": True}))

    assert client.request("POST", "/custom") == {"ok": True}


def test_a_write_without_a_key_isnt_retried_after_a_timeout(
    client: Lodol, api: FakeAPI
) -> None:
    # The request went out; it may have done its work before the answer was lost.
    api.queue(httpx2.ReadTimeout("slow"))

    with pytest.raises(APITimeoutError, match="no answer from Lodol within 30 seconds"):
        client.request("POST", "/custom")

    assert len(api.requests) == 1


def test_a_read_is_retried_after_a_timeout(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.ReadTimeout("slow"), respond(200, {"workflows": []}))

    assert client.workflows.list() == []


def test_a_connection_that_keeps_failing_says_where_it_was_going(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(*[httpx2.ConnectError("refused")] * 3)

    with pytest.raises(APIConnectionError) as exc:
        client.workflows.list()

    assert not isinstance(exc.value, APITimeoutError)
    assert "Couldn't reach Lodol at https://api-prod.lodol.com/api/v1" in str(exc.value)
    assert "GET /workflows" in str(exc.value)


def test_a_delete_is_never_sent_twice(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(500, {"error": "Failed to delete workflow"}))

    with pytest.raises(InternalServerError):
        client.workflows.delete("665f")

    assert len(api.requests) == 1


def test_a_delete_that_never_left_is_retried(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.ConnectError("refused"), respond(200, {"id": "665f", "name": "x"}))

    assert client.workflows.delete("665f").id == "665f"


def test_waits_out_a_rate_limit_as_long_as_asked(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(429, {"error": "Rate limit exceeded."}, {"Retry-After": "7"}),
        respond(200, {"workflows": []}),
    )

    client.workflows.list()

    assert clock.sleeps == [7.0]


def test_a_rate_limit_that_wont_clear_soon_is_reported_at_once(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(respond(429, {"error": "Slow down"}, {"Retry-After": "120"}))

    with pytest.raises(RateLimitError) as exc:
        client.workflows.list()

    assert exc.value.retry_after == 120.0
    assert clock.sleeps == []


def test_a_rate_limit_that_persists_is_raised_after_retries(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(*[respond(429, {"error": "Slow down"}, {"Retry-After": "1"})] * 3)

    with pytest.raises(RateLimitError):
        client.workflows.list()

    assert clock.sleeps == [1.0, 1.0]


def test_a_months_runs_used_up_is_reported_not_slept_through(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """0.1 slept for Retry-After whatever it said: here, until next month."""
    api.queue(
        respond(
            429,
            {
                "error": "You've used all 2,000 runs your plan includes this month.",
                "error_code": "monthly_run_limit",
                "limit": 2000,
                "extra_runs_spend_limit": None,
                "resets_at": "2026-11-01T00:00:00+00:00",
            },
            {"Retry-After": "1555200"},
        )
    )

    with pytest.raises(RunLimitError) as exc:
        client.workflows.run("665f")

    assert clock.sleeps == []
    assert len(api.requests) == 1
    error = exc.value
    assert isinstance(error, RateLimitError)
    assert error.code == "monthly_run_limit"
    assert error.limit == 2000
    assert error.resets_at == dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    assert error.retry_after == 1555200.0


@pytest.mark.parametrize(
    "body",
    [
        # Where the run starts, with the code ...
        {
            "error": "Your plan allows 5 workflows to run at once.",
            "error_code": "concurrency_limit",
        },
        # ... and from the check before it, with only the message.
        {
            "error": "Concurrent execution limit reached. Your plan allows 5 "
            "concurrent executions via the API."
        },
    ],
)
def test_too_many_runs_at_once_is_reported_not_retried(
    client: Lodol, api: FakeAPI, body: dict[str, str]
) -> None:
    api.queue(respond(429, body))

    with pytest.raises(ConcurrencyLimitError) as exc:
        client.workflows.run("665f")

    assert exc.value.code == "concurrency_limit"
    assert len(api.requests) == 1


def test_a_run_still_in_flight_under_its_key_is_waited_for(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """The first attempt timed out but is still starting the run. The retry
    hears 409 + Retry-After, and waits until it can replay that run's answer
    rather than raising a conflict about a run that did start (as 0.1 did)."""
    api.queue(
        httpx2.ReadTimeout("slow"),
        respond(
            409,
            {
                "error": "A request with this Idempotency-Key is currently being processed."
            },
            {"Retry-After": "5"},
        ),
        httpx2.Response(
            202,
            json=run_json(status="queued"),
            headers={"X-Idempotent-Replayed": "true"},
        ),
    )

    run = client.workflows.run("665f")

    assert run.id == "683b"
    assert len(api.requests) == 3
    assert clock.sleeps[1] == 5.0


def test_a_conflict_without_retry_after_is_the_answer(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(respond(409, {"error": "This request has already been decided."}))

    with pytest.raises(ConflictError, match="already been decided"):
        client.approvals.approve("a1")

    assert len(api.requests) == 1
    assert clock.sleeps == []


def test_reads_retry_after_given_as_a_date(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=30)
    api.queue(
        respond(503, headers={"Retry-After": format_datetime(when, usegmt=True)}),
        respond(200, {"workflows": []}),
    )

    client.workflows.list()

    assert 27 <= clock.sleeps[0] <= 30


def test_an_unreadable_retry_after_falls_back_to_backoff(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    api.queue(
        respond(503, headers={"Retry-After": "soon"}), respond(200, {"workflows": []})
    )

    client.workflows.list()

    assert 0.375 <= clock.sleeps[0] <= 0.5


def test_a_request_timeout_is_retried(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(408), respond(200, {"workflows": []}))

    assert client.workflows.list() == []


def test_retries_are_logged(
    client: Lodol, api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    api.queue(respond(503), respond(200, {"workflows": []}))

    with caplog.at_level(logging.INFO, logger="lodol"):
        client.workflows.list()

    assert "GET /workflows failed (HTTP 503); trying again in" in caplog.text
    assert "(retry 1 of 2)" in caplog.text
    assert "sk_live_test" not in caplog.text


@pytest.mark.parametrize("attempt", range(8))
def test_backoff_doubles_up_to_a_cap_with_jitter(attempt: int) -> None:
    ceiling = min(0.5 * 2**attempt, 8.0)

    for _ in range(50):
        assert 0.75 * ceiling <= _transport.backoff(attempt) <= ceiling


def test_error_responses_carry_the_servers_message_and_code(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(
        respond(
            403,
            {
                "error": "This workspace only accepts requests from its allowed networks.",
                "error_code": "ip_not_allowed",
            },
        )
    )

    with pytest.raises(PermissionDeniedError) as exc:
        client.workflows.list()

    assert str(exc.value).startswith("This workspace only accepts")
    assert exc.value.code == "ip_not_allowed"
    assert exc.value.body["error_code"] == "ip_not_allowed"
    assert exc.value.response is not None


def test_a_lodol_local_without_a_licence(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            402,
            {
                "error": "This copy of Lodol needs a licence. Paste the licence file "
                "you were sent into Settings to carry on.",
                "licence_required": True,
            },
        )
    )

    with pytest.raises(PaymentRequiredError, match="needs a licence"):
        client.workflows.list()


def test_an_html_error_page_isnt_the_message(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.Response(502, text="<html><body>Bad gateway</body></html>"))
    http_client = client._http_client
    client.max_retries = 0

    with pytest.raises(InternalServerError) as exc:
        client.workflows.list()

    assert str(exc.value) == "Lodol answered with HTTP 502 Bad Gateway"
    assert "<html>" in exc.value.body
    assert not http_client.is_closed


def test_an_unexpected_status_is_still_an_api_error(
    client: Lodol, api: FakeAPI
) -> None:
    api.queue(respond(418, {"error": "I'm a teapot"}))

    with pytest.raises(APIStatusError) as exc:
        client.workflows.list()

    assert type(exc.value) is APIStatusError
    assert exc.value.status_code == 418


def test_a_redirect_says_lodol_moved(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        httpx2.Response(
            308, headers={"Location": "https://app.lodol.com/api/v1/workflows"}
        )
    )

    with pytest.raises(APIStatusError, match="set base_url to the new address"):
        client.workflows.list()


def test_a_success_that_isnt_json(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.Response(200, text="<html>Login</html>"))

    with pytest.raises(APIResponseValidationError, match="isn't JSON"):
        client.workflows.list()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("12", 12.0),
        ("1.5", 1.5),
        ("-3", 0.0),
        ("nan", None),
        ("", None),
        # A date in the past means now.
        ("Wed, 21 Oct 2015 07:28:00 -0000", 0.0),
    ],
)
def test_retry_after_seconds(value: str, expected: float | None) -> None:
    assert (
        _transport.retry_after_seconds(httpx2.Headers({"Retry-After": value}))
        == expected
    )


def test_debug_logging_shows_each_request_and_replays(
    client: Lodol, api: FakeAPI, caplog: pytest.LogCaptureFixture
) -> None:
    api.queue(
        httpx2.Response(202, json=run_json(), headers={"X-Idempotent-Replayed": "true"})
    )

    with caplog.at_level(logging.DEBUG, logger="lodol"):
        client.workflows.run("665f", idempotency_key="k1")

    assert "POST /workflows/665f/run-async -> 202 in" in caplog.text
    assert "(replayed: this request was already done)" in caplog.text


def test_waiting_for_an_earlier_attempt_doesnt_use_up_retries(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """However many times Lodol says it's still working on the first attempt
    (up to its own five-minute lock), the retry keeps asking, rather than
    raising after max_retries while that attempt goes through."""
    in_flight = respond(
        409,
        {"error": "A request with this Idempotency-Key is currently being processed."},
        {"Retry-After": "5"},
    )
    api.queue(
        httpx2.ReadTimeout("slow"),
        *[in_flight] * 10,
        httpx2.Response(
            202, json=run_json(), headers={"X-Idempotent-Replayed": "true"}
        ),
    )

    run = client.workflows.run("665f")

    assert run.id == "683b"
    assert clock.sleeps.count(5.0) == 10
    keys = {request.headers["Idempotency-Key"] for request in api.requests}
    assert len(keys) == 1


def test_an_earlier_attempt_still_in_flight_after_five_minutes(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    in_flight = respond(
        409,
        {"error": "A request with this Idempotency-Key is currently being processed."},
        {"Retry-After": "5"},
    )
    api.queue(httpx2.ReadTimeout("slow"), *[in_flight] * 70)

    with pytest.raises(ConflictError) as exc:
        client.workflows.run("665f", idempotency_key="acme-2026-05")

    message = str(exc.value)
    assert "still working on an earlier attempt" in message
    assert "may yet go through: check before trying again" in message
    assert "(Idempotency-Key: acme-2026-05)" in message
    assert sum(clock.sleeps) <= 300 + 1


def test_a_rate_limited_request_of_any_kind_is_retried(
    client: Lodol, api: FakeAPI, clock: FakeClock
) -> None:
    """Lodol checks its limits before doing anything, so even a delete, or a
    write without a key, can be sent again after a 429."""
    limited = respond(429, {"error": "Rate limit exceeded."}, {"Retry-After": "3"})
    api.queue(limited, respond(200, {"id": "665f", "name": "x", "deleted": True}))
    api.queue(limited, respond(200, {"ok": True}))

    assert client.workflows.delete("665f").id == "665f"
    assert client.request("POST", "/custom") == {"ok": True}
    assert clock.sleeps == [3.0, 3.0]


def test_a_key_makes_only_a_post_resendable(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(500, {"error": "temporary"}))

    with pytest.raises(InternalServerError):
        client.request("DELETE", "/workflows/665f", idempotency_key="k1")

    assert len(api.requests) == 1


def test_a_certificate_that_cant_be_verified_isnt_retried(
    client: Lodol, api: FakeAPI
) -> None:
    failure = httpx2.ConnectError("certificate verify failed")
    failure.__cause__ = ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in chain"
    )
    api.queue(failure)

    with pytest.raises(APIConnectionError, match="set SSL_CERT_FILE"):
        client.workflows.list()

    assert len(api.requests) == 1


def test_an_answer_that_cant_be_read(client: Lodol, api: FakeAPI) -> None:
    api.queue(httpx2.DecodingError("bad gzip"))

    with pytest.raises(APIConnectionError, match="couldn't be read: bad gzip"):
        client.workflows.list()


def test_the_same_inputs_are_sent_the_same_way(client: Lodol, api: FakeAPI) -> None:
    """Lodol tells a retry from a new request by its exact bytes."""
    api.queue(respond(202, run_json()), respond(202, run_json()))

    client.workflows.run("665f", inputs={"client": "Acme", "month": "2026-05"})
    client.workflows.run("665f", inputs={"month": "2026-05", "client": "Acme"})

    assert api.requests[0].content == api.requests[1].content


@pytest.mark.parametrize(
    ("key", "error", "complaint"),
    [
        (123, TypeError, "must be text, not int"),
        ("", ValueError, "is empty"),
        ("   ", ValueError, "is empty"),
        ("k" * 257, ValueError, "257 characters; Lodol takes at most 256"),
    ],
)
def test_a_bad_idempotency_key_is_refused_before_sending(
    client: Lodol, api: FakeAPI, key: object, error: type[Exception], complaint: str
) -> None:
    with pytest.raises(error, match=complaint):
        client.workflows.run("665f", idempotency_key=key)  # type: ignore[arg-type]

    assert api.requests == []


def test_any_text_works_as_an_idempotency_key(client: Lodol, api: FakeAPI) -> None:
    api.queue(respond(202, run_json()), respond(202, run_json()))

    client.workflows.run("665f", idempotency_key="monthly-report:Café Noël:2026-05")
    client.workflows.run("665f", idempotency_key="monthly-report:Café Noël:2026-05")

    first, second = (request.headers["Idempotency-Key"] for request in api.requests)
    assert first == second == "monthly-report:Caf%C3%A9 No%C3%ABl:2026-05"


def test_notes_the_workspaces_request_allowance(client: Lodol, api: FakeAPI) -> None:
    api.queue(
        respond(
            200,
            {"workflows": []},
            {
                "X-RateLimit-Limit": "5",
                "X-RateLimit-Remaining": "3",
                "X-RateLimit-Reset": "41",
            },
        )
    )

    client.workflows.list()

    assert client._rate_limit == _transport.RateLimit(limit=5, remaining=3, reset=41.0)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"X-RateLimit-Limit": "0", "X-RateLimit-Remaining": "0"},
        {"X-RateLimit-Limit": "many", "X-RateLimit-Remaining": "1"},
        {"X-RateLimit-Limit": "5"},
    ],
)
def test_an_allowance_that_says_nothing_is_ignored(headers: dict[str, str]) -> None:
    assert _transport.parse_rate_limit(httpx2.Headers(headers)) is None


def test_a_certificate_failure_is_recognized_by_its_words_too() -> None:
    error = httpx2.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get issuer")

    assert _transport.is_certificate_error(error)
    assert not _transport.is_certificate_error(httpx2.ConnectError("refused"))
