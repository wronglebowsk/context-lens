#!/usr/bin/env python3
"""
analyze_context.py - Antigravity Session Context & Token Analyzer

Inspects conversation transcripts (.jsonl) and plan telemetry to provide:
- Total active context window usage (when the session exposes token metering)
- Prompt cache hit rate (cache_read_tokens vs input_tokens)
- Base system prompt & tool schema overhead (estimate)
- Composition breakdown: user prompts, assistant output, tool outputs and
  system-injected context (reminders / checkpoints)
- Top context-consuming steps ("Context Hogs"), labeled by real step type
- Subscription plan quota position, with a *measured* per-token burn rate
  derived from a persisted baseline (never inferred from account-wide usage)
- Implementation plan progress efficiency (% of plan completed per token)
- Turn-by-turn context growth

Schema note
-----------
Antigravity transcripts are mixed-type event streams, not just
USER_INPUT / PLANNER_RESPONSE / GENERIC. Tool results carry dedicated `type`
values (RUN_COMMAND, VIEW_FILE, CODE_ACTION, GREP_SEARCH, LIST_DIRECTORY,
SEARCH_WEB, READ_URL_CONTENT); injected system guidance arrives as
EPHEMERAL_MESSAGE, SYSTEM_MESSAGE and CHECKPOINT. Token metering is present
only on *some* PLANNER_RESPONSE steps and is entirely absent on many
sessions, so token figures are reported as "metered" vs "estimated" and never
silently as zero.
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# Colors (disabled automatically for non-TTY / NO_COLOR / --no-color)
# ---------------------------------------------------------------------------
BOLD = DIM = CYAN = GREEN = YELLOW = BLUE = MAGENTA = RED = RESET = ""


def _set_color(enabled):
    global BOLD, DIM, CYAN, GREEN, YELLOW, BLUE, MAGENTA, RED, RESET
    if enabled:
        BOLD, DIM, CYAN, GREEN = "\033[1m", "\033[2m", "\033[36m", "\033[32m"
        YELLOW, BLUE, MAGENTA, RED = "\033[33m", "\033[34m", "\033[35m", "\033[31m"
        RESET = "\033[0m"
    else:
        BOLD = DIM = CYAN = GREEN = YELLOW = BLUE = MAGENTA = RED = RESET = ""


def color_supported():
    if os.environ.get("NO_COLOR") is not None:
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Transcript schema
# ---------------------------------------------------------------------------
# Steps that represent the *result* of a tool/action and carry payload `content`.
TOOL_RESULT_TYPES = {
    "RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "GREP_SEARCH",
    "LIST_DIRECTORY", "SEARCH_WEB", "READ_URL_CONTENT",
    "GENERIC",  # background-task / task-management notices
}
# Steps that inject system context back into the prompt.
INJECTED_TYPES = {"EPHEMERAL_MESSAGE", "SYSTEM_MESSAGE", "CHECKPOINT"}

# Friendly labels for the raw step `type`.
TYPE_LABELS = {
    "USER_INPUT": "user-input",
    "PLANNER_RESPONSE": "assistant-text",
    "RUN_COMMAND": "run_command",
    "VIEW_FILE": "view_file",
    "CODE_ACTION": "code_action",
    "GREP_SEARCH": "grep_search",
    "LIST_DIRECTORY": "list_dir",
    "SEARCH_WEB": "search_web",
    "READ_URL_CONTENT": "read_url_content",
    "GENERIC": "task_notice",
    "EPHEMERAL_MESSAGE": "ephemeral_reminder",
    "SYSTEM_MESSAGE": "system_message",
    "CHECKPOINT": "context_checkpoint",
    "ERROR_MESSAGE": "error",
    "CONVERSATION_HISTORY": "conversation_history",
}

# Expected result type for a requested tool call (authoritative pairing).
CALL_EXPECTED_TYPE = {
    "run_command": "RUN_COMMAND",
    "view_file": "VIEW_FILE",
    "grep_search": "GREP_SEARCH",
    "list_dir": "LIST_DIRECTORY",
    "search_web": "SEARCH_WEB",
    "read_url_content": "READ_URL_CONTENT",
    "write_to_file": "CODE_ACTION",
    "replace_file_content": "CODE_ACTION",
    "multi_replace_file_content": "CODE_ACTION",
}

STATE_DIR = os.path.expanduser("~/.cache/context-lens")


def find_latest_transcript(explicit_path=None):
    if explicit_path:
        if os.path.exists(explicit_path):
            return explicit_path
        raise FileNotFoundError(f"Specified transcript path does not exist: {explicit_path}")

    env_path = os.environ.get("TRANSCRIPT_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    candidates = []
    base_dirs = [
        "~/.gemini/antigravity-cli",
        "~/.gemini/antigravity",
        "~/.gemini/antigravity-ide",
    ]
    for base in base_dirs:
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            pattern = os.path.expanduser(f"{base}/brain/*/.system_generated/logs/{name}")
            candidates.extend(glob.glob(pattern))

    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def parse_transcript(transcript_path):
    if transcript_path.endswith("transcript.jsonl"):
        full_candidate = transcript_path.replace("transcript.jsonl", "transcript_full.jsonl")
        if os.path.exists(full_candidate):
            transcript_path = full_candidate

    steps = []
    with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    return steps, transcript_path


def fetch_quota_telemetry():
    """Queries live plan quota limits from `agy -p /quota` if agy is installed."""
    try:
        res = subprocess.run(["agy", "-p", "/quota"], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return None
        quotas = []
        for line in res.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                family = parts[0].strip()
                limit_desc = parts[1].strip()
                pct_str = parts[2].strip().replace("%", "")
                reset_time = parts[3].strip() if len(parts) > 3 else ""
                try:
                    remaining_pct = int(pct_str)
                except ValueError:
                    continue
                quotas.append({
                    "family": family,
                    "limit": limit_desc,
                    "remaining_pct": remaining_pct,
                    "used_pct": 100 - remaining_pct,
                    "reset_time": reset_time,
                })
        return quotas or None
    except Exception:
        return None


def _quota_baseline_path(conv_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conv_id or "unknown")
    return os.path.join(STATE_DIR, f"quota-{safe}.json")


def load_quota_baseline(conv_id):
    try:
        with open(_quota_baseline_path(conv_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_quota_baseline(conv_id, quota_metrics, billed_tokens):
    """Persist the current quota position so a later run can measure the delta."""
    snap = {
        "billed_tokens": billed_tokens,
        "families": {
            f"{q['family']}::{q['limit']}": {"remaining_pct": q["remaining_pct"]}
            for q in quota_metrics
        },
    }
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_quota_baseline_path(conv_id), "w", encoding="utf-8") as f:
            json.dump(snap, f)
    except Exception:
        pass


def detect_implementation_plan(artifact_dir, explicit_plan_path=None):
    """Scans for active plan/task markdown files to calculate execution progress."""
    candidates = []
    if explicit_plan_path and os.path.exists(explicit_plan_path):
        candidates.append(explicit_plan_path)

    if artifact_dir and os.path.exists(artifact_dir):
        for pattern in ["plan.md", "task.md", "tasks.md", "implementation_plan.md", "*.md"]:
            candidates.extend(glob.glob(os.path.join(artifact_dir, pattern)))

    for path in candidates:
        if os.path.isdir(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            completed = len(re.findall(r"-\s*\[[xX]\]", content))
            pending = len(re.findall(r"-\s*\[\s*\]", content))
            total = completed + pending
            if total > 0:
                return {
                    "file": path,
                    "completed_tasks": completed,
                    "pending_tasks": pending,
                    "total_tasks": total,
                    "progress_pct": round((completed / total) * 100, 1),
                }
        except Exception:
            continue
    return None


def extract_target(stype, content):
    """Best-effort human-readable target for a step (file path, command, ...)."""
    c = content or ""
    if stype == "VIEW_FILE":
        m = re.search(r"File Path:\s*`?file://([^`\n]+)", c)
        if m:
            return m.group(1).strip()
    if stype in ("RUN_COMMAND", "GENERIC"):
        m = re.search(r"Task Description:\s*(.+)", c)
        if m:
            return m.group(1).strip()
    if stype == "CODE_ACTION":
        m = re.search(r"(?:Created|Modified|Updated|Deleted)\s+file\s+file://([^\s]+)", c)
        if m:
            return m.group(1)
    if stype == "GREP_SEARCH":
        m = re.search(r'"File":"([^"]+)"', c)
        if m:
            return m.group(1)
    if stype == "LIST_DIRECTORY":
        m = re.search(r'"name":"([^"]+)"', c)
        if m:
            return m.group(1) + "/ …"
    if stype == "CHECKPOINT":
        m = re.match(r"\{\{\s*(CHECKPOINT[^}]*)\}\}", c)
        if m:
            return m.group(1).strip()
    if stype in ("EPHEMERAL_MESSAGE", "SYSTEM_MESSAGE"):
        m = re.search(r"content=(.{0,60})", c)
        if m:
            return m.group(1).strip()
    if stype in ("PLANNER_RESPONSE", "USER_INPUT"):
        return c.strip().replace("\n", " ")[:60]
    return ""


def _match_call(pending, stype):
    """Return (index, name, quality) of the best pending tool call for a result."""
    for i, name in enumerate(pending):
        if CALL_EXPECTED_TYPE.get(name) == stype:
            return i, name, "matched"
    # A bare FIFO guess is deliberately NOT made: interleaved results make it
    # unreliable (it produced nonsense attributions like a view_file call
    # owning a background-task notice).
    return None, None, None


def analyze_steps(steps, transcript_path, explicit_plan_path=None):
    user_inputs = []
    ranked_steps = []          # tool-output + injected + assistant steps, ranked later
    turns_timeline = []
    pending_calls = []         # tool-call names awaiting a result
    tool_calls_requested = 0
    tool_calls_matched = 0

    composition = {            # category -> chars
        "user": 0,
        "assistant": 0,
        "tool": 0,
        "injected": 0,
        "error": 0,
    }
    tool_category_totals = {}

    metered = []               # planner responses that carried token fields
    unmetered_planner = 0
    first_metered = None

    for s in steps:
        stype = s.get("type")
        sidx = s.get("step_index", 0)
        content = s.get("content") or ""

        if stype == "USER_INPUT":
            composition["user"] += len(content)
            user_inputs.append({
                "step_index": sidx,
                "length": len(content),
                "preview": content.strip().replace("\n", " ")[:80],
            })

        elif stype == "PLANNER_RESPONSE":
            has_tokens = (s.get("input_tokens") is not None
                          or s.get("cache_read_tokens") is not None)
            assistant_chars = len(content) + len(s.get("thinking") or "")
            composition["assistant"] += assistant_chars

            if has_tokens:
                cached = s.get("cache_read_tokens") or 0
                inp = s.get("input_tokens") or 0
                out = s.get("output_tokens") or 0
                rec = {
                    "step_index": sidx,
                    "cached_tokens": cached,
                    "input_tokens": inp,
                    "output_tokens": out,
                    "total_context": cached + inp,
                }
                metered.append(rec)
                turns_timeline.append(rec)
                if first_metered is None:
                    first_metered = rec
            else:
                unmetered_planner += 1

            for tc in (s.get("tool_calls") or []):
                tool_calls_requested += 1
                pending_calls.append(tc.get("name"))

            if assistant_chars:
                ranked_steps.append({
                    "step_index": sidx,
                    "type": stype,
                    "label": TYPE_LABELS.get(stype, stype),
                    "requested_call": None,
                    "match_quality": None,
                    "target": extract_target(stype, content),
                    "chars": assistant_chars,
                    "est_tokens": max(1, assistant_chars // 4),
                    "category": "assistant",
                })

        elif stype in TOOL_RESULT_TYPES:
            chars = len(content)
            composition["tool"] += chars
            label = TYPE_LABELS.get(stype, stype)
            tool_category_totals[label] = tool_category_totals.get(label, 0) + chars

            idx, name, quality = _match_call(pending_calls, stype)
            if idx is not None:
                pending_calls.pop(idx)
                tool_calls_matched += 1

            ranked_steps.append({
                "step_index": sidx,
                "type": stype,
                "label": label,
                "requested_call": name,
                "match_quality": quality,
                "target": extract_target(stype, content),
                "chars": chars,
                "est_tokens": max(1, chars // 4),
                "category": "tool",
            })

        elif stype in INJECTED_TYPES:
            chars = len(content)
            composition["injected"] += chars
            label = TYPE_LABELS.get(stype, stype)
            tool_category_totals[label] = tool_category_totals.get(label, 0) + chars
            ranked_steps.append({
                "step_index": sidx,
                "type": stype,
                "label": label,
                "requested_call": None,
                "match_quality": None,
                "target": extract_target(stype, content),
                "chars": chars,
                "est_tokens": max(1, chars // 4),
                "category": "injected",
            })

        elif stype == "ERROR_MESSAGE":
            composition["error"] += len(content) + len(s.get("error") or "")

    if not metered and unmetered_planner == 0:
        return None

    # Derive conversation id & artifact directory
    conv_id = "unknown"
    artifact_dir = None
    parts = transcript_path.split(os.sep)
    if "brain" in parts:
        b_idx = parts.index("brain")
        if b_idx + 1 < len(parts):
            conv_id = parts[b_idx + 1]
            artifact_dir = os.sep.join(parts[:b_idx + 2])

    # Token telemetry (only if the session actually metered any turn)
    metering_available = bool(metered)
    latest = metered[-1] if metered else None
    cumulative_uncached = sum(r["input_tokens"] for r in metered)
    cumulative_cached = sum(r["cached_tokens"] for r in metered)
    cumulative_output = sum(r["output_tokens"] for r in metered)
    cumulative_billed = cumulative_uncached + cumulative_output

    if latest:
        current_context = latest["total_context"]
        cache_hit_rate = (latest["cached_tokens"] / current_context * 100) if current_context else 0.0
    else:
        current_context = None
        cache_hit_rate = None

    # System baseline estimate: first metered prompt minus the first user message.
    system_baseline_est = None
    if first_metered is not None:
        first_user_len = user_inputs[0]["length"] if user_inputs else 0
        system_baseline_est = max(0, first_metered["input_tokens"] - (first_user_len // 4))

    # Quota telemetry + measured burn rate (delta vs persisted baseline)
    quotas = fetch_quota_telemetry()
    baseline = load_quota_baseline(conv_id)
    plan_quota_metrics = []
    if quotas:
        for q in quotas:
            key = f"{q['family']}::{q['limit']}"
            metric = {
                "family": q["family"],
                "limit": q["limit"],
                "used_pct": q["used_pct"],
                "remaining_pct": q["remaining_pct"],
                "reset_time": q["reset_time"],
                "measured_tokens_per_1pct": None,
                "measured_pct_per_1k_tokens": None,
            }
            if baseline and cumulative_billed > 0:
                b = baseline.get("families", {}).get(key)
                if b is not None:
                    moved_pct = b["remaining_pct"] - q["remaining_pct"]
                    moved_tokens = cumulative_billed - baseline.get("billed_tokens", 0)
                    if moved_pct > 0 and moved_tokens > 0:
                        metric["measured_tokens_per_1pct"] = int(moved_tokens / moved_pct)
                        metric["measured_pct_per_1k_tokens"] = moved_pct / (moved_tokens / 1000.0)
            plan_quota_metrics.append(metric)

    # Implementation plan telemetry
    impl_plan = detect_implementation_plan(artifact_dir, explicit_plan_path)
    impl_plan_metrics = None
    if impl_plan:
        prog = impl_plan["progress_pct"]
        plan_pct_per_1k = (prog / (cumulative_billed / 1000.0)) if cumulative_billed > 0 else 0.0
        tokens_per_1pct = (cumulative_billed / prog) if prog > 0 else 0
        rem_pct = 100.0 - prog
        impl_plan_metrics = {
            "plan_file": impl_plan["file"],
            "completed_tasks": impl_plan["completed_tasks"],
            "total_tasks": impl_plan["total_tasks"],
            "progress_pct": prog,
            "plan_pct_completed_per_1k_tokens": round(plan_pct_per_1k, 4),
            "tokens_per_1pct_progress": int(tokens_per_1pct),
            "est_tokens_to_finish": int(rem_pct * tokens_per_1pct),
            "basis": "metered" if cumulative_billed > 0 else "unavailable",
        }

    return {
        "conversation_id": conv_id,
        "transcript_path": transcript_path,
        "total_steps": len(steps),
        "token_metering_available": metering_available,
        "metered_turns": len(metered),
        "unmetered_planner_turns": unmetered_planner,
        "latest_metered_step": latest["step_index"] if latest else None,
        "current_context_tokens": current_context,
        "cache_read_tokens": latest["cached_tokens"] if latest else None,
        "input_tokens": latest["input_tokens"] if latest else None,
        "latest_output_tokens": latest["output_tokens"] if latest else None,
        "cache_hit_rate_pct": round(cache_hit_rate, 2) if cache_hit_rate is not None else None,
        "system_baseline_tokens": system_baseline_est,
        "cumulative_uncached_tokens": cumulative_uncached,
        "cumulative_cached_tokens": cumulative_cached,
        "cumulative_output_tokens": cumulative_output,
        "cumulative_billed_tokens": cumulative_billed,
        "tool_calls_requested": tool_calls_requested,
        "tool_calls_matched": tool_calls_matched,
        "plan_quota_metrics": plan_quota_metrics,
        "implementation_plan_metrics": impl_plan_metrics,
        "total_user_chars": composition["user"],
        "total_user_est_tokens": composition["user"] // 4,
        "total_user_messages": len(user_inputs),
        "total_assistant_chars": composition["assistant"],
        "total_tool_output_chars": composition["tool"],
        "total_tool_output_est_tokens": composition["tool"] // 4,
        "total_injected_chars": composition["injected"],
        "composition": {
            "user_chars": composition["user"],
            "assistant_chars": composition["assistant"],
            "tool_chars": composition["tool"],
            "injected_chars": composition["injected"],
            "error_chars": composition["error"],
        },
        "tool_category_totals": tool_category_totals,
        "tool_results": ranked_steps,
        "turns_timeline": turns_timeline,
    }


def format_bar(pct, length=24, fill_char="█", empty_char="░", color=CYAN):
    filled = int(round(length * (pct / 100.0)))
    filled = max(0, min(length, filled))
    return f"{color}{fill_char * filled}{DIM}{empty_char * (length - filled)}{RESET}"


def print_cli_report(data, top_hogs=5, show_timeline=False):
    conv_id = data["conversation_id"]
    metered = data["token_metering_available"]

    print(f"\n{BOLD}{CYAN}╭──────────────────────────────────────────────────────────────╮{RESET}")
    print(f"{BOLD}{CYAN}│                   CONTEXT LENS TELEMETRY                     │{RESET}")
    print(f"{BOLD}{CYAN}╰──────────────────────────────────────────────────────────────╯{RESET}")
    print(f"{DIM}Conversation ID:{RESET} {conv_id}")
    print(f"{DIM}Log Source:{RESET}      {data['transcript_path']}")

    # 1. Context window loading
    print(f"\n{BOLD}Context Window Loading (Latest Metered Turn):{RESET}")
    if metered:
        ctx = data["current_context_tokens"]
        cached = data["cache_read_tokens"]
        inp = data["input_tokens"]
        out = data["latest_output_tokens"]
        hit_rate = data["cache_hit_rate_pct"]
        ctx_bar = format_bar(hit_rate, length=30, color=GREEN)
        print(f"  {BOLD}{ctx:,}{RESET} total prompt tokens "
              f"{DIM}(step {data['latest_metered_step']}){RESET}")
        print(f"  [{ctx_bar}] {BOLD}{GREEN}{hit_rate:.1f}% cached{RESET}")
        print(f"  ├─ {GREEN}Prompt Cache Hit (Cached Prefix):{RESET}  {cached:,} tokens")
        print(f"  ├─ {YELLOW}Fresh Turn Input (Uncached):{RESET}       {inp:,} tokens")
        print(f"  └─ {BLUE}Turn Generation (Output/CoT):{RESET}      {out:,} tokens")
        if data["unmetered_planner_turns"]:
            print(f"  {DIM}({data['metered_turns']} of "
                  f"{data['metered_turns'] + data['unmetered_planner_turns']} "
                  f"assistant turns carried token metering){RESET}")
    else:
        print(f"  {YELLOW}Token metering unavailable for this session.{RESET}")
        print(f"  {DIM}No PLANNER_RESPONSE step carried input/cache/output token fields;{RESET}")
        print(f"  {DIM}figures below are character-based estimates only (~chars/4).{RESET}")

    # 2. Quota telemetry
    quotas = data.get("plan_quota_metrics")
    if quotas:
        print(f"\n{BOLD}Subscription Plan Quota:{RESET}")
        if metered:
            print(f"  {DIM}Session billed tokens (this transcript):{RESET} "
                  f"{BOLD}{data['cumulative_billed_tokens']:,}{RESET}")
        print(f"  {DIM}Quota percentages are account-wide and include every session.{RESET}")
        if not any(q.get("measured_tokens_per_1pct") for q in quotas):
            print(f"  {DIM}(no measured per-token burn yet; a baseline will be recorded){RESET}")
        for q in quotas:
            used = q["used_pct"]
            rem = q["remaining_pct"]
            q_bar = format_bar(used, length=18,
                               color=RED if used > 80 else (YELLOW if used > 40 else GREEN))
            family_lbl = f"{q['family']} ({q['limit'].replace(' Remaining', '')})"
            print(f"\n  • {BOLD}{family_lbl}:{RESET}")
            print(f"    Quota Used : {BOLD}{used}%{RESET} [{q_bar}] "
                  f"{DIM}({rem}% remaining, resets {q['reset_time']}){RESET}")
            if q.get("measured_tokens_per_1pct") is not None:
                print(f"    Measured   : ~{BOLD}{q['measured_tokens_per_1pct']:,}{RESET} tokens "
                      f"per 1% of plan {DIM}({q['measured_pct_per_1k_tokens']:.5f}% / 1k tokens, "
                      f"delta vs baseline){RESET}")

    # 3. Implementation plan efficiency
    impl = data.get("implementation_plan_metrics")
    if impl:
        prog = impl["progress_pct"]
        prog_bar = format_bar(prog, length=20, color=GREEN)
        print(f"\n{BOLD}Implementation Plan Progress Efficiency:{RESET}")
        print(f"  Plan File  : {DIM}{impl['plan_file']}{RESET}")
        print(f"  Tasks Done : {BOLD}{impl['completed_tasks']}/{impl['total_tasks']}{RESET} "
              f"[{prog_bar}] {BOLD}{GREEN}{prog:.1f}%{RESET}")
        if impl["basis"] == "metered":
            print(f"  Efficiency : {BOLD}{CYAN}{impl['plan_pct_completed_per_1k_tokens']:.3f}%{RESET} "
                  f"of plan completed per 1k tokens")
            print(f"  Pace       : ~{BOLD}{impl['tokens_per_1pct_progress']:,}{RESET} tokens "
                  f"per 1% plan progress")
            if prog < 100:
                print(f"  Est. Left  : ~{BOLD}{impl['est_tokens_to_finish']:,}{RESET} tokens "
                      f"to finish remaining tasks {DIM}(linear extrapolation){RESET}")
        else:
            print(f"  Efficiency : {DIM}token metering unavailable for this session{RESET}")

    # 4. Composition breakdown
    comp = data["composition"]
    print(f"\n{BOLD}Prompt Composition Breakdown {DIM}(character-based){RESET}{BOLD}:{RESET}")
    rows = [
        ("Base System & Tool Schemas", data.get("system_baseline_tokens"), MAGENTA,
         "initial prompt overhead, est."),
        ("User Prompts", comp["user_chars"] // 4, BLUE,
         f"{data['total_user_messages']} messages, {comp['user_chars']:,} chars"),
        ("Assistant Output (text + CoT)", comp["assistant_chars"] // 4, CYAN,
         f"{comp['assistant_chars']:,} chars"),
        ("Tool Outputs", comp["tool_chars"] // 4, YELLOW,
         f"{comp['tool_chars']:,} chars"),
        ("Injected System/Reminders/Checkpoints", comp["injected_chars"] // 4, RED,
         f"{comp['injected_chars']:,} chars"),
    ]
    for label, tok, col, note in rows:
        if tok is None:
            print(f"  • {col}{label}:{RESET} ~n/a {DIM}({note}){RESET}")
        else:
            print(f"  • {col}{label}:{RESET} ~{tok:,} tokens {DIM}({note}){RESET}")
    if comp["error_chars"]:
        print(f"  • {DIM}Errors:{RESET} ~{comp['error_chars'] // 4:,} tokens")

    # 5. Context usage by step type
    cat_totals = data["tool_category_totals"]
    if cat_totals:
        print(f"\n{BOLD}Context Usage by Step Type:{RESET}")
        sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        max_chars = sorted_cats[0][1] if sorted_cats else 1
        for tool, chars in sorted_cats:
            pct_bar = format_bar(chars / max_chars * 100, length=14, color=YELLOW)
            print(f"  {tool:<20} : {BOLD}{chars // 4:>6,}{RESET} tokens "
                  f"[{pct_bar}] {DIM}({chars:,} chars){RESET}")

    # 6. Context hogs (all step categories, ranked by size)
    results = sorted(data["tool_results"], key=lambda x: x["chars"], reverse=True)
    if results:
        print(f"\n{BOLD}Top Context Consumers (Context Hogs):{RESET}")
        for idx, item in enumerate(results[:top_hogs], 1):
            t_name = item["label"]
            t_target = item["target"]
            if len(t_target) > 50:
                t_target = t_target[:47] + "..."
            via = ""
            if item.get("requested_call"):
                qmark = "" if item.get("match_quality") == "matched" else "?"
                via = f" {DIM}← {item['requested_call']}{qmark}{RESET}"
            cat = item["category"]
            cat_tag = "" if cat == "tool" else f" {DIM}[{cat}]{RESET}"
            print(f"  {idx}. {BOLD}{t_name:<20}{RESET}{cat_tag}{via} "
                  f"{DIM}[step {item['step_index']}]{RESET}")
            print(f"     ~{item['est_tokens']:,} tokens {DIM}({item['chars']:,} chars){RESET}")
            if t_target:
                print(f"     {DIM}↳ {t_target}{RESET}")

    # 7. Timeline
    if show_timeline and len(data["turns_timeline"]) > 1:
        print(f"\n{BOLD}Turn-by-Turn Context Progression:{RESET}")
        for t in data["turns_timeline"]:
            print(f"  Turn @ Step {t['step_index']:<5} : {t['total_context']:>7,} tokens "
                  f"{DIM}({t['cached_tokens']:>7,} cached){RESET}")

    print(f"\n{CYAN}{'─' * 64}{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Context & Token Analyzer")
    parser.add_argument("-p", "--path", help="Path to transcript.jsonl or transcript_full.jsonl")
    parser.add_argument("--plan", help="Explicit path to an implementation plan/tasks markdown file")
    parser.add_argument("-j", "--json", action="store_true", help="Output raw JSON data")
    parser.add_argument("-H", "--hogs", type=int, default=5, help="Number of context hogs to show (default: 5)")
    parser.add_argument("-t", "--timeline", action="store_true", help="Show turn-by-turn context growth")
    parser.add_argument("--no-state", action="store_true",
                        help="Do not read or write the quota baseline state file")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors")

    args = parser.parse_args()
    _set_color(color_supported() and not args.no_color)

    try:
        target_path = find_latest_transcript(args.path)
        if not target_path:
            print(f"{RED}Error: No active Antigravity session transcript found.{RESET}", file=sys.stderr)
            sys.exit(1)

        steps, resolved_path = parse_transcript(target_path)
        data = analyze_steps(steps, resolved_path, explicit_plan_path=args.plan)

        if not data:
            print(f"{RED}Error: Unable to parse steps from transcript.{RESET}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(json.dumps(data, indent=2))
            return

        # Persist/refresh the quota baseline for measured burn rates.
        if (not args.no_state and data.get("plan_quota_metrics")
                and data["cumulative_billed_tokens"] > 0):
            save_quota_baseline(
                data["conversation_id"],
                data["plan_quota_metrics"],
                data["cumulative_billed_tokens"],
            )

        print_cli_report(data, top_hogs=args.hogs, show_timeline=args.timeline)

    except Exception as e:
        print(f"{RED}Error analyzing session context: {e}{RESET}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
