---
name: context-lens
description: Analyzes active conversation context window loading, prompt caching efficiency, token breakdown (system prompt, user messages, tool outputs), plan quota usage per token, and implementation plan progress. Use whenever the user asks about context size, token usage, plan percentage used per token, or how much memory/history the agent has consumed.
---

# Context Lens: Session Context & Token Telemetry

This skill inspects session transcripts and active plans to diagnose context consumption, prompt cache hit ratios, quota burn rate, and plan execution progress.

## When to Use
- User asks about token counts or context window capacity.
- User asks about **percent of plan used per token** (subscription quota or implementation plan).
- User wants to know how much of the context is system prompt vs conversation history.
- Inspecting which file reads or commands added the most tokens to the conversation.
- Diagnosing latency slowdowns related to context growth.

## How to Run

Execute the bundled Python analyzer:
```bash
python3 <path-to-skill>/scripts/analyze_context.py [options]
```

### Supported Flags:
- `--plan <file>`: Point to an implementation plan/task markdown file (e.g., `plan.md`) to measure `% of plan completed per token` and projected tokens to finish remaining tasks.
- `--timeline`, `-t`: Display turn-by-turn context growth and cache checkpoints.
- `--hogs <n>`, `-H <n>`: Show the top `n` tool calls that generated the largest outputs (default: 5).
- `--json`, `-j`: Output raw machine-readable JSON telemetry.
- `--path <path>`, `-p <path>`: Explicit path to a `transcript.jsonl` or `transcript_full.jsonl` file (otherwise auto-detects the active session).

## Metric Definitions

1. **Plan Quota Burn Rate**: Measures the percentage of the user's weekly and 5-hour rate limits consumed per token and per 1,000 tokens (derived from live `/quota`).
2. **Implementation Plan Progress**: Measures the percentage of checklist items completed (`- [x]` vs `- [ ]`) relative to tokens spent, showing pace and projected token budget to finish.
3. **Total Context Window**: Sum of `cache_read_tokens` (cached prefix) and `input_tokens` (new turn delta) sent to the LLM on the most recent turn.
4. **Cache Hit Rate**: Percentage of the total prompt served from Gemini's prompt cache.
5. **Base System Overhead**: Measured at Step 1 (`input_tokens` minus the first user message).
6. **Context Hogs**: Individual tool calls ranked by output size.
