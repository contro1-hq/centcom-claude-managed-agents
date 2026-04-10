# Claude Managed Agents Connector Guide

This guide defines the bridge between Claude Managed Agents action-needed events and Contro1/CENTCOM approvals.

## Target flow

1. Listener receives managed-agent session event stream.
2. On `requires_action`, bridge creates a Contro1 protocol request.
3. Operator handles the request in CENTCOM.
4. Bridge receives signed callback.
5. Bridge maps callback to Anthropic continuation action.

## Required environment

```bash
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_secret
```

## Protocol request mapping

Use one Contro1 request per action-needed event.

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
    "tool_input": {"tool_name": "delete_file"}
  },
  "continuation": {
    "mode": "instruction",
    "callback_url": "https://your-bridge/centcom-callback"
  },
  "external_request_id": "sess_123:action_987"
}
```

## Dedupe strategy (required)

- Key: `session_id + external_action_id`
- Store dedupe key before retry loops
- Treat duplicate create attempts as idempotent no-op

This prevents duplicate operator requests during SSE reconnect/replay.

## Callback verification (required)

Always verify:

- `X-CentCom-Signature`
- `X-CentCom-Timestamp`

Reject callbacks that fail signature check.

## Callback -> Anthropic mapping

Map operator result into one of:

1. **Tool confirmation**
   - approve -> confirm tool
   - deny -> deny with clear reason

2. **Custom tool result**
   - pass `message` and `structured_response` as tool output

3. **Interrupt + message**
   - use when operator gives correction/steering instead of binary allow/deny

## Instruction-mode rule

When operator selects revise/clarify behavior:

- map to `instruction` continuation handling
- do not degrade to plain deny unless policy requires hard stop

## Retry model

- Retry transport failures to Anthropic continuation API with bounded exponential backoff.
- Keep retries idempotent using same external action identifiers.
- Log final dead-letter after retry exhaustion.

## Smoke test

1. Start local bridge: `python examples/session_event_bridge.py`
2. POST a fake `requires_action` event to `/managed-agent/event`
3. Confirm request appears in CENTCOM
4. Approve/deny in dashboard
5. Verify callback is accepted and mapped in logs
