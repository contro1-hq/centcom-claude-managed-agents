"""Claude Managed Agents -> Contro1 approval bridge.

This example mirrors Anthropic's Managed Agents event model:
- tool confirmation pauses on session.status_idle with stop_reason.type="requires_action"
- blocking event IDs are in stop_reason.event_ids
- agent.tool_use / agent.mcp_tool_use resume with user.tool_confirmation
- agent.custom_tool_use resumes with user.custom_tool_result
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from anthropic import Anthropic
from centcom import CentcomClient
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

PORT = int(os.environ.get("LISTENER_PORT", "8084"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
DB_PATH = os.environ.get("BRIDGE_DB_PATH", "bridge_state.db")
CALLBACK_MAX_SKEW_SECONDS = int(os.environ.get("CALLBACK_MAX_SKEW_SECONDS", "300"))
RESPONSE_RETRY_ATTEMPTS = int(os.environ.get("RESPONSE_RETRY_ATTEMPTS", "4"))
RESPONSE_RETRY_BASE_SECONDS = float(os.environ.get("RESPONSE_RETRY_BASE_SECONDS", "1.0"))
SIMULATE_CLAUDE_RESPONSE = os.environ.get("SIMULATE_CLAUDE_RESPONSE", "true").lower() == "true"

centcom = CentcomClient(
    api_key=os.environ["CENTCOM_API_KEY"],
    base_url=os.environ.get("CENTCOM_BASE_URL", "https://api.contro1.com/api/centcom/v1"),
)
anthropic_client: Anthropic | None = None


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
              dedupe_key TEXT PRIMARY KEY,
              request_id TEXT UNIQUE,
              session_id TEXT NOT NULL,
              event_id TEXT NOT NULL,
              blocking_event_type TEXT NOT NULL,
              blocking_event_json TEXT NOT NULL,
              status TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              last_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dead_letters (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              dedupe_key TEXT NOT NULL,
              request_id TEXT,
              error TEXT NOT NULL,
              payload_json TEXT,
              created_at TEXT NOT NULL
            )
            """
        )


def get_approval_by_dedupe_key(dedupe_key: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM approvals WHERE dedupe_key = ?", (dedupe_key,)).fetchone()


def get_approval_by_request_id(request_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM approvals WHERE request_id = ?", (request_id,)).fetchone()


def upsert_approval(
    *,
    dedupe_key: str,
    request_id: str | None,
    session_id: str,
    event_id: str,
    blocking_event_type: str,
    blocking_event: dict[str, Any],
    status: str,
    last_error: str | None = None,
) -> None:
    now = utc_now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO approvals (
              dedupe_key, request_id, session_id, event_id, blocking_event_type,
              blocking_event_json, status, created_at, updated_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
              request_id = excluded.request_id,
              blocking_event_type = excluded.blocking_event_type,
              blocking_event_json = excluded.blocking_event_json,
              status = excluded.status,
              updated_at = excluded.updated_at,
              last_error = excluded.last_error
            """,
            (
                dedupe_key,
                request_id,
                session_id,
                event_id,
                blocking_event_type,
                json.dumps(blocking_event, sort_keys=True),
                status,
                now,
                now,
                last_error,
            ),
        )


def write_dead_letter(*, dedupe_key: str, request_id: str | None, error: str, payload: dict[str, Any]) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO dead_letters (dedupe_key, request_id, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dedupe_key, request_id, error, json.dumps(payload), utc_now_iso()),
        )


def verify_contro1_signature(raw_body: bytes) -> tuple[bool, str]:
    secret = os.environ.get("CENTCOM_WEBHOOK_SECRET", "")
    signature = request.headers.get("X-CentCom-Signature", "").strip()
    timestamp = request.headers.get("X-CentCom-Timestamp", "").strip()
    if not secret:
        return False, "CENTCOM_WEBHOOK_SECRET is not configured"
    if not signature or not timestamp:
        return False, "Missing signature headers"

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False, "Invalid timestamp header"

    if abs(int(time.time()) - timestamp_int) > CALLBACK_MAX_SKEW_SECONDS:
        return False, "Stale callback timestamp"

    signed_input = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed_input, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "Invalid callback signature"

    return True, ""


def create_contro1_request(session_id: str, event_id: str, blocking_event: dict[str, Any]) -> dict[str, Any]:
    blocking_event_type = str(blocking_event.get("type", "agent.tool_use"))
    dedupe_key = f"claude:{session_id}:{event_id}"
    existing = get_approval_by_dedupe_key(dedupe_key)
    if existing and existing["status"] not in {"failed_create", "dead_letter"}:
        return {"status": "duplicate_ignored", "request_id": existing["request_id"], "dedupe_key": dedupe_key}

    upsert_approval(
        dedupe_key=dedupe_key,
        request_id=None,
        session_id=session_id,
        event_id=event_id,
        blocking_event_type=blocking_event_type,
        blocking_event=blocking_event,
        status="creating_request",
    )

    created = centcom.create_protocol_request(
        {
            "title": f"Claude managed-agent event: {blocking_event_type}",
            "description": "Claude Managed Agents paused for a human decision.",
            "request_type": "review",
            "source": {
                "integration": "claude-managed-agents",
                "framework": "anthropic-managed-agents",
                "session_id": session_id,
                "run_id": event_id,
            },
            "routing": {"priority": "normal", "required_role": "developer"},
            "context": {"blocking_event_id": event_id, "blocking_event": blocking_event},
            "continuation": {"mode": "event", "callback_url": f"{PUBLIC_BASE_URL}/centcom-callback"},
            "external_request_id": dedupe_key,
            "correlation_id": session_id,
        }
    )
    request_id = str(created.get("id") or created.get("request_id") or "").strip()
    if not request_id:
        raise RuntimeError("Contro1 did not return request_id")

    upsert_approval(
        dedupe_key=dedupe_key,
        request_id=request_id,
        session_id=session_id,
        event_id=event_id,
        blocking_event_type=blocking_event_type,
        blocking_event=blocking_event,
        status="queued_for_operator",
    )
    return {"status": "queued", "request_id": request_id, "dedupe_key": dedupe_key}


def approved_from_callback(payload: dict[str, Any]) -> bool:
    status = str(payload.get("status") or (payload.get("protocol_response") or {}).get("status") or "")
    return status == "approved"


def build_claude_response_events(approval: sqlite3.Row, payload: dict[str, Any]) -> list[dict[str, Any]]:
    approved = approved_from_callback(payload)
    event_id = str(approval["event_id"])
    blocking_event_type = str(approval["blocking_event_type"])
    blocking_event = json.loads(str(approval["blocking_event_json"]))

    if blocking_event_type in {"agent.tool_use", "agent.mcp_tool_use"}:
        event: dict[str, Any] = {
            "type": "user.tool_confirmation",
            "tool_use_id": event_id,
            "result": "allow" if approved else "deny",
        }
        if not approved:
            event["deny_message"] = payload.get("message") or "Denied in Contro1"
        return [event]

    if blocking_event_type == "agent.custom_tool_use":
        if not approved:
            return []
        # Replace this with your real custom-tool execution.
        result = f"Approved custom tool {blocking_event.get('name', event_id)}"
        return [
            {
                "type": "user.custom_tool_result",
                "custom_tool_use_id": event_id,
                "content": [{"type": "text", "text": result}],
            }
        ]

    raise RuntimeError(f"Unsupported blocking event type: {blocking_event_type}")


def send_claude_response_events(session_id: str, events: list[dict[str, Any]]) -> None:
    if SIMULATE_CLAUDE_RESPONSE:
        app.logger.info("SIMULATED Claude events: session_id=%s events=%s", session_id, events)
        return
    global anthropic_client
    if anthropic_client is None:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required when SIMULATE_CLAUDE_RESPONSE=false")
        anthropic_client = Anthropic(api_key=api_key)
    anthropic_client.beta.sessions.events.send(session_id, events=events)


def send_with_retries(approval: sqlite3.Row, payload: dict[str, Any]) -> tuple[bool, str]:
    events = build_claude_response_events(approval, payload)
    if not events:
        return True, ""

    last_error = ""
    for attempt in range(1, RESPONSE_RETRY_ATTEMPTS + 1):
        try:
            send_claude_response_events(str(approval["session_id"]), events)
            return True, ""
        except Exception as error:  # noqa: BLE001 - dead-letter any transport/runtime failure.
            last_error = str(error)
            if attempt < RESPONSE_RETRY_ATTEMPTS:
                time.sleep(RESPONSE_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    write_dead_letter(
        dedupe_key=str(approval["dedupe_key"]),
        request_id=str(approval["request_id"]),
        error=last_error or "Unknown response-event error",
        payload=payload,
    )
    return False, last_error or "Unknown response-event error"


@app.post("/managed-agent/event")
def managed_agent_event():
    payload = request.get_json(force=True, silent=False) or {}
    session_id = str(payload.get("session_id", "")).strip()
    event = payload.get("event") or payload
    events_by_id = payload.get("events_by_id") or {}

    if event.get("type") != "session.status_idle":
        return jsonify({"status": "ignored"})
    stop_reason = event.get("stop_reason") or {}
    if stop_reason.get("type") != "requires_action":
        return jsonify({"status": "ignored"})
    if not session_id:
        return jsonify({"error": "session_id is required"}), 400

    results = []
    for event_id in stop_reason.get("event_ids", []):
        blocking_event = events_by_id.get(event_id)
        if not blocking_event:
            results.append({"event_id": event_id, "status": "missing_blocking_event"})
            continue
        try:
            results.append(create_contro1_request(session_id, event_id, blocking_event))
        except Exception as error:  # noqa: BLE001 - keep bridge resilient.
            app.logger.exception("Failed to create Contro1 request for event_id=%s", event_id)
            results.append({"event_id": event_id, "error": str(error)})

    return jsonify({"status": "processed", "results": results})


@app.post("/centcom-callback")
def centcom_callback():
    raw = request.get_data(cache=False, as_text=False)
    valid, reason = verify_contro1_signature(raw)
    if not valid:
        return jsonify({"error": "invalid_signature", "message": reason}), 401

    payload = json.loads(raw.decode("utf-8") or "{}")
    request_id = str(payload.get("request_id") or (payload.get("protocol_response") or {}).get("request_id") or "")
    if not request_id:
        return jsonify({"error": "missing_request_id"}), 400

    approval = get_approval_by_request_id(request_id)
    if not approval:
        return jsonify({"error": "unknown_request_id"}), 404

    success, last_error = send_with_retries(approval, payload)
    centcom.log_action(
        action=(
            "claude_managed_agent.response_event_delivered"
            if success
            else "claude_managed_agent.response_event_dead_lettered"
        ),
        summary=(
            f"Delivered Claude response event for {approval['event_id']}"
            if success
            else f"Could not deliver Claude response event for {approval['event_id']}: {last_error}"
        ),
        source={
            "integration": "claude-managed-agents",
            "workflow_id": str(approval["blocking_event_type"]),
            "run_id": str(approval["event_id"]),
        },
        outcome="success" if success else "failure",
        severity="info" if success else "warning",
        correlation_id=str(approval["session_id"]),
        in_reply_to={"type": "request", "id": request_id},
    )

    upsert_approval(
        dedupe_key=str(approval["dedupe_key"]),
        request_id=request_id,
        session_id=str(approval["session_id"]),
        event_id=str(approval["event_id"]),
        blocking_event_type=str(approval["blocking_event_type"]),
        blocking_event=json.loads(str(approval["blocking_event_json"])),
        status="completed" if success else "dead_letter",
        last_error=None if success else last_error,
    )
    return jsonify({"status": "ok" if success else "dead_letter", "request_id": request_id})


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=True)
