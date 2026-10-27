"""A small stand-in for the Developer API, served over a real socket.

It behaves as the server's own code does: its errors (each with an
``error_code``), its cursors, its ``X-Request-Id`` on every answer, and its
Idempotency-Key handling (in flight: 409 + Retry-After; done: the first
answer replayed; reused for something else: 422).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from lodol import constants

KEY = "sk_live_e2e"


class StandIn:
    """What the stand-in server knows and has seen."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.workflows: dict[str, dict[str, Any]] = {
            "665f": {
                "id": "665f",
                "name": "Monthly report",
                "description": "",
                "publish_state": "published",
                "inputs": [
                    {
                        "name": "client",
                        "type": "text",
                        "required": True,
                        "default": None,
                        "description": "",
                    }
                ],
                "outputs": [{"name": "total", "type": "number", "description": ""}],
            },
            "7a1c": {
                "id": "7a1c",
                "name": "Always fails",
                "inputs": [],
                "outputs": [],
            },
            # Starts from its own trigger: running it sets it listening.
            "9b2e": {"id": "9b2e", "name": "New invoices", "inputs": [], "outputs": []},
            # Its accounting system is down the first time it runs; a retry
            # gets through.
            "5c3d": {
                "id": "5c3d",
                "name": "Sync invoices",
                "inputs": [],
                "outputs": [{"name": "synced", "type": "number", "description": ""}],
            },
        }
        # Newest first, as the API lists them.
        self.runs: list[dict[str, Any]] = []
        self.polls: dict[str, int] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.approvals: list[dict[str, Any]] = [
            {
                "id": "a2",
                "kind": "reply",
                "status": "pending",
                "title": "PO number?",
                "execution_id": "r0",
                "workflow_id": "665f",
            },
            {
                "id": "a1",
                "kind": "approval",
                "status": "pending",
                "title": "Send the invoice?",
                "execution_id": "r0",
                "workflow_id": "665f",
            },
        ]
        # Workflows the key's member can't see: their approval requests are
        # left out after a page is read, as the server leaves them out.
        self.hidden_workflows: set[str] = set()
        self.headers_seen: list[dict[str, str]] = []
        self.slow_seconds = 0.0
        # How long starting a run takes, during which a retry under the same
        # Idempotency-Key hears 409 + Retry-After, as from the server.
        self.start_seconds = 0.0
        self.in_flight_retry_after = "5"
        self.next_id = 1
        # Its own counter: a reply is often sent with ``lock`` already held.
        self.request_numbers = itertools.count(1)


class Handler(BaseHTTPRequestHandler):
    server: Server
    # Every value of each query parameter, for those that repeat.
    query_lists: dict[str, list[str]]

    def log_message(self, *_args: Any) -> None:  # keep test output clean
        pass

    # ── plumbing ──

    def _reply(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        if status >= 400 and isinstance(body, dict) and not body.get("error_code"):
            # The code for its kind, as the server gives any refusal that
            # names nothing more exact.
            body = {
                **body,
                "error_code": _FALLBACK_CODES.get(status, "invalid_request"),
            }
        data = b"" if body is None else json.dumps(body).encode()
        request_id = f"req_{next(self.server.state.request_numbers):032x}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", request_id)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, message: str, **extra: Any) -> None:
        self._reply(status, {"error": message, **extra})

    def _handle(self) -> None:
        state = self.server.state
        with state.lock:
            state.headers_seen.append(dict(self.headers))
        if state.slow_seconds:
            time.sleep(state.slow_seconds)
        token = self.headers.get("Authorization", "")
        if not token.startswith("Bearer "):
            return self._error(
                401,
                "Missing or malformed Authorization header",
                error_code="missing_api_key",
            )
        if token != f"Bearer {KEY}":
            return self._error(
                401, "Invalid or expired API key", error_code="invalid_api_key"
            )
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p][2:]  # drop "api", "v1"
        self.query_lists = parse_qs(url.query)
        query = {k: v[0] for k, v in self.query_lists.items()}
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with state.lock:
            self._route(state, self.command, parts, query, raw)

    do_GET = do_POST = do_DELETE = _handle

    # ── the API ──

    def _route(
        self,
        state: StandIn,
        method: str,
        parts: list[str],
        query: dict[str, str],
        raw: bytes,
    ) -> None:
        if method == "GET" and parts == ["workflows"]:
            return self._reply(200, {"workflows": list(state.workflows.values())})
        if parts[:1] == ["workflows"] and len(parts) == 2:
            workflow = state.workflows.get(parts[1])
            if workflow is None:
                return self._error(404, "Workflow not found")
            if method == "GET":
                return self._reply(200, {**workflow, "program": {"kind": "Program"}})
            del state.workflows[parts[1]]
            return self._reply(
                200, {"id": workflow["id"], "name": workflow["name"], "deleted": True}
            )
        if (
            method == "POST"
            and parts[:1] == ["workflows"]
            and parts[2:] == ["run-async"]
        ):
            return self._idempotent(
                state, raw, lambda: self._start_run(state, parts[1], raw)
            )
        if method == "GET" and parts == ["executions"]:
            runs = state.runs
            if "workflow_id" in query:
                runs = [r for r in runs if r["workflow_id"] == query["workflow_id"]]
            try:
                runs = self._filtered(runs, query, self.query_lists)
            except ValueError as exc:
                return self._error(400, str(exc))
            if "after" in query:
                ids = [r["execution_id"] for r in runs]
                runs = runs[ids.index(query["after"]) + 1 :]
            limit = min(int(query.get("limit", 20)), 100)
            return self._reply(
                200, {"executions": [self._listed(r) for r in runs[:limit]]}
            )
        if parts[:1] == ["executions"] and len(parts) >= 2:
            run = next((r for r in state.runs if r["execution_id"] == parts[1]), None)
            if run is None:
                return self._error(404, "Execution not found")
            if method == "DELETE" and len(parts) == 2:
                if run["status"] not in ("success", "failed", "stopped"):
                    return self._error(
                        409,
                        "Stop this run before deleting it.",
                        error_code="run_not_finished",
                    )
                state.runs.remove(run)
                return self._reply(
                    200,
                    {
                        "execution_id": run["execution_id"],
                        "deleted": True,
                        "started_runs_deleted": 0,
                    },
                )
            if method == "POST" and parts[2:] == ["retry"]:
                return self._idempotent(
                    state, raw, lambda: self._retry(state, run, raw)
                )
            if method == "POST" and parts[2:] == ["stop"]:
                if run["status"] in ("success", "failed", "stopped"):
                    return self._error(
                        400,
                        f"Execution already {run['status']}",
                        error_code="run_already_finished",
                    )
                # A run in progress is asked to stop, and stops just after.
                run["status"] = "stopped"
                return self._reply(
                    200, {"execution_id": run["execution_id"], "status": "stopping"}
                )
            self._advance(state, run)
            body = self._detail(run)
            if query.get("include_step_results") == "true":
                body["steps"] = run["steps"]
            return self._reply(200, body)
        if method == "GET" and parts == ["approvals"]:
            wanted = query.get("status", "pending")
            items = [
                a
                for a in state.approvals
                if wanted == "all"
                or (a["status"] == "pending") == (wanted == "pending")
            ]
            limit = int(query.get("limit", 20))
            if "before" in query:
                ids = [a["id"] for a in items]
                items = items[ids.index(query["before"]) + 1 :]
            read = items[:limit]
            return self._reply(
                200,
                {
                    "approvals": [a for a in read if self._reachable(state, a)],
                    # The last one read, reported or not, so paging goes on
                    # past a page that was all left out.
                    "next_before": read[-1]["id"] if len(read) == limit else None,
                },
            )
        if method == "GET" and parts[:1] == ["approvals"] and len(parts) == 2:
            approval = self._approval(state, parts[1])
            if approval is None:
                return self._error(404, "Approval request not found")
            return self._reply(200, approval)
        if (
            method == "POST"
            and parts[:1] == ["approvals"]
            and parts[2:] == ["decision"]
        ):
            return self._idempotent(
                state, raw, lambda: self._decide(state, parts[1], raw)
            )
        return self._error(404, "Not found")

    def _idempotent(
        self, state: StandIn, raw: bytes, handler: Callable[[], tuple[int, Any]]
    ) -> None:
        key = self.headers.get("Idempotency-Key")
        if key is None:
            status, body = handler()
            return self._reply(status, body)
        fingerprint = hashlib.sha256(
            self.command.encode() + self.path.encode() + raw
        ).hexdigest()
        entry = state.idempotency.get(key)
        if entry is not None:
            if entry["fingerprint"] != fingerprint:
                return self._error(
                    422,
                    "This Idempotency-Key has already been used for a different "
                    "request. Reuse a key only for identical retries.",
                    error_code="idempotency_key_reused",
                )
            if entry["state"] == "processing":
                return self._reply(
                    409,
                    {
                        "error": "A request with this Idempotency-Key is currently "
                        "being processed. Please retry later.",
                        "error_code": "request_in_progress",
                    },
                    {"Retry-After": state.in_flight_retry_after},
                )
            return self._reply(
                entry["status"], entry["body"], {"X-Idempotent-Replayed": "true"}
            )
        state.idempotency[key] = {"state": "processing", "fingerprint": fingerprint}
        if state.start_seconds:
            # Slow work, during which other requests are answered.
            state.lock.release()
            try:
                time.sleep(state.start_seconds)
            finally:
                state.lock.acquire()
        status, body = handler()
        if 200 <= status < 300:
            state.idempotency[key] = {
                "state": "done",
                "fingerprint": fingerprint,
                "status": status,
                "body": body,
            }
        else:
            del state.idempotency[key]
        self._reply(status, body)

    def _start_run(
        self, state: StandIn, workflow_id: str, raw: bytes
    ) -> tuple[int, Any]:
        workflow = state.workflows.get(workflow_id)
        if workflow is None:
            return 404, {"error": "Workflow not found"}
        body = json.loads(raw) if raw.strip() else {}
        inputs = body.get("inputs") or {}
        if workflow["inputs"] and "client" not in inputs:
            return 400, {
                "error": f"'{workflow['name']}' needs a value for its 'client' input.",
                "error_code": "invalid_inputs",
            }
        run = self._new_run(state, workflow_id, workflow["name"], inputs)
        started: dict[str, Any] = {
            "execution_id": run["execution_id"],
            "workflow_id": workflow_id,
            "version_id": "v" + workflow_id,
            "status": "queued",
            "created_at": "2026-10-13T09:00:00+00:00",
            "is_trigger_listener": run["listening"],
        }
        if run["listening"]:
            started.update(status="running", is_trigger_workflow=True)
        return 202, started

    @staticmethod
    def _new_run(
        state: StandIn,
        workflow_id: str,
        workflow_name: str,
        inputs: dict[str, Any],
        *,
        retry_of: str | None = None,
    ) -> dict[str, Any]:
        run_id = f"r{state.next_id}"
        state.next_id += 1
        failure = _FAILURES.get(workflow_id)
        if workflow_id == "5c3d" and retry_of is not None:
            failure = None  # The accounting system is back.
        listening = workflow_id == "9b2e"
        run: dict[str, Any] = {
            "execution_id": run_id,
            "workflow_id": workflow_id,
            "version_id": "v" + workflow_id,
            "workflow_name": workflow_name,
            "status": "running" if listening else "queued",
            "created_at": "2026-10-13T09:00:00+00:00",
            "started_at": None,
            "completed_at": None,
            "error": None,
            "outputs": {},
            "is_trigger_listener": listening,
            "retry_of": retry_of,
            "failure": failure,
            # Whether, once it fails, it keeps where it failed, so it can be
            # retried from there. A run from before runs kept it can't.
            "kept_where_it_failed": True,
            "listening": listening,
            "inputs": inputs,
            "steps": [
                {
                    "index": 0,
                    "status": "failed" if failure else "completed",
                    "result": (
                        None if failure else f"Report for {inputs.get('client')}"
                    ),
                    **({"error": failure} if failure else {}),
                }
            ],
        }
        state.runs.insert(0, run)
        state.polls[run_id] = 0
        return run

    def _retry(
        self, state: StandIn, run: dict[str, Any], raw: bytes
    ) -> tuple[int, Any]:
        body = json.loads(raw) if raw.strip() else {}
        mode = body.get("from", "failed_step")
        if mode not in ("failed_step", "start"):
            return 400, {"error": '"from" must be "failed_step" or "start".'}
        offered = self._retry_options(run)
        if not offered["from_start"]:
            return 409, {
                "error": "Only a run that failed can be retried.",
                "error_code": "run_not_retryable",
            }
        if mode == "failed_step" and not offered["from_failed_step"]:
            return 409, {
                "error": "This run failed before Lodol kept where runs fail, so "
                "it can only be retried from the start.",
                "error_code": "run_not_retryable",
            }
        retry = self._new_run(
            state,
            run["workflow_id"],
            run["workflow_name"],
            run["inputs"],
            retry_of=run["execution_id"],
        )
        return 202, {
            "execution_id": retry["execution_id"],
            "retry_of": run["execution_id"],
            "workflow_id": retry["workflow_id"],
            "version_id": retry["version_id"],
            "workflow_name": retry["workflow_name"],
            "from": mode,
            "version": "latest",
            "failed_step": (
                {"error_type": "step_failed", "retryable": True}
                if run["kept_where_it_failed"]
                else None
            ),
            "status": "queued",
        }

    @staticmethod
    def _retry_options(run: dict[str, Any]) -> dict[str, bool]:
        retryable = run["status"] == "failed" and not run["listening"]
        return {
            "from_start": retryable,
            "from_failed_step": retryable and run["kept_where_it_failed"],
        }

    @staticmethod
    def _advance(state: StandIn, run: dict[str, Any]) -> None:
        if run["status"] in ("success", "failed", "stopped") or run["listening"]:
            return
        state.polls[run["execution_id"]] = state.polls.get(run["execution_id"], 0) + 1
        if state.polls[run["execution_id"]] == 1:
            run["status"], run["started_at"] = "running", "2026-10-13T09:00:01+00:00"
        else:
            run["status"] = "failed" if run["failure"] else "success"
            run["completed_at"] = "2026-10-13T09:00:04+00:00"
            if run["failure"]:
                run["error"] = run["failure"]
            elif run["workflow_id"] in _OUTPUTS:
                run["outputs"] = dict(_OUTPUTS[run["workflow_id"]])

    @staticmethod
    def _filtered(
        runs: list[dict[str, Any]], query: dict[str, str], lists: dict[str, list[str]]
    ) -> list[dict[str, Any]]:
        # As the server reads them. It leaves out a status it doesn't know,
        # and with none left, doesn't filter by status at all.
        statuses = [
            status
            for status in (s.strip().lower() for s in lists.get("status", []))
            if status in _KNOWN_STATUSES
        ]
        if statuses:
            runs = [r for r in runs if r["status"] in statuses]
        start = _run_time(query.get("from"), "from")
        end = _run_time(query.get("to"), "to")
        if start is not None and end is not None and end <= start:
            raise ValueError("The end of the date range must be after its start.")
        if start is not None or end is not None:
            # By when a run started: one still queued hasn't.
            runs = [
                r
                for r in runs
                if r["started_at"] is not None
                and (start is None or _started(r) >= start)
                and (end is None or _started(r) < end)
            ]
        search = (query.get("search") or "").strip().lower()
        if search:
            runs = [
                r
                for r in runs
                if search
                in json.dumps(
                    [r["workflow_name"], r["inputs"], r["outputs"], r["error"]]
                ).lower()
            ]
        return runs

    @staticmethod
    def _listed(run: dict[str, Any]) -> dict[str, Any]:
        # As GET /executions lists a run: no outputs, no steps.
        fields = (
            "execution_id",
            "workflow_id",
            "version_id",
            "workflow_name",
            "status",
            "is_trigger_listener",
            "created_at",
            "started_at",
            "completed_at",
            "error",
        )
        return {name: run[name] for name in fields}

    def _detail(self, run: dict[str, Any]) -> dict[str, Any]:
        # As GET /executions/<id> gives a run: with what it handed back, and
        # how it can be retried.
        fields = (
            "execution_id",
            "workflow_id",
            "version_id",
            "workflow_name",
            "status",
            "is_trigger_listener",
            "created_at",
            "started_at",
            "completed_at",
            "error",
            "outputs",
            "retry_of",
        )
        return {
            **{name: run[name] for name in fields},
            "retry_options": self._retry_options(run),
        }

    @staticmethod
    def _reachable(state: StandIn, approval: dict[str, Any]) -> bool:
        return approval.get("workflow_id") not in state.hidden_workflows

    def _approval(self, state: StandIn, approval_id: str) -> dict[str, Any] | None:
        # A request the key can't see is one it can't read or answer either.
        approval = next((a for a in state.approvals if a["id"] == approval_id), None)
        if approval is None or not self._reachable(state, approval):
            return None
        return approval

    def _decide(self, state: StandIn, approval_id: str, raw: bytes) -> tuple[int, Any]:
        approval = self._approval(state, approval_id)
        if approval is None:
            return 404, {"error": "Approval request not found."}
        data = json.loads(raw)
        if approval["status"] != "pending":
            return 409, {"error": "This request has already been decided."}
        if approval["kind"] == "reply":
            approval["status"] = "replied"
            approval["decision"] = {
                "decision": "replied",
                "reply": data.get("reply"),
                "decided_by": {"type": "api", "name": "API key 'e2e'"},
                "via": "api",
            }
        else:
            approval["status"] = data["decision"]
            approval["decision"] = {
                "decision": data["decision"],
                "comment": data.get("comment", ""),
                "decided_by": {"type": "api", "name": "API key 'e2e'"},
                "via": "api",
            }
        return 200, approval


# Every status the server keeps a run in, and so knows as a filter.
_KNOWN_STATUSES = frozenset(
    {
        "queued",
        "running",
        "success",
        "failed",
        "stopped",
        "paused",
        "awaiting_review",
        "awaiting_input",
        "awaiting_approval",
        "waiting",
        "waiting_for_trigger",
        "unknown",
    }
)


def _run_time(raw: str | None, name: str) -> dt.datetime | None:
    # As the server reads one: a date alone is the start of that day, and a
    # time without an offset is UTC.
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"{name} must be a date or time in ISO 8601, like 2026-10-15 or "
            "2026-10-15T09:30:00Z."
        ) from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def _started(run: dict[str, Any]) -> dt.datetime:
    started = _run_time(run["started_at"], "started_at")
    assert started is not None
    return started


# Why each workflow's runs fail, for those that do.
_FAILURES = {"7a1c": "Sheet not found", "5c3d": "The accounting system didn't answer"}
# What each workflow's runs hand back when they succeed.
_OUTPUTS: dict[str, dict[str, Any]] = {"665f": {"total": 1250}, "5c3d": {"synced": 12}}

_FALLBACK_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    404: "not_found",
    409: "conflict",
    422: "unprocessable_request",
    429: "rate_limited",
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.state = StandIn()


def serve(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    # Real sockets and real sleeps, but short ones.
    monkeypatch.setattr(constants, "INITIAL_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(constants, "MAX_POLL_INTERVAL", 0.05)
    httpd = Server()
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def address(server: Server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"
