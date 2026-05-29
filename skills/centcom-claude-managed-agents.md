# Contro1 Claude Managed Agents Skill

Use this when wiring Anthropic/Claude managed-agent session events into Contro1.

## Rules

- Derive `correlation_id` from `session_id`; keep `external_request_id` scoped to the individual action (`session_id:external_action_id`).
- Use `create_protocol_request` for `requires_action` events that need operator approval or instruction.
- Use `log_action` for continuation delivery, dead-lettering, and any autonomous allowed action.
- When logging after an operator callback, include `in_reply_to={"type": "request", "id": request_id}`.
- Dead-letter failed continuations and log them with `outcome="failure"` and `severity="warning"`.

## Case continuity

All events from the same managed-agent session share one `correlation_id` so the dashboard shows the complete case timeline:

```python
client.log_action(
    action="claude_managed_agent.continuation_dead_lettered",
    summary=f"Could not deliver operator response: {last_error}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    outcome="failure",
    severity="warning",
    correlation_id=session_id,
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
- `correlation_id`: `session_id` (groups all events from this session into one case)
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

## Check routing before submitting (Control Map)

For sessions with high-risk action types that require specific roles, verify routing is ready before submitting the request. Cache the result per session bootstrap.

```python
preview = client.post("/requests/control-map", {
    "approval_requirements": {"required_roles": ["manager"], "required_approvals": 2},
    "approval_policy": {
        "mode": "threshold",
        "required_approvals": 2,
        "separation_of_duties": True,
        "fail_closed_on_timeout": True,
    },
})

if not preview["satisfiable"]:
    raise RuntimeError(f"Cannot route managed-agent review: {preview['warnings']}")
```

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
- Losing case IDs between event ingest and callback handling.
- Retrying continuation without idempotent keys.
- Dropping exhausted continuation failures instead of dead-lettering.
- Continuing after the first approval while the second approval is still pending.

## Production pattern: Agent Plugin

```python
from datetime import datetime, timedelta
from centcom import CentcomClient

class Contro1Plugin:
    def __init__(self, client: CentcomClient, cache_ttl_minutes: int = 10):
        self._client = client
        self._cache: dict = {}
        self._ttl = timedelta(minutes=cache_ttl_minutes)

    def preview_policy(self, approval_requirements: dict, approval_policy: dict) -> dict:
        key = str(sorted(approval_requirements.items()))
        cached = self._cache.get(key)
        if cached and datetime.utcnow() < cached["expires"]:
            return cached["data"]
        result = self._client.post("/requests/control-map", {
            "approval_requirements": approval_requirements,
            "approval_policy": approval_policy,
        })
        self._cache[key] = {"data": result, "expires": datetime.utcnow() + self._ttl}
        return result

    def request_human_review(self, payload: dict) -> dict:
        return self._client.create_protocol_request(payload)

    def log_audit_action(self, payload: dict) -> dict:
        return self._client.log_action(**payload)

    def resume_from_decision(self, case_id: str) -> dict:
        return self._client.get(f"/cases/{case_id}")
```

## Full reference links

- Repo: https://github.com/contro1-hq/centcom-claude-managed-agents
- Production bridge example: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/examples/session_event_bridge.py
- Connector architecture doc: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/docs/claude-managed-agents-connector.md
- Skill file source: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/skills/centcom-claude-managed-agents.md
- Core Python SDK: https://github.com/contro1-hq/centcom
- Microsoft AGT companion skill: https://github.com/contro1-hq/contro1-microsoft-agent-governance-toolkit-integration/blob/main/skills/contro1-microsoft-agent-governance-toolkit-integration.md
- Protocol docs: https://contro1.com/docs/audit-records-and-cases

## Governance readiness

For teams operating under EU or US AI governance requirements, see:
- https://contro1.com/guides/eu-ai-act-readiness
- https://contro1.com/guides/us-ai-governance-readiness
