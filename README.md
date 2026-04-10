# centcom-claude-managed-agents

Starter scaffold for Claude Managed Agents -> Contro1 approvals.

This repository is the V1 bridge pattern for:

1. listening to session events (`requires_action`)
2. creating Contro1 protocol requests
3. mapping operator decisions back to managed-agent continuation actions

## Files

- `.env.example`
- `requirements.txt`
- `examples/session_event_bridge.py`
- `docs/claude-managed-agents-connector.md`
- `skills/centcom-claude-managed-agents.md`

## V1 decisions

- one Contro1 request per action-needed event
- dedupe by `session_id + external_action_id`
- "revise" is mapped to **instruction mode** (not deny-only)

## Quick Start

```bash
python -m venv .venv
. .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
python examples/session_event_bridge.py
```

The script is intentionally a local scaffold and does not include Anthropic SDK wiring yet.

## What the starter already solves

- protocol v1 request mapping (`decision` / `instruction`)
- dedupe for replay/reconnect events
- external action correlation (`session_id + external_action_id`)
- callback endpoint placeholder for mapping to:
  - tool confirmation
  - custom tool result
  - interrupt + follow-up message

## Recommended next implementation step

Replace the local `/managed-agent/event` input with your real session stream listener and wire `/centcom-callback` to the exact Anthropic continuation API your runtime uses.
