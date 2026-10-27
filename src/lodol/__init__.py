"""Lodol's Developer API, from Python.

from lodol import Lodol

lodol = Lodol()  # reads LODOL_API_KEY
run = lodol.workflows.run("665f...", inputs={"client": "Acme"}, wait=True)
print(run.status)
"""

from __future__ import annotations

import warnings
from typing import Any

from lodol._logs import setup_logging
from lodol.client import AsyncLodol, Lodol
from lodol.exceptions import (
    APIConnectionError,
    APIError,
    APIResponseValidationError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    ConfigurationError,
    ConflictError,
    InternalServerError,
    LodolError,
    NotFoundError,
    PaymentRequiredError,
    PermissionDeniedError,
    RateLimitError,
    RunFailedError,
    RunLimitError,
    UnprocessableEntityError,
    WaitTimeoutError,
)
from lodol.models import (
    Approval,
    ApprovalStatus,
    Decision,
    DeletedRun,
    DeletedWorkflow,
    RetryOptions,
    Run,
    RunStatus,
    Step,
    Workflow,
    WorkflowInput,
    WorkflowOutput,
)
from lodol.pagination import AsyncPaginator, Paginator
from lodol.version import __version__

setup_logging()

__all__ = [
    "APIConnectionError",
    "APIError",
    "APIResponseValidationError",
    "APIStatusError",
    "APITimeoutError",
    "Approval",
    "ApprovalStatus",
    "AsyncLodol",
    "AsyncPaginator",
    "AuthenticationError",
    "BadRequestError",
    "ConfigurationError",
    "ConflictError",
    "Decision",
    "DeletedRun",
    "DeletedWorkflow",
    "InternalServerError",
    "Lodol",
    "LodolError",
    "NotFoundError",
    "Paginator",
    "PaymentRequiredError",
    "PermissionDeniedError",
    "RateLimitError",
    "RetryOptions",
    "Run",
    "RunFailedError",
    "RunLimitError",
    "RunStatus",
    "Step",
    "UnprocessableEntityError",
    "WaitTimeoutError",
    "Workflow",
    "WorkflowInput",
    "WorkflowOutput",
    "__version__",
]

# Renamed in 0.2.0. Importing an old name still works, with a warning, so a
# script written for 0.1 starts and says what to change.
_RENAMED = {"Execution": "Run", "LodolTimeoutError": "WaitTimeoutError"}


def __getattr__(name: str) -> Any:
    if name in _RENAMED:
        new_name = _RENAMED[name]
        warnings.warn(
            f"lodol.{name} is now lodol.{new_name}; the old name will be removed "
            "in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[new_name]
    raise AttributeError(f"module 'lodol' has no attribute {name!r}")
