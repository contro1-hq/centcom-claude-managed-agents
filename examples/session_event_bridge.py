"""Local scaffold for Claude Managed Agents event -> Contro1 bridge."""

from __future__ import annotations

import os

from centcom import CentcomClient
from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

app = Flask(__name__)
PORT = int(os.environ.get("LISTENER_PORT", "8084"))

client = CentcomClient(
    api_key=os.environ["CENTCOM_API_KEY"],
    base_url=os.environ.get("CENTCOM_BASE_URL", "https://contro1.com/api/centcom/v1"),
)

# dedupe key: session_id:external_action_id
SEEN: set[str] = set()


@app.post("/managed-agent/event")
def managed_agent_event():
    event = request.get_json(force=True, silent=False) or {}

    if event.get("type") != "requires_action":
        return jsonify({"status": "ignored"})

    session_id = str(event.get("session_id", "")).strip()
    external_action_id = str(event.get("external_action_id", "")).strip()
    action_type = str(event.get("action_type", "tool_confirmation"))
    summary = str(event.get("summary", "Managed agent action requires review"))

    if not session_id or not external_action_id:
        return jsonify({"error": "session_id and external_action_id are required"}), 400

    dedupe_key = f"{session_id}:{external_action_id}"
    if dedupe_key in SEEN:
        return jsonify({"status": "duplicate_ignored", "dedupe_key": dedupe_key})

    protocol_request = {
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
            "callback_url": f"http://localhost:{PORT}/centcom-callback",
        },
        "external_request_id": dedupe_key,
        "metadata": {
            "session_id": session_id,
            "external_action_id": external_action_id,
            "action_type": action_type,
        },
    }

    created = client.create_protocol_request(protocol_request)
    SEEN.add(dedupe_key)

    return jsonify({"status": "queued", "request_id": created["id"], "dedupe_key": dedupe_key})


@app.post("/centcom-callback")
def centcom_callback():
    # Map callback payload to one of:
    # - tool confirmation
    # - custom tool result
    # - interrupt + message
    payload = request.get_json(force=True, silent=False) or {}
    app.logger.info("Managed-agent callback mapping placeholder: %s", payload)
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=True)
