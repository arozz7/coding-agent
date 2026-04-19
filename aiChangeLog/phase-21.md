# Phase 21: Agent System Prompt Architecture Refactoring

## Overview
This phase introduces a formal separation between the agent's persona/instructions and the dynamic user context. Instead of concatenating system prompts with the user's input, the LLM pipeline was updated to pass system prompts as an isolated, top-level role (e.g. `{"role": "system", "content": ...}` for OpenAI/Ollama, or a `system` parameter for Anthropic). 

This improves model comprehension, caching, and explicitly prevents prompt injection inside the task block. We adopted a unified prompt style across all agents (Developer, Reviewer, Tester, Architect, Chat, Planner, Research), matching a concise, tool-specific format while retaining compatibility with internal Regex parsers.

## Files Modified

**LLM Client Layer**
*   `llm/ollama_client.py`: Added `system_prompt` handling.
*   `llm/cloud_api_client.py`: Plumbed `system_prompt` to OpenAI, Anthropic, and OpenRouter payload generators.
*   `llm/model_router.py`: Plumbed `system_prompt` down from the global `generate()` and `generate_stream()` interfaces into the clients, including throughout the `_run_fallback_chain` loops.

**Agent Execution Layer**
*   `agent/agents/developer_agent.py`
*   `agent/agents/planner_agent.py`
*   `agent/agents/research_agent.py`
*   `agent/agents/architect_agent.py`
*   `agent/agents/reviewer_agent.py`
*   `agent/agents/tester_agent.py`
*   `agent/agents/chat_agent.py`

*In each agent:* Restructured `get_system_prompt()` to the new multi-tool descriptive format, and removed system prompt concatenation from within `execute()`.

**Testing Suite**
*   `tests/integration/test_model_fallback.py`: Repaired mock interfaces for `_local_runtime` and `_switch_callbacks` causing previously missing config asserts. Fixed deprecated `_get_local_fallback` assertions by routing them securely to the new parameterised `_get_fallback_chain()` interface and verifying matching behavior.

## Behavior Changes
- The `generate()` and `stream_generate()` methods in both `ModelRouter` and clients now safely accept a `system_prompt` kwargs override.
- Model calls structure their conversational flow around the `system` block, keeping the user `task` purely dynamic which allows caching to heavily optimize repetitive workflows.
- Extraneous text was cleared out of personality blocks, unifying the multi-agent personality system.

## Risks & Assumptions
- **Assumption:** No external codebase heavily depends strictly on the exact message list layout inside the raw clients, as the `system_prompt` will prepend messages at index 0.
- **Risk:** Existing LLM models running on Ollama locally might not technically support a pure system block well depending on the specific model type (some instruction models ignore it), however modern versions of Qwen and Llama natively assume specific boundaries for the system tags and enforce them anyway. Testing proved stable syntax adherence.

## Conclusion
Code changes align perfectly with existing tool definitions, integrating successfully and maintaining parsing capabilities (like `FILE:` tags) in every modified agent wrapper. Integrations tests successfully run locally.
