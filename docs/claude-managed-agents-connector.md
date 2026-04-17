# Claude Managed Agents Connector Guide

This guide defines a production-safe bridge between Claude Managed Agents action-needed events and Contro1/CENTCOM approvals.

## End-to-end flow

1. Session stream listener receives event.
2. Bridge filters `type=requires_action` only.
3. Bridge computes dedupe key: `session_id:external_action_id`.
4. Bridge persists action record (status=`creating_request`).
5. Bridge creates Protocol v1 request in Contro1.
6. Operator resolves in CENTCOM.
7. Bridge receives signed callback from Contro1.
8. Bridge verifies signature and timestamp.
9. Bridge maps callback status/message to continuation payload.
10. Bridge sends continuation to Anthropic endpoint with bounded retries.
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
CONTINUATION_RETRY_ATTEMPTS=4
CONTINUATION_RETRY_BASE_SECONDS=1.0
ANTHROPIC_TIMEOUT_SECONDS=15

SIMULATE_CONTINUATION=true
ANTHROPIC_CONTINUATION_URL=
ANTHROPIC_API_KEY=
```

## Protocol request mapping

Use one request per action-needed event. `external_request_id` must equal dedupe key.

```json
{
  "title": "Managed agent action: tool_confirmation",
  "description": "Action requires operator confirmation",
  "request_type": "review",
  "source": {
    "integration": "claude-managed-agents",
    "framework": "anthropic-managed-agents",
    "session_id": "sess_123",
    "run_id": "action_987"
  },
  "routing": {
    "required_role": "manager",
    "priority": "normal"
  },
  "context": {
    "action_type": "tool_confirmation",
    "tool_input": { "tool_name": "delete_file" }
  },
  "continuation": {
    "mode": "instruction",
    "callback_url": "https://your-bridge.example.com/centcom-callback"
  },
  "external_request_id": "sess_123:action_987"
}
```

## Persistence contract

Store at least:

- `dedupe_key`
- `request_id`
- `session_id`
- `external_action_id`
- `action_type`
- `continuation_mode`
- `status`
- `last_error`

And a dead-letter table/queue for exhausted continuation retries.

## Dedupe and replay rules

- Deduplicate before creating request.
- If duplicate event arrives and record is not `failed_create`/`dead_letter`, return idempotent acknowledgment.
- Keep deterministic idempotency key for Anthropic continuation calls (`session_id:external_action_id`).

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

- `approved` + `tool_confirmation` -> `confirm_tool`
- `denied` + `tool_confirmation` -> `deny`
- `timed_out` / `cancelled` -> `deny` with explicit reason
- `custom_tool_result` -> `tool_result` (structured payload)
- Any revise/clarify content -> `instruction` payload (do not silently flatten to deny)

## Retry and dead-letter model

- Retry continuation transport with exponential backoff: `base * 2^(attempt-1)`.
- Retry only transport/runtime failures (not permanent contract failures).
- On exhaustion:
  - mark action status as `dead_letter`
  - persist callback payload + error for manual replay

## Observability minimum

Emit structured logs with:

- `dedupe_key`
- `request_id`
- `session_id`
- `external_action_id`
- `status`
- `attempt`
- `error` (if any, redacted)

Track metrics:

- requests created
- duplicate events ignored
- callback signature failures
- continuation retry count
- dead-letter count

## Smoke test

1. Start bridge: `python examples/session_event_bridge.py`
2. POST fake event to `/managed-agent/event`
3. Confirm request in CENTCOM
4. Resolve in dashboard
5. Confirm callback accepted and continuation mapping logged
6. Force continuation failure and confirm dead-letter write
