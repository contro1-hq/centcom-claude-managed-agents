---
name: centcom-claude-managed-agents
description: Build and harden a bridge between Claude Managed Agents action-needed events and Contro1/CENTCOM approval workflows.
user_invocable: true
---

# CENTCOM + Claude Managed Agents Skill

Use this skill when implementing a managed-agent bridge that requires operator approvals and instruction-based continuation.

## Required configuration

```bash
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_secret
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

### Incoming from Contro1 callback

- verify signature
- extract protocol response
- map decision/instruction to Anthropic continuation action

## Implementation steps

1. Build session event listener.
2. Filter only `requires_action` events.
3. Deduplicate by `session_id + external_action_id`.
4. Create Contro1 request via SDK protocol method.
5. Store mapping table:
   - `request_id`
   - `session_id`
   - `external_action_id`
   - `action_type`
   - `continuation_mode`
6. Receive signed callback from Contro1.
7. Resolve mapping and send Anthropic continuation payload.
8. Retry continuation API on transport failure (bounded backoff).

## Decision mapping policy

- `approved` -> continue action
- `denied` -> deny action with explicit reason
- instruction payload/message -> continue via instruction-mode action
- `timed_out` -> fail closed unless policy explicitly allows fail open

## Security requirements

- Never trust callback body without signature verification.
- Reject stale timestamps to reduce replay risk.
- Keep idempotency key deterministic and bounded.
- Avoid storing raw secrets in logs.
- Validate that callback `request_id` exists in mapping table before continuation.

## Common mistakes to avoid

- Creating multiple Contro1 requests for one replayed event.
- Mapping `revise` to hard deny instead of instruction mode.
- Losing correlation IDs between event ingest and callback handling.
- Retrying continuation without idempotent keys.
