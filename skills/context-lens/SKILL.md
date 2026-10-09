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
- `--hogs <n>`, `-H <n>`: Show the top `n` steps that generated the largest outputs (default: 5).
- `--json`, `-j`: Output raw machine-readable JSON telemetry.
- `--path <path>`, `-p <path>`: Explicit path to a `transcript.jsonl` or `transcript_full.jsonl` file (otherwise auto-detects the active session).
- `--no-state`: Do not read or write the quota baseline state file (`~/.cache/context-lens`).
- `--no-color`: Disable ANSI colors (also auto-disabled when stdout is not a TTY or `NO_COLOR` is set).

## Metric Definitions

1. **Plan Quota**: The live account-wide position from `agy -p /quota`. Percentages include *every* session, so they are reported as context, not attributed to this session. The **burn rate** (tokens per 1% of plan) is only shown when it can be *measured*: the tool persists a baseline (`~/.cache/context-lens/quota-<conversation>.json`) on first run and, on later runs, reports `tokens / (baseline% - current%)`. It is never inferred by dividing account-wide usage by this session's tokens.
2. **Implementation Plan Progress**: Measures the percentage of checklist items completed (`- [x]` vs `- [ ]`) relative to tokens spent, showing pace and projected token budget to finish. Requires metered tokens; otherwise reported as unavailable.
3. **Total Context Window**: Sum of `cache_read_tokens` (cached prefix) and `input_tokens` (new turn delta) sent to the LLM on the most recent **metered** turn.
4. **Cache Hit Rate**: Percentage of the total prompt served from Gemini's prompt cache on that turn.
5. **Base System Overhead**: Estimate from the first *metered* prompt minus the first user message (~`chars/4`).
6. **Context Hogs**: Individual steps ranked by content size — tool results (`run_command`, `view_file`, `grep_search`, `code_action`, `list_dir`, `search_web`, `read_url_content`, `task_notice`) and injected system context (`ephemeral_reminder`, `system_message`, `context_checkpoint`) — labeled by real step type and a file/command target.
7. **Token metering availability**: Antigravity attaches token fields to only some `PLANNER_RESPONSE` steps, and to none on many sessions. When no turn is metered the tool states this explicitly and falls back to character-based estimates rather than reporting zeros.
8. **Tool-result encoding** (`tool_result_encoding` in JSON): Antigravity either emits dedicated typed result steps (paired to the requesting call by type) or serializes every tool result as `GENERIC` (paired by order to the requesting call, labelled with the real tool name and a target taken from the call arguments). The encoding is auto-detected per transcript, so `task_notice` only appears for genuinely unpaired notices.
