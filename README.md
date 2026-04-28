# centcom-claude-managed-agents

Production-oriented blueprint for bridging Claude Managed Agents action-needed events into Contro1/CENTCOM approvals using Integration Protocol v1.

## What this blueprint covers

1. Ingest `requires_action` events from your managed-agent stream.
2. Create exactly one Contro1 protocol request per action-needed event.
3. Verify signed callbacks from Contro1.
4. Map callback outcomes to managed-agent continuation payloads.
5. Persist correlation state, dedupe replays, retry continuation transport, and dead-letter exhausted failures.

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

## Request and log pattern

Use a request for actions that need approval or instruction before continuation:

```python
request = client.create_protocol_request({
    "title": f"Managed agent action: {action_type}",
    "request_type": "review",
    "source": {"integration": "claude-managed-agents", "session_id": session_id, "run_id": external_action_id},
    "continuation": {"mode": "instruction", "callback_url": callback_url},
    "external_request_id": dedupe_key,
    "thread_id": thread_id,
})
```

Log the continuation result in the same thread:

```python
client.log_action(
    action="claude_managed_agent.continuation_delivered",
    summary=f"Delivered operator response to managed agent action {external_action_id}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    thread_id=thread_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

See the full bridge example at https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/examples/session_event_bridge.py.
