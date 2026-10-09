---
name: context
description: Inspect the active session context size, tokens, quota plan burn rate, and implementation plan progress.
---

When the user runs `/context`, execute the `context-lens` script on the active session transcript and display the telemetry results.

### Options
- `--plan <file>`: Measure implementation plan progress (% completed per token and projected tokens remaining).
- `--timeline`, `-t`: Display turn-by-turn context growth and cache checkpoints.
- `-H <count>`: Number of context hogs to display (default: 5).
- `--json`, `-j`: Output raw machine-readable JSON telemetry.

### Execution
Run the analysis script:
```bash
python3 "$PLUGIN_DIR/skills/context-lens/scripts/analyze_context.py" "$@"
```
