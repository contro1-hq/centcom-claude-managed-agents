---
name: centcom-claude-managed-agents
description: Integrate Contro1 approval requests, signed callbacks, logs, and evidence into Claude Managed Agents session event flows.
user_invocable: true
---

# Contro1 + Claude Managed Agents Skill

Use this skill when integrating Contro1 into an existing Claude Managed Agents bridge.

## Rules

- Listen to the Claude Managed Agents session event stream.
- When the session emits `session.status_idle` with `stop_reason.type == "requires_action"`, inspect the blocking `stop_reason.event_ids`.
- For `agent.tool_use`, create a Contro1 approval request and then send `user.tool_confirmation` with `result: "allow"` or `result: "deny"`.
- For `agent.custom_tool_use`, create a Contro1 approval request before executing the custom tool, then send `user.custom_tool_result` only after approval.
- Use `session_id` as `correlation_id` so the full managed-agent session appears in one case timeline.
- Use `session_id:event_id` in `external_request_id` so replayed or reprocessed events are idempotent.
- Verify signed Contro1 callbacks before sending any Claude continuation event.
- Treat rejected, cancelled, timed_out, invalid signatures, and unknown request IDs as fail-closed for production actions.
- Do not use Control Map in the normal approval path; use it only when routing fails or you need to see who is currently available.

## Setup

```bash
pip install anthropic centcom flask python-dotenv
```

```bash
ANTHROPIC_API_KEY=your_anthropic_key
ANTHROPIC_WEBHOOK_SIGNING_KEY=whsec_from_anthropic_console
CENTCOM_API_KEY=cc_live_your_key
CENTCOM_BASE_URL=https://api.contro1.com/api/centcom/v1
CENTCOM_WEBHOOK_SECRET=whsec_your_contro1_secret
PUBLIC_BASE_URL=https://your-bridge.example.com
```

Claude Managed Agents endpoints require Anthropic's managed-agents beta header. The Anthropic SDK sets it automatically for beta managed-agent calls.

## 1. Short example: ask for approval

Use this when a blocking event needs a human decision before Claude can continue.

```python
request = centcom.create_request(
    type="approval",
    question=f"Allow Claude managed-agent tool event {event_id}?",
    context={"event": blocking_event},
    callback_url=f"{PUBLIC_BASE_URL}/webhooks/contro1",
    required_role="developer",
    external_request_id=f"claude:{session_id}:{event_id}",
    correlation_id=session_id,
    metadata={
        "integration": "claude-managed-agents",
        "session_id": session_id,
        "blocking_event_id": event_id,
        "blocking_event_type": blocking_event["type"],
    },
)
```

## 2. Full production bridge shape

A production bridge should stream session events, detect `session.status_idle` with `stop_reason.type == "requires_action"`, create a Contro1 request for each blocking event, persist the mapping, wait for a signed Contro1 callback, and then send the correct Claude response event.

```python
with anthropic_client.beta.sessions.events.stream(session_id) as stream:
    for event in stream:
        if event.type != "session.status_idle" or not event.stop_reason:
            continue
        if event.stop_reason.type != "requires_action":
            continue

        for event_id in event.stop_reason.event_ids:
            blocking_event = events_by_id[event_id]
            request = centcom.create_request(
                type="approval",
                question=f"Allow Claude event {event_id}?",
                context={"event": blocking_event},
                callback_url=f"{PUBLIC_BASE_URL}/webhooks/contro1",
                required_role="developer",
                external_request_id=f"claude:{session_id}:{event_id}",
                correlation_id=session_id,
                metadata={
                    "integration": "claude-managed-agents",
                    "session_id": session_id,
                    "blocking_event_id": event_id,
                    "blocking_event_type": blocking_event["type"],
                },
            )
            store_mapping(request["id"], session_id, event_id, blocking_event["type"])
```

Then in the signed Contro1 webhook handler:

```python
payload = verify_contro1_webhook(request_body, headers)
mapping = load_mapping(payload["request_id"])
approved = payload["status"] == "approved" and payload.get("response", {}).get("approved")

if mapping.blocking_event_type == "agent.tool_use":
    anthropic_client.beta.sessions.events.send(
        mapping.session_id,
        events=[{
            "type": "user.tool_confirmation",
            "tool_use_id": mapping.event_id,
            "result": "allow" if approved else "deny",
            **({} if approved else {"deny_message": "Denied in Contro1"}),
        }],
    )
elif mapping.blocking_event_type == "agent.custom_tool_use":
    if not approved:
        send_custom_tool_denial(mapping.session_id, mapping.event_id)
    else:
        result = execute_custom_tool(mapping.event_id)
        anthropic_client.beta.sessions.events.send(
            mapping.session_id,
            events=[{
                "type": "user.custom_tool_result",
                "custom_tool_use_id": mapping.event_id,
                "content": [{"type": "text", "text": result}],
            }],
        )
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
    },
})

print(preview["satisfiable"])            # can this request be routed?
print(preview.get("on_shift_capacity"))  # who is currently available?
print(preview.get("fallback_reviewers")) # who can receive fallback routing?
print(preview.get("warnings"))           # why routing may fail
```

## 4. Log autonomous and delivery actions

Every approval request already stores the human decision in Contro1. Use audit records for autonomous allowed actions, Claude response-event delivery, and dead-letter records.

```python
centcom.log_action(
    action="claude_managed_agent.read_only_action_allowed",
    summary="Allowed read-only managed-agent event without human approval",
    source={"integration": "claude-managed-agents", "run_id": event_id},
    outcome="success",
    severity="info",
    correlation_id=session_id,
)
```

For a post-approval continuation result:

```python
centcom.log_action(
    action="claude_managed_agent.response_event_delivered",
    summary=f"Sent Claude response event for {event_id}",
    source={"integration": "claude-managed-agents", "workflow_id": blocking_event_type, "run_id": event_id},
    outcome="success",
    correlation_id=session_id,
    in_reply_to={"type": "request", "id": request_id},
)
```

For exhausted continuation retries:

```python
centcom.log_action(
    action="claude_managed_agent.response_event_dead_lettered",
    summary=f"Could not deliver Claude response event: {last_error}",
    source={"integration": "claude-managed-agents", "workflow_id": blocking_event_type, "run_id": event_id},
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

- Request evidence shows one reviewed event: context, policy, reviewer, decision, callback, timestamps, and final response.
- Case timeline shows all approvals and audit records that share the same `correlation_id`.

## Common mistakes to avoid

- Treating `requires_action` as the event itself. It is the stop reason on `session.status_idle`; the actionable events are referenced by `stop_reason.event_ids`.
- Sending `user.tool_confirmation` for a custom tool. Custom tools need `user.custom_tool_result`.
- Creating multiple Contro1 requests for one replayed blocking event.
- Continuing after rejected, cancelled, timed_out, or invalid callback results.
- Losing the `session_id` between event ingest, callback handling, and audit logging.
- Treating Control Map as a required step before every approval.

## Reference links

- Website: https://contro1.com
- Documentation: https://contro1.com/docs/claude-managed-agents-human-approval
- Repo: https://github.com/contro1-hq/centcom-claude-managed-agents
- Claude Managed Agents overview: https://platform.claude.com/docs/en/managed-agents/overview
- Claude session event stream: https://platform.claude.com/docs/en/managed-agents/events-and-streaming
- Claude permission policies: https://platform.claude.com/docs/en/managed-agents/permission-policies
- Contro1 webhooks: https://contro1.com/docs/webhooks
