# Claude Managed Agents Connector Guide

This guide defines a production-safe bridge between Claude Managed Agents blocking session events and Contro1/CENTCOM approvals.

## End-to-end flow

1. Session stream listener receives agent/session events and stores them by event ID.
2. Bridge detects `session.status_idle` where `stop_reason.type = "requires_action"`.
3. Bridge creates one request for each blocking ID in `stop_reason.event_ids`.
4. Bridge computes dedupe key: `session_id:event_id`.
4. Bridge persists action record (status=`creating_request`).
5. Bridge creates Protocol v1 request in Contro1.
6. Operator resolves in CENTCOM.
7. Bridge receives signed callback from Contro1.
8. Bridge verifies signature and timestamp.
9. Bridge maps callback status/message to `user.tool_confirmation` or `user.custom_tool_result`.
10. Bridge sends the response event to the Claude Managed Agents session events API with bounded retries.
11. Exhausted failures are written to dead-letter storage.

## Installation

```bash
pip install centcom flask python-dotenv
```

## Required environment

```bash
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://api.contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_secret

LISTENER_PORT=8084
PUBLIC_BASE_URL=https://your-bridge.example.com
BRIDGE_DB_PATH=bridge_state.db

CALLBACK_MAX_SKEW_SECONDS=300
RESPONSE_RETRY_ATTEMPTS=4
RESPONSE_RETRY_BASE_SECONDS=1.0

SIMULATE_CLAUDE_RESPONSE=true
ANTHROPIC_API_KEY=
```

## Protocol request mapping

Use one request per blocking event ID. `external_request_id` must equal dedupe key.

```json
{
  "title": "Managed agent event: agent.tool_use",
  "description": "Tool confirmation requires operator approval",
  "request_type": "review",
  "source": {
    "integration": "claude-managed-agents",
    "framework": "anthropic-managed-agents",
    "session_id": "sess_123",
    "run_id": "evt_987"
  },
  "routing": {
    "required_role": "manager",
    "priority": "normal"
  },
  "context": {
    "blocking_event_type": "agent.tool_use",
    "blocking_event_id": "evt_987",
    "tool_input": { "tool_name": "delete_file" }
  },
  "continuation": {
    "mode": "event",
    "callback_url": "https://your-bridge.example.com/centcom-callback"
  },
  "external_request_id": "sess_123:evt_987"
}
```

## Persistence contract

Store at least:

- `dedupe_key`
- `request_id`
- `session_id`
- `event_id`
- `blocking_event_type`
- `continuation_mode`
- `status`
- `last_error`

And a dead-letter table/queue for exhausted response-event retries.

## Dedupe and replay rules

- Deduplicate before creating request.
- If duplicate event arrives and record is not `failed_create`/`dead_letter`, return idempotent acknowledgment.
- Keep deterministic idempotency key for Anthropic response-event calls (`session_id:event_id`).

## Callback verification (mandatory)

Verify all callbacks:

- `X-CentCom-Signature`
- `X-CentCom-Timestamp`

Validation rules:

- Reject missing headers.
- Reject stale timestamps (`abs(now - timestamp) > CALLBACK_MAX_SKEW_SECONDS`).
- Reject HMAC mismatch for `sha256(timestamp + "." + raw_body)`.

## Callback mapping policy

Use `status`, `message`, `structured_response` from callback (or nested `protocol_response`).

- `approved` + `agent.tool_use` / `agent.mcp_tool_use` -> `user.tool_confirmation` with `result: "allow"`
- `denied` + `agent.tool_use` / `agent.mcp_tool_use` -> `user.tool_confirmation` with `result: "deny"` and `deny_message`
- `timed_out` / `cancelled` -> fail closed with `result: "deny"` for tool confirmations
- `approved` + `agent.custom_tool_use` -> execute the custom tool, then send `user.custom_tool_result`
- `denied` + `agent.custom_tool_use` -> do not execute the custom tool

## Retry and dead-letter model

- Retry response-event transport with exponential backoff: `base * 2^(attempt-1)`.
- Retry only transport/runtime failures (not permanent contract failures).
- On exhaustion:
  - mark action status as `dead_letter`
  - persist callback payload + error for manual replay

## Observability minimum

Emit structured logs with:

- `dedupe_key`
- `request_id`
- `session_id`
- `event_id`
- `status`
- `attempt`
- `error` (if any, redacted)

Track metrics:

- requests created
- duplicate events ignored
- callback signature failures
- response-event retry count
- dead-letter count

## Smoke test

1. Start bridge: `python examples/session_event_bridge.py`
2. POST fake event to `/managed-agent/event`
3. Confirm request in CENTCOM
4. Resolve in dashboard
5. Confirm callback accepted and response-event mapping logged
6. Force continuation failure and confirm dead-letter write
