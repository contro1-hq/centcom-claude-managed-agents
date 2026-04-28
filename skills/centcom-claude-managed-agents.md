# Contro1 Claude Managed Agents Skill

Use this when wiring Anthropic/Claude managed-agent session events into Contro1.

## Rules

- Derive `thread_id` from `session_id`; keep `external_request_id` scoped to the individual action.
- Use `create_protocol_request` for `requires_action` events that need operator approval or instruction.
- Use `log_action` for continuation delivery, dead-lettering, and any autonomous allowed action.
- When logging after an operator callback, include `in_reply_to={"type": "request", "id": request_id}`.
- Dead-letter failed continuations and log them with `outcome="failure"` and `severity="warning"`.

## Threaded continuation

```python
client.log_action(
    action="claude_managed_agent.continuation_dead_lettered",
    summary=f"Could not deliver operator response: {last_error}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    outcome="failure",
    severity="warning",
    thread_id=thread_id,
    in_reply_to={"type": "request", "id": request_id},
)
```
---
name: centcom-claude-managed-agents
description: Build and harden a production bridge between Claude Managed Agents action-needed events and Contro1/CENTCOM approval workflows.
user_invocable: true
---

# CENTCOM + Claude Managed Agents Skill

Use this skill when implementing a managed-agent bridge that requires operator approvals and instruction-based continuation.

## Installation

```bash
pip install centcom flask python-dotenv
```

## Required configuration

```bash
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://api.contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_secret
PUBLIC_BASE_URL=https://your-bridge.example.com
BRIDGE_DB_PATH=bridge_state.db
CALLBACK_MAX_SKEW_SECONDS=300
CONTINUATION_RETRY_ATTEMPTS=4
CONTINUATION_RETRY_BASE_SECONDS=1.0
SIMULATE_CONTINUATION=true
ANTHROPIC_CONTINUATION_URL=
ANTHROPIC_API_KEY=
```

## Integration contract

### Incoming from managed agents

- `session_id`
- `external_action_id` (or equivalent event action ID)
- `action_type` (`tool_confirmation`, `custom_tool_result`, `interrupt_message`)
- action payload / tool input

### Outgoing to Contro1

Create protocol v1 request with:

- `request_type`: typically `review`
- `source.integration`: `claude-managed-agents`
- `source.session_id`
- `external_request_id`: `session_id:external_action_id`
- `continuation.mode`: `instruction` (recommended default)
- `continuation.callback_url`: `${PUBLIC_BASE_URL}/centcom-callback`
- `approval_policy`: required for high-risk actions that need two-person review

Example high-risk policy:

```json
{
  "approval_policy": {
    "mode": "threshold",
    "required_approvals": 2,
    "required_roles": ["manager", "admin"],
    "separation_of_duties": true,
    "fail_closed_on_timeout": true
  }
}
```

### Incoming from Contro1 callback

- verify signature
- verify timestamp freshness
- extract protocol response
- map decision/instruction to Anthropic continuation action

## Implementation steps

1. Build session event listener (SSE/webhook consumer).
2. Filter only `requires_action` events.
3. Compute dedupe key `session_id:external_action_id`.
4. Persist mapping before network calls.
5. Create Contro1 request via SDK protocol method.
6. Store mapping table:
   - `request_id`
   - `session_id`
   - `external_action_id`
   - `action_type`
   - `continuation_mode`
7. Receive signed callback from Contro1.
8. Resolve mapping and send Anthropic continuation payload.
9. Retry continuation API on transport failure (bounded exponential backoff).
10. Write dead-letter record when retries are exhausted.

## Decision mapping policy

- `approved` -> continue action
- `denied` -> deny action with explicit reason
- instruction payload/message -> continue via instruction-mode action
- `timed_out` -> fail closed unless policy explicitly allows fail open
- `cancelled` -> deny with explicit operator cancellation reason
- quorum pending -> do not continue the managed-agent action yet; wait for final callback

## Security requirements

- Never trust callback body without signature verification.
- Reject stale timestamps to reduce replay risk.
- Keep idempotency key deterministic and bounded.
- Avoid storing raw secrets in logs.
- Validate that callback `request_id` exists in mapping table before continuation.
- Never continue the same `session_id:external_action_id` twice.
- For deploys, vendor payments, data deletion, and privilege escalation, require two-person approval and fail closed before quorum.

## Common mistakes to avoid

- Creating multiple Contro1 requests for one replayed event.
- Mapping `revise` to hard deny instead of instruction mode.
- Losing correlation IDs between event ingest and callback handling.
- Retrying continuation without idempotent keys.
- Dropping exhausted continuation failures instead of dead-lettering.
- Continuing after the first approval while the second approval is still pending.

## Full reference links

- Repo: https://github.com/contro1-hq/centcom-claude-managed-agents
- Production bridge example: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/examples/session_event_bridge.py
- Connector architecture doc: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/docs/claude-managed-agents-connector.md
- Skill file source: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/skills/centcom-claude-managed-agents.md
- Core Python SDK: https://github.com/contro1-hq/centcom
- Protocol docs: https://contro1.com/docs/audit-records-and-threads
