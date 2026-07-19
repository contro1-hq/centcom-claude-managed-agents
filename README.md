# centcom-claude-managed-agents

Production-oriented blueprint for bridging Claude Managed Agents blocking session events into Contro1/CENTCOM approvals using Integration Protocol v1.

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

1. Stream Claude Managed Agents session events.
2. Detect `session.status_idle` with `stop_reason.type = "requires_action"`.
3. Create exactly one Contro1 protocol request per blocking `stop_reason.event_ids` item.
4. Verify signed callbacks from Contro1.
5. Map callback outcomes to `user.tool_confirmation` or `user.custom_tool_result`.
6. Persist correlation state, dedupe replays, retry response-event transport, and dead-letter exhausted failures.

## What this skill helps with

- Creating approval requests for blocking `agent.tool_use` and `agent.custom_tool_use` events.
- Using `external_request_id` for idempotent action review.
- Using `correlation_id` to keep a full session timeline together.
- Handling signed callback verification before continuation.
- Producing audit-ready evidence for approvals, denials, retries, and dead letters.

## Security note

Production approvals must go through Contro1 APIs and signed webhooks.

## Files

- `.env.example`
- `requirements.txt`
- `examples/session_event_bridge.py`
- `docs/claude-managed-agents-connector.md`
- `skills/centcom-claude-managed-agents.md`

## Contract decisions (required)

- Dedupe key: `session_id:event_id`
- One request per blocking event ID
- Tool confirmations resume with `user.tool_confirmation`
- Custom tools resume with `user.custom_tool_result`
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
4. Confirm callback arrives at `/centcom-callback` and bridge logs the Claude response-event mapping.

## Request and log pattern

Use `correlation_id = session_id` to group all events from one managed-agent session into a single case timeline:

```python
tool_input = dict(blocking_event.get("input") or {})
reason = tool_input.pop("reason", None)  # require `reason` on risky custom tools

request = client.create_protocol_request({
    "title": f"Managed agent event: {blocking_event_type}",
    "request_type": "review",
    "source": {"integration": "claude-managed-agents", "session_id": session_id, "run_id": event_id},
    "context": {
        "action": {"tool": blocking_event.get("name"), "input": tool_input},
        "machine_observed": {"blocking_event_id": event_id, "blocking_event_type": blocking_event_type},
        "agent_reported": {"justification": reason},
    },
    "continuation": {"mode": "event", "callback_url": callback_url},
    "external_request_id": dedupe_key,
    "correlation_id": session_id,
})
```

## Send context the reviewer can trust

Build request `context` in the bridge from the exact `blocking_event["input"]` your code already holds, the session/event that triggered it, and the agent's own justification - and require a `reason` parameter on risky custom tools so Claude produces that justification at decision time, not after the event is already blocking. Keep the split explicit: verbatim facts under `machine_observed`, model-authored text under `agent_reported`. Agent-reported text should never change routing, `required_role`, or `approval_policy`, and a high-risk custom-tool event that arrives without its required `reason` should fail closed rather than being forwarded to a human to guess. Full pattern: https://contro1.com/docs/requests-api.

Log the response-event result in the same case:

```python
client.log_action(
    action="claude_managed_agent.response_event_delivered",
    summary=f"Delivered Claude response event for {event_id}",
    source={"integration": "claude-managed-agents", "workflow_id": blocking_event_type, "run_id": event_id},
    correlation_id=session_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

## Control Map preview

Most integrations do not need Control Map in the normal approval path. If a request cannot be routed, times out unexpectedly, or your bridge wants to show a clear operational error, call Control Map to see who is currently available.

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
    print("Routing setup needed:", preview["warnings"])
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

- **Local default**: `SIMULATE_CLAUDE_RESPONSE=true` (logs Claude response events without calling Anthropic).
- **Production**: replace the simulated transport with the Anthropic Managed Agents session events API and send either `user.tool_confirmation` or `user.custom_tool_result`.

## Production checklist

- Run behind HTTPS and stable public callback URL (`PUBLIC_BASE_URL`).
- Persist `actions` and `dead_letters` in durable storage (replace sqlite when needed).
- Monitor retry exhaustion and dead letters.
- Add health checks and structured logging.
- Lock down callback endpoint with signature + timestamp validation.

## Notes

The example keeps the mapping logic, persistence model, and retry behavior explicit. In production, send response events through the Anthropic Managed Agents session events API: `user.tool_confirmation` for `agent.tool_use`, and `user.custom_tool_result` for `agent.custom_tool_use`.

## Related repositories

- [centcom](https://github.com/contro1-hq/centcom) - Python SDK for direct API integrations
- [centcom-sdk](https://github.com/contro1-hq/centcom-sdk) - JavaScript/TypeScript SDK for direct API integrations
- [contro1-microsoft-agent-governance-toolkit-integration](https://github.com/contro1-hq/contro1-microsoft-agent-governance-toolkit-integration) - companion bridge for Microsoft AGT `require_approval` policy decisions

## Governance readiness

For teams operating AI in regulated environments:
- [EU AI Act readiness guide](https://contro1.com/guides/eu-ai-act-readiness)
- [US AI Governance readiness guide](https://contro1.com/guides/us-ai-governance-readiness)
