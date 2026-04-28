"""Production-oriented bridge for Claude Managed Agents -> Contro1 Protocol v1."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from centcom import CentcomClient
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)

PORT = int(os.environ.get("LISTENER_PORT", "8084"))
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
DB_PATH = os.environ.get("BRIDGE_DB_PATH", "bridge_state.db")

CALLBACK_MAX_SKEW_SECONDS = int(os.environ.get("CALLBACK_MAX_SKEW_SECONDS", "300"))
CONTINUATION_RETRY_ATTEMPTS = int(os.environ.get("CONTINUATION_RETRY_ATTEMPTS", "4"))
CONTINUATION_RETRY_BASE_SECONDS = float(os.environ.get("CONTINUATION_RETRY_BASE_SECONDS", "1.0"))
ANTHROPIC_TIMEOUT_SECONDS = int(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "15"))
SIMULATE_CONTINUATION = os.environ.get("SIMULATE_CONTINUATION", "true").lower() == "true"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_CONTINUATION_URL = os.environ.get("ANTHROPIC_CONTINUATION_URL", "")

client = CentcomClient(
    api_key=os.environ["CENTCOM_API_KEY"],
    base_url=os.environ.get("CENTCOM_BASE_URL", "https://api.contro1.com/api/centcom/v1"),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def contro1_thread_id(value: str) -> str:
    if value.startswith("thr_") and len(value) <= 68:
        return value
    return f"thr_claude_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS actions (
              dedupe_key TEXT PRIMARY KEY,
              request_id TEXT UNIQUE,
              session_id TEXT NOT NULL,
              external_action_id TEXT NOT NULL,
              action_type TEXT NOT NULL,
              continuation_mode TEXT NOT NULL,
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


def get_action_by_dedupe_key(dedupe_key: str) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM actions WHERE dedupe_key = ?",
            (dedupe_key,),
        ).fetchone()
        return row


def get_action_by_request_id(request_id: str) -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM actions WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return row


def upsert_action(
    *,
    dedupe_key: str,
    request_id: str | None,
    session_id: str,
    external_action_id: str,
    action_type: str,
    continuation_mode: str,
    status: str,
    last_error: str | None = None,
) -> None:
    now = utc_now_iso()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO actions (
              dedupe_key, request_id, session_id, external_action_id, action_type,
              continuation_mode, status, created_at, updated_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(dedupe_key) DO UPDATE SET
              request_id = excluded.request_id,
              action_type = excluded.action_type,
              continuation_mode = excluded.continuation_mode,
              status = excluded.status,
              updated_at = excluded.updated_at,
              last_error = excluded.last_error
            """,
            (
                dedupe_key,
                request_id,
                session_id,
                external_action_id,
                action_type,
                continuation_mode,
                status,
                now,
                now,
                last_error,
            ),
        )


def write_dead_letter(
    *,
    dedupe_key: str,
    request_id: str | None,
    error: str,
    payload: dict[str, Any],
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO dead_letters (dedupe_key, request_id, error, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (dedupe_key, request_id, error, json.dumps(payload), utc_now_iso()),
        )


def verify_centcom_signature(raw_body: bytes) -> tuple[bool, str]:
    secret = os.environ.get("CENTCOM_WEBHOOK_SECRET", "")
    if not secret:
        return False, "CENTCOM_WEBHOOK_SECRET is not configured"

    signature = request.headers.get("X-CentCom-Signature", "").strip()
    timestamp = request.headers.get("X-CentCom-Timestamp", "").strip()

    if not signature or not timestamp:
        return False, "Missing signature headers"

    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False, "Invalid timestamp header"

    now = int(time.time())
    if abs(now - timestamp_int) > CALLBACK_MAX_SKEW_SECONDS:
        return False, "Stale callback timestamp"

    signed_input = f"{timestamp}.{raw_body.decode('utf-8')}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), signed_input, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False, "Invalid callback signature"

    return True, ""


def build_protocol_request(event: dict[str, Any], dedupe_key: str) -> dict[str, Any]:
    session_id = str(event["session_id"]).strip()
    external_action_id = str(event["external_action_id"]).strip()
    action_type = str(event.get("action_type", "tool_confirmation"))
    summary = str(event.get("summary", "Managed agent action requires review"))
    thread_id = contro1_thread_id(session_id)

    return {
        "title": f"Managed agent action: {action_type}",
        "description": summary,
        "request_type": "review",
        "source": {
            "integration": "claude-managed-agents",
            "framework": "anthropic-managed-agents",
            "session_id": session_id,
            "run_id": external_action_id,
        },
        "routing": {
            "priority": "normal",
            "required_role": "manager",
        },
        "context": {
            "action_type": action_type,
            "tool_input": event.get("tool_input"),
            "summary": summary,
        },
        "continuation": {
            "mode": "instruction",
            "callback_url": f"{PUBLIC_BASE_URL}/centcom-callback",
        },
        "external_request_id": dedupe_key,
        "thread_id": thread_id,
        "metadata": {
            "session_id": session_id,
            "external_action_id": external_action_id,
            "action_type": action_type,
            "contro1_thread_id": thread_id,
            "event_hash": hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest(),
        },
    }


def map_callback_to_continuation(action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status", "")).strip() or str(
        (payload.get("protocol_response") or {}).get("status", "")
    ).strip()
    message = payload.get("message")
    if message is None:
        message = (payload.get("protocol_response") or {}).get("message")
    structured = payload.get("structured_response")
    if structured is None:
        structured = (payload.get("protocol_response") or {}).get("structured_response")

    if status in {"timed_out", "cancelled"}:
        return {
            "action": "deny",
            "reason": f"Operator resolution status: {status}",
        }

    if action_type == "tool_confirmation":
        if status == "approved":
            return {"action": "confirm_tool", "reason": message or "Approved by operator"}
        return {"action": "deny", "reason": message or "Denied by operator"}

    if action_type == "custom_tool_result":
        return {
            "action": "tool_result",
            "result": structured if isinstance(structured, dict) else {"message": message},
        }

    return {
        "action": "instruction",
        "instruction": message or "Proceed with operator guidance",
        "context": structured if isinstance(structured, dict) else {},
    }


def send_to_anthropic_continuation(
    *,
    session_id: str,
    external_action_id: str,
    continuation_payload: dict[str, Any],
) -> None:
    if SIMULATE_CONTINUATION:
        app.logger.info(
            "SIMULATED continuation: session_id=%s external_action_id=%s payload=%s",
            session_id,
            external_action_id,
            continuation_payload,
        )
        return

    if not ANTHROPIC_CONTINUATION_URL:
        raise RuntimeError("ANTHROPIC_CONTINUATION_URL is required when SIMULATE_CONTINUATION=false")
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is required when SIMULATE_CONTINUATION=false")

    body = json.dumps(
        {
            "session_id": session_id,
            "external_action_id": external_action_id,
            "continuation": continuation_payload,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url=ANTHROPIC_CONTINUATION_URL,
        method="POST",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {ANTHROPIC_API_KEY}",
            "Idempotency-Key": f"{session_id}:{external_action_id}",
        },
    )
    with urllib.request.urlopen(req, timeout=ANTHROPIC_TIMEOUT_SECONDS) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"Anthropic continuation returned HTTP {response.status}")


def continue_with_retries(
    *,
    dedupe_key: str,
    request_id: str,
    session_id: str,
    external_action_id: str,
    action_type: str,
    callback_payload: dict[str, Any],
) -> tuple[bool, str]:
    continuation_payload = map_callback_to_continuation(action_type, callback_payload)

    last_error = ""
    for attempt in range(1, CONTINUATION_RETRY_ATTEMPTS + 1):
        try:
            send_to_anthropic_continuation(
                session_id=session_id,
                external_action_id=external_action_id,
                continuation_payload=continuation_payload,
            )
            app.logger.info("Continuation success for %s on attempt %s", dedupe_key, attempt)
            return True, ""
        except (RuntimeError, urllib.error.URLError, urllib.error.HTTPError) as error:
            last_error = str(error)
            app.logger.warning(
                "Continuation failed for %s on attempt %s/%s: %s",
                dedupe_key,
                attempt,
                CONTINUATION_RETRY_ATTEMPTS,
                last_error,
            )
            if attempt < CONTINUATION_RETRY_ATTEMPTS:
                sleep_for = CONTINUATION_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                time.sleep(sleep_for)

    write_dead_letter(
        dedupe_key=dedupe_key,
        request_id=request_id,
        error=last_error or "Unknown continuation error",
        payload=callback_payload,
    )
    return False, last_error or "Unknown continuation error"


@app.post("/managed-agent/event")
def managed_agent_event():
    event = request.get_json(force=True, silent=False) or {}

    if event.get("type") != "requires_action":
        return jsonify({"status": "ignored"})

    session_id = str(event.get("session_id", "")).strip()
    external_action_id = str(event.get("external_action_id", "")).strip()
    action_type = str(event.get("action_type", "tool_confirmation"))
    if not session_id or not external_action_id:
        return jsonify({"error": "session_id and external_action_id are required"}), 400

    dedupe_key = f"{session_id}:{external_action_id}"
    existing = get_action_by_dedupe_key(dedupe_key)
    if existing and existing["status"] not in {"failed_create", "dead_letter"}:
        return jsonify(
            {
                "status": "duplicate_ignored",
                "dedupe_key": dedupe_key,
                "request_id": existing["request_id"],
            }
        )

    upsert_action(
        dedupe_key=dedupe_key,
        request_id=None,
        session_id=session_id,
        external_action_id=external_action_id,
        action_type=action_type,
        continuation_mode="instruction",
        status="creating_request",
    )

    try:
        created = client.create_protocol_request(build_protocol_request(event, dedupe_key))
        request_id = str(created.get("id") or created.get("request_id") or "").strip()
        if not request_id:
            raise RuntimeError("Contro1 did not return request_id")

        upsert_action(
            dedupe_key=dedupe_key,
            request_id=request_id,
            session_id=session_id,
            external_action_id=external_action_id,
            action_type=action_type,
            continuation_mode="instruction",
            status="queued_for_operator",
        )
        return jsonify({"status": "queued", "request_id": request_id, "dedupe_key": dedupe_key})
    except Exception as error:  # noqa: BLE001 - keep bridge resilient.
        upsert_action(
            dedupe_key=dedupe_key,
            request_id=None,
            session_id=session_id,
            external_action_id=external_action_id,
            action_type=action_type,
            continuation_mode="instruction",
            status="failed_create",
            last_error=str(error),
        )
        app.logger.exception("Failed to create Contro1 request for dedupe_key=%s", dedupe_key)
        return jsonify({"error": "request_create_failed", "dedupe_key": dedupe_key}), 502


@app.post("/centcom-callback")
def centcom_callback():
    raw = request.get_data(cache=False, as_text=False)
    valid, reason = verify_centcom_signature(raw)
    if not valid:
        app.logger.warning("Rejected callback: %s", reason)
        return jsonify({"error": "invalid_signature", "message": reason}), 401

    payload = json.loads(raw.decode("utf-8") or "{}")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        request_id = str((payload.get("protocol_response") or {}).get("request_id", "")).strip()
    if not request_id:
        return jsonify({"error": "missing_request_id"}), 400

    action = get_action_by_request_id(request_id)
    if not action:
        return jsonify({"error": "unknown_request_id"}), 404

    success, last_error = continue_with_retries(
        dedupe_key=action["dedupe_key"],
        request_id=request_id,
        session_id=action["session_id"],
        external_action_id=action["external_action_id"],
        action_type=action["action_type"],
        callback_payload=payload,
    )
    thread_id = contro1_thread_id(str(action["session_id"]))
    client.log_action(
        action="claude_managed_agent.continuation_delivered" if success else "claude_managed_agent.continuation_dead_lettered",
        summary=(
            f"Delivered operator response to managed agent action {action['external_action_id']}"
            if success
            else f"Could not deliver operator response to managed agent action {action['external_action_id']}: {last_error}"
        ),
        source={
            "integration": "claude-managed-agents",
            "workflow_id": str(action["action_type"]),
            "run_id": str(action["external_action_id"]),
        },
        outcome="success" if success else "failure",
        severity="info" if success else "warning",
        thread_id=thread_id,
        in_reply_to={"type": "request", "id": request_id},
    )

    upsert_action(
        dedupe_key=action["dedupe_key"],
        request_id=request_id,
        session_id=action["session_id"],
        external_action_id=action["external_action_id"],
        action_type=action["action_type"],
        continuation_mode=action["continuation_mode"],
        status="completed" if success else "dead_letter",
        last_error=None if success else last_error,
    )

    return jsonify(
        {
            "status": "ok" if success else "dead_letter",
            "request_id": request_id,
            "dedupe_key": action["dedupe_key"],
            "error": None if success else last_error,
        }
    )


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=PORT, debug=True)
