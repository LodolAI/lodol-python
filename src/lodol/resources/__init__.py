"""What the client can work with: ``workflows``, ``runs`` and ``approvals``."""

from __future__ import annotations

from lodol.resources.approvals import Approvals, AsyncApprovals
from lodol.resources.runs import AsyncRuns, Runs
from lodol.resources.workflows import AsyncWorkflows, Workflows

__all__ = [
    "Approvals",
    "AsyncApprovals",
    "AsyncRuns",
    "AsyncWorkflows",
    "Runs",
    "Workflows",
]
