"""The objects the API's answers become, and the names they used to have."""

from __future__ import annotations

import datetime as dt
import logging
import pickle
import subprocess
import sys
import warnings

import pytest

import lodol
from lodol import (
    Approval,
    ApprovalStatus,
    Run,
    RunStatus,
    WaitTimeoutError,
    Workflow,
    WorkflowInput,
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
