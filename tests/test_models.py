"""The objects the API's answers become, and the names they used to have."""

from __future__ import annotations

import datetime as dt
import logging
import pickle
import subprocess
import sys
import warnings

import httpx2
import pytest

import lodol
from lodol import (
    Approval,
    ApprovalStatus,
    RetryOptions,
    Run,
    RunStatus,
    Step,
    StepStatus,
    WaitTimeoutError,
    Workflow,
    WorkflowCheck,
    WorkflowInput,
    WorkflowOutput,
)
from lodol.models import parse_datetime

UTC = dt.timezone.utc


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-01-15T10:30:00+00:00", dt.datetime(2026, 1, 15, 10, 30, tzinfo=UTC)),
        (
            "2026-01-15T10:30:00.123000+00:00",
            dt.datetime(2026, 1, 15, 10, 30, 0, 123000, tzinfo=UTC),
        ),
        ("2026-01-15T10:30:00Z", dt.datetime(2026, 1, 15, 10, 30, tzinfo=UTC)),
        # A time that names no zone is UTC, as Lodol stores them.
        ("2026-01-15T10:30:00", dt.datetime(2026, 1, 15, 10, 30, tzinfo=UTC)),
        (
            "2026-01-15T05:30:00-05:00",
            dt.datetime(2026, 1, 15, 5, 30, tzinfo=dt.timezone(dt.timedelta(hours=-5))),
        ),
        ("not a time", None),
        ("", None),
        (None, None),
        (1736937000, None),
    ],
)
def test_parse_datetime(text: object, expected: dt.datetime | None) -> None:
    assert parse_datetime(text) == expected


def test_statuses_compare_as_text() -> None:
    run = Run(id="r", status="awaiting_approval")

    assert run.status == RunStatus.AWAITING_APPROVAL
    assert f"{RunStatus.SUCCESS}" == "success"
    assert str(ApprovalStatus.EXPIRED) == "expired"
    assert Step(index=0, status="continued").status == StepStatus.CONTINUED
    assert str(StepStatus.SKIPPED) == "skipped"


def test_a_step_says_how_it_went() -> None:
    retried, continued, skipped = (
        Step.from_api(step)
        for step in [
            {"index": 0, "status": "completed", "result": {"rows": 3}, "attempts": 2},
            {
                "index": 1,
                "status": "continued",
                "result": None,
                "error": "Slack didn't answer",
            },
            {"index": 2, "status": "skipped", "reason": "This step is turned off."},
        ]
    )

    assert retried == Step(index=0, status="completed", result={"rows": 3}, attempts=2)
    assert continued.status == StepStatus.CONTINUED
    assert continued.error == "Slack didn't answer" and continued.attempts is None
    assert skipped == Step(index=2, status="skipped", reason="This step is turned off.")
    assert skipped.result is None


@pytest.mark.parametrize("attempts", [True, "2", 2.0, None])
def test_attempts_that_arent_a_count_are_left_out(attempts: object) -> None:
    assert Step.from_api({"index": 0, "attempts": attempts}).attempts is None


@pytest.mark.parametrize(
    ("status", "finished", "succeeded"),
    [
        ("queued", False, False),
        ("running", False, False),
        ("waiting", False, False),
        ("awaiting_approval", False, False),
        ("success", True, True),
        ("failed", True, False),
        ("stopped", True, False),
        # A status this version doesn't know yet is taken as still going.
        ("rescheduled", False, False),
    ],
)
def test_run_state(status: str, finished: bool, succeeded: bool) -> None:
    run = Run(id="r", status=status)

    assert (run.finished, run.succeeded) == (finished, succeeded)


def test_models_tolerate_a_sparse_answer() -> None:
    assert Run.from_api({}) == Run(id="", status="")
    assert Workflow.from_api({"inputs": "nope"}).inputs == []
    assert WorkflowInput.from_api({"name": "client"}) == WorkflowInput(
        name="client", type="text"
    )
    approval = Approval.from_api({"id": "a1", "decision": "nope", "step_index": "3"})
    assert approval.kind == "approval" and approval.decision is None
    assert approval.step_index is None


def test_raw_keeps_what_the_sdk_doesnt_model_and_stays_out_of_equality() -> None:
    first = Run.from_api({"execution_id": "r", "status": "success", "new_field": 1})
    second = Run.from_api({"execution_id": "r", "status": "success"})

    assert first.raw["new_field"] == 1
    assert first == second


def test_models_are_plain_data() -> None:
    run = Run.from_api({"execution_id": "r", "status": "success", "steps": []})

    assert pickle.loads(pickle.dumps(run)) == run
    with pytest.raises(AttributeError):
        run.status = "failed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("old", "new"), [("Execution", Run), ("LodolTimeoutError", WaitTimeoutError)]
)
def test_renamed_names_still_import_with_a_warning(old: str, new: type) -> None:
    with pytest.warns(
        DeprecationWarning, match=f"lodol.{old} is now lodol.{new.__name__}"
    ):
        assert getattr(lodol, old) is new


def test_unknown_names_are_still_unknown() -> None:
    with pytest.raises(AttributeError, match="no attribute 'Nope'"):
        getattr(lodol, "Nope")


def test_everything_exported_exists() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for name in lodol.__all__:
            assert getattr(lodol, name) is not None


def test_logging_is_silent_unless_asked() -> None:
    handlers = logging.getLogger("lodol").handlers

    assert any(isinstance(h, logging.NullHandler) for h in handlers)


@pytest.mark.parametrize("level", ["debug", "INFO"])
def test_lodol_log_turns_on_logging(level: str) -> None:
    script = (
        "import logging, lodol; logger = logging.getLogger('lodol'); "
        "print(logging.getLevelName(logger.level)); logger.info('hello')"
    )
    done = subprocess.run(
        [sys.executable, "-c", script],
        env={"LODOL_LOG": level, "PATH": ""},
        capture_output=True,
        text=True,
        check=True,
    )

    assert done.stdout.strip() == level.upper()
    assert "lodol INFO hello" in done.stderr


def test_errors_cross_process_boundaries() -> None:
    """Pickled, as multiprocessing, concurrent.futures and task queues do."""
    response = httpx2.Response(
        429,
        json={"error": "Out of runs", "error_code": "monthly_run_limit"},
        request=httpx2.Request("POST", "https://api-prod.lodol.com/api/v1/x"),
    )
    run = Run(id="683b", status="failed", error="Sheet not found")
    errors: list[lodol.LodolError] = [
        lodol.NotFoundError("Workflow not found", status_code=404, code=None, body={}),
        lodol.RunLimitError(
            "Out of runs",
            status_code=429,
            retry_after=60.0,
            resets_at=dt.datetime(2026, 11, 1, tzinfo=UTC),
            limit=2000,
            code="monthly_run_limit",
            response=response,
        ),
        lodol.RunFailedError("Run 683b failed: Sheet not found", run=run),
        WaitTimeoutError("Stopped waiting", run=run),
        lodol.APITimeoutError("no answer"),
        lodol.ConfigurationError("No key"),
    ]

    for error in errors:
        copy = pickle.loads(pickle.dumps(error))
        assert type(copy) is type(error)
        assert str(copy) == str(error)
        assert copy.__dict__.keys() == error.__dict__.keys()

    limit = pickle.loads(pickle.dumps(errors[1]))
    assert isinstance(limit, lodol.RunLimitError)
    assert limit.resets_at == dt.datetime(2026, 11, 1, tzinfo=UTC)
    assert limit.response is not None and limit.response.status_code == 429
    failed = pickle.loads(pickle.dumps(errors[2]))
    assert isinstance(failed, lodol.RunFailedError) and failed.run == run
    assert isinstance(pickle.loads(pickle.dumps(errors[3])), TimeoutError)


def test_models_can_be_hashed_whatever_they_hold() -> None:
    workflow = Workflow.from_api(
        {
            "id": "665f",
            "name": "x",
            "inputs": [{"name": "tags", "default": ["a"]}],
            "outputs": [{"name": "total", "type": "number"}],
        }
    )
    run = Run.from_api(
        {
            "execution_id": "r",
            "status": "success",
            "steps": [{"index": 0, "result": {"a": 1}}],
            "outputs": {"rows": [{"a": 1}]},
        }
    )

    assert {workflow, workflow} == {workflow}
    assert {run, run} == {run}
    # What a field holds (here, step results and outputs) stays out of the hash.
    assert hash(run) == hash(Run.from_api({"execution_id": "r", "status": "success"}))


def test_statuses_include_every_one_a_run_reports() -> None:
    assert {status.value for status in RunStatus} >= {
        "queued",
        "running",
        "waiting",
        "awaiting_approval",
        "paused",
        "stopping",
        "success",
        "failed",
        "stopped",
    }


def test_a_run_carries_what_it_handed_back() -> None:
    run = Run.from_api(
        {
            "execution_id": "683b",
            "status": "success",
            "outputs": {"total": 1250, "summary.text": "A good month", "owner": None},
        }
    )

    assert run.outputs == {"total": 1250, "summary.text": "A good month", "owner": None}
    assert not run.is_trigger_listener


def test_a_run_without_outputs_has_none() -> None:
    assert Run.from_api({"execution_id": "683b", "status": "running"}).outputs == {}
    assert Run.from_api({"execution_id": "683b", "outputs": None}).outputs == {}


def test_a_run_says_whether_it_is_a_retry_and_how_it_can_be_retried() -> None:
    run = Run.from_api(
        {
            "execution_id": "684c",
            "status": "failed",
            "retry_of": "683b",
            "retry_options": {"from_start": True, "from_failed_step": True},
        }
    )

    assert run.retry_of == "683b"
    assert run.retry_options == RetryOptions(from_start=True, from_failed_step=True)
    assert {run, run} == {run}


def test_a_run_that_doesnt_say_how_it_can_be_retried() -> None:
    # As a list gives it: neither field.
    listed = Run.from_api({"execution_id": "683b", "status": "failed"})
    # As a run that isn't a retry, and didn't fail, is given alone.
    alone = Run.from_api(
        {
            "execution_id": "683b",
            "status": "success",
            "retry_of": None,
            "retry_options": {"from_start": False, "from_failed_step": False},
        }
    )

    assert listed.retry_of is None and listed.retry_options is None
    assert alone.retry_of is None
    assert alone.retry_options == RetryOptions()
    assert RetryOptions.from_api({}) == RetryOptions(False, False)


def test_a_listener_is_marked() -> None:
    run = Run.from_api(
        {"execution_id": "683b", "status": "running", "is_trigger_listener": True}
    )

    assert run.is_trigger_listener
    assert not run.finished


def test_a_workflow_just_saved_says_what_it_needs() -> None:
    saved = Workflow.from_api(
        {
            "id": "665f",
            "name": "Say hello",
            "version_id": "6690",
            "url": "https://app.lodol.com/create?programId=665f",
            "apps_to_connect": ["Gmail", "Slack"],
            "trigger_paused": True,
        }
    )
    looked_up = Workflow.from_api(
        {"id": "665f", "name": "x", "apps_to_connect": "Gmail", "trigger_paused": 1}
    )

    assert saved.version_id == "6690"
    assert saved.url == "https://app.lodol.com/create?programId=665f"
    assert saved.apps_to_connect == ["Gmail", "Slack"]
    assert saved.trigger_paused is True
    assert {saved, saved} == {saved}
    # What isn't said, or can't be read, is None: not "nothing to connect".
    assert looked_up.version_id is None and looked_up.url is None
    assert looked_up.apps_to_connect is None and looked_up.trigger_paused is None


def test_a_check_tolerates_a_sparse_answer() -> None:
    check = WorkflowCheck.from_api({"valid": "yes", "warnings": [], "actions": "x"})

    assert check == WorkflowCheck(valid=False)
    assert WorkflowCheck.from_api({"valid": True}).valid


def test_a_workflow_says_what_it_hands_back() -> None:
    workflow = Workflow.from_api(
        {
            "id": "665f",
            "name": "Monthly report",
            "outputs": [
                {"name": "total", "type": "number", "description": "In dollars"},
                {"name": "summary"},
                "not an output",
            ],
        }
    )

    assert workflow.outputs == [
        WorkflowOutput(name="total", type="number", description="In dollars"),
        WorkflowOutput(name="summary", type="text"),
    ]
    assert Workflow.from_api({"id": "665f", "name": "x"}).outputs == []
