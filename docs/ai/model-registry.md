---
description: "Which LLM Scotri runs on. Decided 2026-10-07 (OPEN_ITEMS.md #4): one model for every stage, Claude Sonnet 5.5 (`claude-sonnet-5-5`), one vendor (Anthropic). services/config/model_registry.yaml is the machine-readable twin that app/container.py actually reads."
last-validated-commit: n/a — hand-maintained registry, update directly if the model changes
---

# Model registry

## Current assignment

**Decided 2026-10-07 (OPEN_ITEMS.md #4): Scotri uses one model, Claude Sonnet 5.5, for every
lifecycle stage.** There is no per-stage routing and no second vendor.

| Scope | Vendor | Model | Model id | Secret |
|---|---|---|---|---|
| Every stage (`default`) | anthropic | Claude Sonnet 5.5 | `claude-sonnet-5-5` | `ANTHROPIC_API_KEY` |

Machine-readable twin: [`../../services/config/model_registry.yaml`](../../services/config/model_registry.yaml).
Same hand-sync convention as `repo_registry.yaml`: if the two ever drift, this file wins.

## How it is wired

`app/container.py`'s `agent_runtime` reads `default` from the YAML and passes its `model` to the
Agent SDK as `ClaudeAgentOptions.model`. Leaving that unset would omit `--model` from the CLI call
and silently run whatever the CLI defaults to (fixed 2026-09-25, see
`adapters/agent_sdk/claude_agent_adapter.py`). One engagement is one `query()` call that runs the
whole lifecycle, so the model applies to every stage of that run. To change the model, edit both
files and restart the API/MCP processes.

## If this is ever revisited

- **Another Claude model for one stage** would mean splitting that stage into its own Agent SDK
  call. Then it is a `stages:` entry in the YAML, which the loader already supports, with a fallback
  to `default`.
- **Another vendor** is real integration work, not a config edit. The Agent SDK's skill-loading and
  tool-calling loop, which drives this whole lifecycle from `.claude/skills/`, only works with Claude
  models. A non-Claude stage would need its own hand-built tool-calling wrapper (e.g. LiteLLM or the
  vendor's SDK).
