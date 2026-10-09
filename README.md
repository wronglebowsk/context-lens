# Context Lens for Antigravity

**Context Lens** is an Antigravity plugin that inspects your session's context window, prompt caching efficiency, token composition, tool outputs, and plan burn rate in real time.

---

## Features

- **Total Context Telemetry**: View the exact number of tokens sent to the LLM on the current turn.
- **Prompt Cache Hit Rate**: Monitor Gemini prompt caching (`cache_read_tokens` vs `input_tokens`).
- **Subscription Plan Quota Telemetry**: Live correlation with `/quota` to track percentage of plan rate limits used per token and per 1,000 tokens.
- **Implementation Plan Progress**: Measures task checklist completion (`- [x]` vs `- [ ]`) relative to tokens spent, showing pace and projected token budget to finish.
- **System Prompt Baseline**: Isolate base system instructions + tool schema overhead from conversation history.
- **Context Hogs Diagnostic**: Automatically ranks the individual tool calls (large file reads, noisy shell commands) that consumed the most tokens.
- **Turn-by-Turn Timeline**: Inspect how your context accumulated turn-by-turn.
- **Interactive Slash Command**: Access directly from the CLI or IDE via `/context`.

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
```

---

## License

Licensed under the [Apache-2.0 License](./LICENSE).
