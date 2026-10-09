# Context Lens for Antigravity

**Context Lens** is an Antigravity plugin that inspects your session's context window, prompt caching efficiency, token composition, tool outputs, and plan burn rate in real time.

---

## Features

- **Total Context Telemetry**: View the number of tokens sent to the LLM on the most recent *metered* turn.
- **Prompt Cache Hit Rate**: Monitor Gemini prompt caching (`cache_read_tokens` vs `input_tokens`).
- **Subscription Plan Quota**: Live position from `/quota`, plus a **measured** per-token burn rate taken from the delta against a persisted baseline (never inferred from account-wide usage).
- **Implementation Plan Progress**: Measures task checklist completion (`- [x]` vs `- [ ]`) relative to tokens spent, showing pace and projected token budget to finish.
- **System Prompt Baseline**: Isolate base system instructions + tool schema overhead from conversation history.
- **Context Hogs Diagnostic**: Ranks the individual steps that consumed the most context — tool outputs (`run_command`, `view_file`, `grep_search`, …) *and* injected system context (`ephemeral_reminder`, `system_message`, `context_checkpoint`) — with their real step type and a file/command target.
- **Turn-by-Turn Timeline**: Inspect how your context accumulated turn-by-turn.
- **Interactive Slash Command**: Access directly from the CLI or IDE via `/context`.

### A note on token metering

Antigravity only attaches `input_tokens` / `cache_read_tokens` /
`output_tokens` to *some* `PLANNER_RESPONSE` steps, and on many sessions to
none at all. When no turn is metered the tool says so explicitly and reports
character-based estimates (`~chars/4`) instead of printing a misleading row of
zeros. Similarly, context hogs and the composition breakdown are always
character-based estimates.

---

## Directory Structure

```text
context-lens/
├── plugin.json                # Plugin manifest
├── README.md                  # Documentation
├── LICENSE                    # Apache 2.0 license
├── assets/
│   └── logo.svg               # Vector icon
├── commands/
│   └── context.md             # Slash command definition (/context)
├── skills/
│   └── context-lens/
│       ├── SKILL.md           # Progressive disclosure instructions
│       └── scripts/
│           └── analyze_context.py # Telemetry parser
├── tests/
│   └── test_analyze_context.py    # Unit tests (hermetic; no network/CLI)
└── bin/
    └── context-lens           # Standalone terminal executable
```

---

## Installation

### Method 1: Install via `agy plugin install` (Recommended)

From anywhere in your terminal:
```bash
# Clone the repository
git clone https://github.com/wronglebowsk/context-lens.git

# Install the plugin
agy plugin install ./context-lens
```

### Method 2: Link into Global Customizations
Alternatively, copy or symlink into your global plugins directory:
```bash
ln -s "$(pwd)/context-lens" ~/.gemini/config/plugins/context-lens
```

---

## Usage

### In the Antigravity CLI / IDE Chat:
```text
/context
```
Or with flags:
```text
/context --plan path/to/plan.md
/context --timeline
/context -H 10
```

### In Your Shell:
```bash
# Run standalone
./bin/context-lens

# Track against an implementation plan
./bin/context-lens --plan path/to/plan.md

# Show timeline of turns
./bin/context-lens --timeline

# Output machine-readable JSON
./bin/context-lens --json

# Colored output only on a TTY; force-disable with:
./bin/context-lens --no-color

# Skip reading/writing the quota baseline state (~/.cache/context-lens):
./bin/context-lens --no-state
```

---

## Testing

```bash
python3 -m unittest discover -s tests -v
```

The suite is hermetic: it never shells out to `agy` and never touches your real
baseline state directory.

---

## License

Licensed under the [Apache-2.0 License](./LICENSE).
