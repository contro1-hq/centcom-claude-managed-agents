---
name: centcom-claude-managed-agents
description: Integrate Contro1 approval requests, signed callbacks, logs, and evidence into Claude Managed Agents session-action flows.
user_invocable: true
---

# Contro1 + Claude Managed Agents Skill

Use this skill when integrating Contro1 into an existing Claude Managed Agents bridge.

## Rules

- Create one Contro1 approval request per risky `requires_action` item.
- Use `session_id` as `correlation_id` so the full managed-agent session appears in one case timeline.
- Use `session_id:external_action_id` in `external_request_id` so replayed events are idempotent.
- Verify signed Contro1 callbacks before sending any continuation payload back to the managed-agent runtime.
- Treat rejected, cancelled, timed_out, invalid signatures, and unknown request IDs as fail-closed for production actions.
- Log autonomous allowed actions, continuation delivery failures, and dead letters when they need evidence.
- Do not use Control Map in the normal approval path; use it only when routing fails or you need to see who is currently available.

## Setup

```bash
pip install centcom flask python-dotenv
```

```bash
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://api.contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_secret
PUBLIC_BASE_URL=https://your-bridge.example.com
ANTHROPIC_CONTINUATION_URL=https://your-managed-agent-runtime.example.com/continue
ANTHROPIC_API_KEY=your_anthropic_key
```

## 1. Short example: ask for approval

When a managed-agent session emits a risky action, create one Contro1 request:

```python
request = centcom.create_request(
    type="approval",
    question=f"Approve managed-agent action: {action.type}?",
    context=action.input,
    callback_url=f"{PUBLIC_BASE_URL}/webhooks/contro1",
    required_role="developer",
    external_request_id=f"claude:{session_id}:{action.id}",
    correlation_id=session_id,
    metadata={
        "integration": "claude-managed-agents",
        "session_id": session_id,
        "external_action_id": action.id,
        "action_type": action.type,
    },
)
```

## 2. Full production bridge shape

A production bridge should listen for `requires_action`, create the request, persist the mapping, wait for a signed callback, and continue only on approval.

```python
for event in managed_agent.stream_events(session_id):
    if event.type != "requires_action":
        continue

    for action in event.required_actions:
        request = centcom.create_request(
            type="approval",
            question=f"Approve managed-agent action: {action.type}?",
            context=action.input,
            callback_url=f"{PUBLIC_BASE_URL}/webhooks/contro1",
            required_role="developer",
            external_request_id=f"claude:{session_id}:{action.id}",
            correlation_id=session_id,
            metadata={
                "integration": "claude-managed-agents",
                "session_id": session_id,
                "external_action_id": action.id,
                "action_type": action.type,
            },
        )
        store_mapping(request["id"], session_id, action.id)
```

Then in the Contro1 webhook handler:

```python
payload = verify_contro1_webhook(request_body, headers)
mapping = load_mapping(payload["request_id"])

if payload["status"] != "approved":
    deny_managed_agent_action(mapping.session_id, mapping.external_action_id, payload["response"])
else:
    continue_managed_agent_action(mapping.session_id, mapping.external_action_id, payload["response"])
```

## 3. If routing fails, check who is available

Most integrations do not need Control Map in the normal approval path. If a request cannot be routed, times out unexpectedly, or your bridge wants to show a clear operational error, call Control Map to see who is currently available.

```python
preview = centcom.post("/requests/control-map", {
    "approval_requirements": {
        "required_roles": ["developer"],
        "required_approvals": 1,
    },
    "metadata": {
        "integration": "claude-managed-agents",
        "session_id": session_id,
        "action_type": action_type,
    },
})

print(preview["satisfiable"])            # can this request be routed?
print(preview.get("on_shift_capacity"))  # who is currently available?
print(preview.get("fallback_reviewers")) # who can receive fallback routing?
print(preview.get("warnings"))           # why routing may fail
```

## 4. Log autonomous and delivery actions

Every approval request already stores the human decision in Contro1. Use audit records for autonomous allowed actions, continuation delivery, and dead-letter records.

```python
centcom.log_action(
    action="claude_managed_agent.read_only_action_allowed",
    summary="Allowed read-only tool result without human approval",
    source={"integration": "claude-managed-agents", "run_id": external_action_id},
    outcome="success",
    severity="info",
    correlation_id=session_id,
)
```

For a post-approval continuation result:

```python
centcom.log_action(
    action="claude_managed_agent.continuation_delivered",
    summary=f"Delivered operator response to managed-agent action {external_action_id}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    outcome="success",
    correlation_id=session_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

For exhausted continuation retries:

```python
centcom.log_action(
    action="claude_managed_agent.continuation_dead_lettered",
    summary=f"Could not deliver operator response: {last_error}",
    source={"integration": "claude-managed-agents", "workflow_id": action_type, "run_id": external_action_id},
    outcome="failure",
    severity="warning",
    correlation_id=session_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

## 5. Get evidence

Use evidence when compliance or incident review needs proof of what happened.

```python
evidence = centcom.get(f"/requests/{request_id}/evidence")
timeline = centcom.get(f"/cases/{session_id}")
```

- Request evidence shows one reviewed action: context, policy, reviewer, decision, callback, timestamps, and final response.
- Case timeline shows all approvals and audit records that share the same `correlation_id`.

## Common mistakes to avoid

- Creating multiple Contro1 requests for one replayed `requires_action` event.
- Continuing after rejected, cancelled, timed_out, or invalid callback results.
- Losing the `session_id` between event ingest, callback handling, and audit logging.
- Dropping continuation failures instead of logging a dead-letter record.
- Treating Control Map as a required step before every approval.

## Reference links

- Website: https://contro1.com
- Documentation: https://contro1.com/docs/claude-managed-agents-human-approval
- Repo: https://github.com/contro1-hq/centcom-claude-managed-agents
- Production bridge example: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/examples/session_event_bridge.py
- Connector architecture doc: https://github.com/contro1-hq/centcom-claude-managed-agents/blob/main/docs/claude-managed-agents-connector.md
- Contro1 webhooks: https://contro1.com/docs/webhooks
