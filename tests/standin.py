"""A small stand-in for the Developer API, served over a real socket.

It behaves as the server's own code does: its errors, its cursors, and its
Idempotency-Key handling (in flight: 409 + Retry-After; done: the first
answer replayed; reused for something else: 422).
"""

from __future__ import annotations

import hashlib
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
            },
            "7a1c": {"id": "7a1c", "name": "Always fails", "inputs": []},
            # Starts from its own trigger: running it sets it listening.
            "9b2e": {"id": "9b2e", "name": "New invoices", "inputs": []},
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
        self.headers_seen: list[dict[str, str]] = []
        self.slow_seconds = 0.0
        # How long starting a run takes, during which a retry under the same
        # Idempotency-Key hears 409 + Retry-After, as from the server.
        self.start_seconds = 0.0
        self.in_flight_retry_after = "5"
        self.next_id = 1


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, *_args: Any) -> None:  # keep test output clean
        pass

    # ── plumbing ──

    def _reply(
        self, status: int, body: Any = None, headers: dict[str, str] | None = None
    ) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
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
            return self._error(401, "Missing or malformed Authorization header")
        if token != f"Bearer {KEY}":
            return self._error(401, "Invalid or expired API key")
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p][2:]  # drop "api", "v1"
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
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
            if method == "POST" and parts[2:] == ["stop"]:
                if run["status"] in ("success", "failed", "stopped"):
                    return self._error(400, f"Execution already {run['status']}")
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
            page = items[:limit]
            return self._reply(
                200,
                {
                    "approvals": page,
                    "next_before": page[-1]["id"] if len(page) == limit else None,
                },
            )
        if method == "GET" and parts[:1] == ["approvals"] and len(parts) == 2:
            approval = next((a for a in state.approvals if a["id"] == parts[1]), None)
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
                        "being processed. Please retry later."
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
                "error": f"'{workflow['name']}' needs a value for its 'client' input."
            }
        run_id = f"r{state.next_id}"
        state.next_id += 1
        fails = workflow_id == "7a1c"
        listening = workflow_id == "9b2e"
        state.runs.insert(
            0,
            {
                "execution_id": run_id,
                "workflow_id": workflow_id,
                "version_id": "v" + workflow_id,
                "workflow_name": workflow["name"],
                "status": "running" if listening else "queued",
                "created_at": "2026-10-13T09:00:00+00:00",
                "started_at": None,
                "completed_at": None,
                "error": None,
                "fails": fails,
                "listening": listening,
                "inputs": inputs,
                "steps": [
                    {
                        "index": 0,
                        "status": "failed" if fails else "completed",
                        "result": (
                            None if fails else f"Report for {inputs.get('client')}"
                        ),
                        **({"error": "Sheet not found"} if fails else {}),
                    }
                ],
            },
        )
        state.polls[run_id] = 0
        started: dict[str, Any] = {
            "execution_id": run_id,
            "workflow_id": workflow_id,
            "version_id": "v" + workflow_id,
            "status": "queued",
            "created_at": "2026-10-13T09:00:00+00:00",
        }
        if listening:
            started.update(status="running", is_trigger_workflow=True)
        return 202, started

    @staticmethod
    def _advance(state: StandIn, run: dict[str, Any]) -> None:
        if run["status"] in ("success", "failed", "stopped") or run["listening"]:
            return
        state.polls[run["execution_id"]] = state.polls.get(run["execution_id"], 0) + 1
        if state.polls[run["execution_id"]] == 1:
            run["status"], run["started_at"] = "running", "2026-10-13T09:00:01+00:00"
        else:
            run["status"] = "failed" if run["fails"] else "success"
            run["completed_at"] = "2026-10-13T09:00:04+00:00"
            if run["fails"]:
                run["error"] = "Sheet not found"

    @staticmethod
    def _listed(run: dict[str, Any]) -> dict[str, Any]:
        # As GET /executions lists a run: no error, no steps.
        fields = (
            "execution_id",
            "workflow_id",
            "version_id",
            "workflow_name",
            "status",
            "created_at",
            "started_at",
            "completed_at",
        )
        return {name: run[name] for name in fields}

    @staticmethod
    def _detail(run: dict[str, Any]) -> dict[str, Any]:
        # As GET /executions/<id> gives a run: no workflow name.
        fields = (
            "execution_id",
            "workflow_id",
            "version_id",
            "status",
            "created_at",
            "started_at",
            "completed_at",
            "error",
        )
        return {name: run[name] for name in fields}

    def _decide(self, state: StandIn, approval_id: str, raw: bytes) -> tuple[int, Any]:
        approval = next((a for a in state.approvals if a["id"] == approval_id), None)
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
