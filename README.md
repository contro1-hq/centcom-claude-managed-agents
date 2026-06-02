# centcom-claude-managed-agents

Production-oriented blueprint for bridging Claude Managed Agents action-needed events into Contro1/CENTCOM approvals using Integration Protocol v1.

Website: https://contro1.com

Documentation: https://contro1.com/docs/claude-managed-agents-human-approval

Repository description:

Human approval and audit-control skill for Claude Managed Agents, routing risky agent actions through Contro1 with signed callbacks and policy-ready logs.

## Agent Integration Kit

To save time, give your coding agent this skill. It inspects your system, reports governance gaps, and suggests Contro1 integration (optional):

```
https://contro1.com/agent-kit
```

## What this blueprint covers

1. Ingest `requires_action` events from your managed-agent stream.
2. Create exactly one Contro1 protocol request per action-needed event.
3. Verify signed callbacks from Contro1.
4. Map callback outcomes to managed-agent continuation payloads.
5. Persist correlation state, dedupe replays, retry continuation transport, and dead-letter exhausted failures.

## What this skill helps with

- Creating approval requests for managed-agent session actions.
- Using `external_request_id` for idempotent action review.
- Using `correlation_id` to keep a full session timeline together.
- Handling signed callback verification before continuation.
- Producing audit-ready evidence for approvals, denials, retries, and dead letters.

## Security note

Production approvals must go through Contro1 APIs and signed webhooks. MCP or coding-agent skills can help implement and inspect the integration, but they are not the production approval transport.

## Files

- `.env.example`
- `requirements.txt`
- `examples/session_event_bridge.py`
- `docs/claude-managed-agents-connector.md`
- `skills/centcom-claude-managed-agents.md`

## Contract decisions (required)

- Dedupe key: `session_id:external_action_id`
- One request per action-needed event
- `continuation.mode=instruction` by default
- Status mapping is explicit (`approved`, `denied`, `cancelled`, `timed_out`)
- Callback signature + timestamp verification is mandatory

## Quick Start

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python examples/session_event_bridge.py
```

Then:

1. POST a sample event to `/managed-agent/event`.
2. Confirm a request is created in CENTCOM.
3. Resolve request in dashboard.
4. Confirm callback arrives at `/centcom-callback` and bridge logs continuation mapping.

## Request and log pattern

Use `correlation_id = session_id` to group all events from one managed-agent session into a single case timeline:

```python
request = client.create_protocol_request({
    "title": f"Managed agent action: {action_type}",
    "request_type": "review",
    "source": {"integration": "claude-managed-agents", "session_id": session_id, "run_id": external_action_id},
    "continuation": {"mode": "instruction", "callback_url": callback_url},
    "external_request_id": dedupe_key,
    "correlation_id": session_id,
})
```

Log the continuation result in the same case:

```python
client.log_action(
    action="claude_managed_agent.continuation_delivered",
    summary=f"Delivered operator response to managed agent action {external_action_id}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    correlation_id=session_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

## Control Map preview

For sessions with high-risk action types, check routing at session start. Cache the result for the session duration.

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

## Production pattern: Agent Plugin

Wrap Contro1 calls behind a plugin to reduce per-event overhead:

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

## Local vs production mode

- **Local default**: `SIMULATE_CONTINUATION=true` (logs continuation payload without calling Anthropic).
- **Production**: set `SIMULATE_CONTINUATION=false` and configure:
  - `ANTHROPIC_CONTINUATION_URL`
  - `ANTHROPIC_API_KEY`

## Production checklist

- Run behind HTTPS and stable public callback URL (`PUBLIC_BASE_URL`).
- Persist `actions` and `dead_letters` in durable storage (replace sqlite when needed).
- Monitor retry exhaustion and dead letters.
- Add health checks and structured logging.
- Lock down callback endpoint with signature + timestamp validation.

## Notes

The example intentionally avoids Anthropic SDK-specific assumptions. Keep the mapping logic, persistence model, and retry behavior as-is, and swap only the `send_to_anthropic_continuation(...)` transport for your runtime endpoint.

## Related repositories

- [centcom](https://github.com/contro1-hq/centcom) - Python SDK for direct API integrations
- [centcom-sdk](https://github.com/contro1-hq/centcom-sdk) - JavaScript/TypeScript SDK for direct API integrations
- [contro1-microsoft-agent-governance-toolkit-integration](https://github.com/contro1-hq/contro1-microsoft-agent-governance-toolkit-integration) - companion bridge for Microsoft AGT `require_approval` policy decisions

## Governance readiness

For teams operating AI in regulated environments:
- [EU AI Act readiness guide](https://contro1.com/guides/eu-ai-act-readiness)
- [US AI Governance readiness guide](https://contro1.com/guides/us-ai-governance-readiness)
