#!/usr/bin/env python3
"""
analyze_context.py - Antigravity Session Context & Token Analyzer

Inspects conversation transcripts (.jsonl) and plan telemetry to provide:
- Total active context window usage
- Prompt cache hit rate (cache_read_tokens vs input_tokens)
- Base system prompt & tool schema overhead
- User prompt vs. Tool output breakdown
- Top context-consuming tool calls ("Context Hogs")
- Subscription Plan Quota Consumption (% of plan used per token / per 1k tokens)
- Implementation Plan Progress Efficiency (% of plan completed per token)
- Turn-by-turn context growth
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys

# ANSI Colors for terminal output
BOLD = "\033[1m"
DIM = "\033[2m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
RED = "\033[31m"
RESET = "\033[0m"


def find_latest_transcript(explicit_path=None):
    if explicit_path:
        if os.path.exists(explicit_path):
            return explicit_path
        raise FileNotFoundError(f"Specified transcript path does not exist: {explicit_path}")

    # Check environment variable
    env_path = os.environ.get("TRANSCRIPT_PATH")
    if env_path and os.path.exists(env_path):
        return env_path

    # Search across potential Antigravity app directories
    candidates = []
    base_dirs = [
        "~/.gemini/antigravity-cli",
        "~/.gemini/antigravity",
        "~/.gemini/antigravity-ide",
    ]

    for base in base_dirs:
        pattern = os.path.expanduser(f"{base}/brain/*/.system_generated/logs/transcript_full.jsonl")
        candidates.extend(glob.glob(pattern))
        pattern_compact = os.path.expanduser(f"{base}/brain/*/.system_generated/logs/transcript.jsonl")
        candidates.extend(glob.glob(pattern_compact))

    if not candidates:
        return None

    # Pick the most recently modified transcript
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
        lines = res.stdout.strip().splitlines()
        quotas = []
        for line in lines:
            parts = line.split("\t")
            if len(parts) >= 3:
                family = parts[0].strip()
                limit_desc = parts[1].strip()
                pct_str = parts[2].strip().replace("%", "")
                reset_time = parts[3].strip() if len(parts) > 3 else ""
                try:
                    remaining_pct = int(pct_str)
                    used_pct = 100 - remaining_pct
                    quotas.append({
                        "family": family,
                        "limit": limit_desc,
                        "remaining_pct": remaining_pct,
                        "used_pct": used_pct,
                        "reset_time": reset_time
                    })
                except ValueError:
                    continue
        return quotas
    except Exception:
        return None


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
                    "progress_pct": round((completed / total) * 100, 1)
                }
        except Exception:
            continue

    return None


def analyze_steps(steps, transcript_path, explicit_plan_path=None):
    first_planner = None
    latest_planner = None
    user_inputs = []
    tool_calls_pending = []
    tool_results = []
    turns_timeline = []

    cumulative_uncached_tokens = 0
    cumulative_cached_tokens = 0
    cumulative_output_tokens = 0

    total_user_chars = 0
    total_tool_output_chars = 0
    tool_category_totals = {}

    for s in steps:
        stype = s.get("type")
        sidx = s.get("step_index", 0)

        if stype == "USER_INPUT":
            content = s.get("content") or ""
            total_user_chars += len(content)
            user_inputs.append({
                "step_index": sidx,
                "length": len(content),
                "preview": content.strip().replace("\n", " ")[:80]
            })

        elif stype == "PLANNER_RESPONSE":
            if first_planner is None:
                first_planner = s
            latest_planner = s

            cached = s.get("cache_read_tokens") or 0
            inp = s.get("input_tokens") or 0
            out = s.get("output_tokens") or 0
            total_ctx = cached + inp

            cumulative_uncached_tokens += inp
            cumulative_cached_tokens += cached
            cumulative_output_tokens += out

            turns_timeline.append({
                "step_index": sidx,
                "cached_tokens": cached,
                "input_tokens": inp,
                "output_tokens": out,
                "total_context": total_ctx
            })

            # Queue pending tool calls
            for tc in (s.get("tool_calls") or []):
                tool_calls_pending.append((sidx, tc.get("name"), tc.get("args") or {}))

        elif stype == "GENERIC":
            content = s.get("content") or ""
            chars = len(content)
            total_tool_output_chars += chars

            if tool_calls_pending:
                call_step, tool_name, args = tool_calls_pending.pop(0)
            else:
                call_step, tool_name, args = None, "system/other", {}

            target = (
                args.get("CommandLine")
                or args.get("AbsolutePath")
                or args.get("TargetFile")
                or args.get("query")
                or args.get("Url")
                or args.get("toolSummary")
                or ""
            )

            tool_category_totals[tool_name] = tool_category_totals.get(tool_name, 0) + chars

            tool_results.append({
                "call_step": call_step,
                "result_step": sidx,
                "tool": tool_name,
                "target": str(target),
                "chars": chars,
                "est_tokens": max(1, chars // 4)
            })

    if not latest_planner:
        return None

    # Derive Conversation ID & Artifact Directory
    conv_id = "unknown"
    artifact_dir = None
    parts = transcript_path.split(os.sep)
    if "brain" in parts:
        b_idx = parts.index("brain")
        if b_idx + 1 < len(parts):
            conv_id = parts[b_idx + 1]
            artifact_dir = os.sep.join(parts[:b_idx + 2])

    latest_cached = latest_planner.get("cache_read_tokens") or 0
    latest_input = latest_planner.get("input_tokens") or 0
    latest_output = latest_planner.get("output_tokens") or 0
    current_context = latest_cached + latest_input

    # Estimate System prompt baseline:
    first_input = first_planner.get("input_tokens") or 0
    first_user_len = user_inputs[0]["length"] if user_inputs else 0
    system_baseline_est = max(0, first_input - (first_user_len // 4))

    cache_hit_rate = (latest_cached / current_context * 100) if current_context > 0 else 0.0
    cumulative_billed_tokens = cumulative_uncached_tokens + cumulative_output_tokens

    # Fetch Quota Telemetry (Subscription Plan)
    quotas = fetch_quota_telemetry()
    plan_quota_metrics = []
    if quotas:
        for q in quotas:
            used = q["used_pct"]
            pct_per_token = (used / cumulative_billed_tokens) if cumulative_billed_tokens > 0 else 0.0
            pct_per_1k = pct_per_token * 1000
            tokens_per_1pct = (cumulative_billed_tokens / used) if used > 0 else 0
            plan_quota_metrics.append({
                "family": q["family"],
                "limit": q["limit"],
                "used_pct": used,
                "remaining_pct": q["remaining_pct"],
                "reset_time": q["reset_time"],
                "pct_per_token": pct_per_token,
                "pct_per_1k_tokens": pct_per_1k,
                "tokens_per_1pct": int(tokens_per_1pct)
            })

    # Detect Implementation Plan Telemetry
    impl_plan = detect_implementation_plan(artifact_dir, explicit_plan_path)
    impl_plan_metrics = None
    if impl_plan:
        prog = impl_plan["progress_pct"]
        plan_pct_per_token = (prog / cumulative_billed_tokens) if cumulative_billed_tokens > 0 else 0.0
        plan_pct_per_1k = plan_pct_per_token * 1000
        tokens_per_1pct_prog = (cumulative_billed_tokens / prog) if prog > 0 else 0
        rem_pct = 100.0 - prog
        est_tokens_to_finish = int(rem_pct * tokens_per_1pct_prog)

        impl_plan_metrics = {
            "plan_file": impl_plan["file"],
            "completed_tasks": impl_plan["completed_tasks"],
            "total_tasks": impl_plan["total_tasks"],
            "progress_pct": prog,
            "plan_pct_completed_per_1k_tokens": round(plan_pct_per_1k, 4),
            "tokens_per_1pct_progress": int(tokens_per_1pct_prog),
            "est_tokens_to_finish": est_tokens_to_finish
        }

    return {
        "conversation_id": conv_id,
        "transcript_path": transcript_path,
        "total_steps": len(steps),
        "current_context_tokens": current_context,
        "cache_read_tokens": latest_cached,
        "input_tokens": latest_input,
        "latest_output_tokens": latest_output,
        "cache_hit_rate_pct": round(cache_hit_rate, 2),
        "system_baseline_tokens": system_baseline_est,
        "cumulative_uncached_tokens": cumulative_uncached_tokens,
        "cumulative_cached_tokens": cumulative_cached_tokens,
        "cumulative_output_tokens": cumulative_output_tokens,
        "cumulative_billed_tokens": cumulative_billed_tokens,
        "plan_quota_metrics": plan_quota_metrics,
        "implementation_plan_metrics": impl_plan_metrics,
        "total_user_chars": total_user_chars,
        "total_user_est_tokens": total_user_chars // 4,
        "total_user_messages": len(user_inputs),
        "total_tool_output_chars": total_tool_output_chars,
        "total_tool_output_est_tokens": total_tool_output_chars // 4,
        "tool_category_totals": tool_category_totals,
        "tool_results": tool_results,
        "turns_timeline": turns_timeline
    }


def format_bar(pct, length=24, fill_char="█", empty_char="░", color=CYAN):
    filled = int(round(length * (pct / 100.0)))
    filled = max(0, min(length, filled))
    return f"{color}{fill_char * filled}{DIM}{empty_char * (length - filled)}{RESET}"


def print_cli_report(data, top_hogs=5, show_timeline=False):
    conv_id = data["conversation_id"]
    ctx = data["current_context_tokens"]
    cached = data["cache_read_tokens"]
    inp = data["input_tokens"]
    out = data["latest_output_tokens"]
    hit_rate = data["cache_hit_rate_pct"]
    baseline = data["system_baseline_tokens"]
    user_tok = data["total_user_est_tokens"]
    tool_tok = data["total_tool_output_est_tokens"]
    cum_billed = data["cumulative_billed_tokens"]

    print(f"\n{BOLD}{CYAN}╭──────────────────────────────────────────────────────────────╮{RESET}")
    print(f"{BOLD}{CYAN}│                   CONTEXT LENS TELEMETRY                     │{RESET}")
    print(f"{BOLD}{CYAN}╰──────────────────────────────────────────────────────────────╯{RESET}")
    print(f"{DIM}Conversation ID:{RESET} {conv_id}")
    print(f"{DIM}Log Source:{RESET}      {data['transcript_path']}")

    # 1. Context Window Loading
    print(f"\n{BOLD}Context Window Loading (Latest Turn):{RESET}")
    ctx_bar = format_bar(hit_rate, length=30, color=GREEN)
    print(f"  {BOLD}{ctx:,}{RESET} total prompt tokens")
    print(f"  [{ctx_bar}] {BOLD}{GREEN}{hit_rate:.1f}% cached{RESET}")
    print(f"  ├─ {GREEN}Prompt Cache Hit (Cached Prefix):{RESET}  {cached:,} tokens")
    print(f"  ├─ {YELLOW}Fresh Turn Input (Uncached):{RESET}       {inp:,} tokens")
    print(f"  └─ {BLUE}Turn Generation (Output/CoT):{RESET}      {out:,} tokens")

    # 2. Plan Quota Telemetry (% of Plan Used per Token)
    quotas = data.get("plan_quota_metrics")
    if quotas:
        print(f"\n{BOLD}Subscription Plan Quota Telemetry (% Used per Token):{RESET}")
        print(f"  {DIM}Cumulative Session Billed Tokens:{RESET} {BOLD}{cum_billed:,}{RESET} tokens")
        for q in quotas:
            used = q["used_pct"]
            rem = q["remaining_pct"]
            q_bar = format_bar(used, length=18, color=RED if used > 80 else (YELLOW if used > 40 else GREEN))
            family_lbl = f"{q['family']} ({q['limit'].replace(' Remaining', '')})"
            print(f"\n  • {BOLD}{family_lbl}:{RESET}")
            print(f"    Quota Used : {BOLD}{used}%{RESET} [{q_bar}] {DIM}({rem}% remaining, resets {q['reset_time']}){RESET}")
            if used > 0 and q["pct_per_1k_tokens"] > 0:
                print(f"    Burn Rate  : {BOLD}{YELLOW}{q['pct_per_1k_tokens']:.5f}%{RESET} of plan per 1k tokens {DIM}({q['pct_per_token']:.8f}% / token){RESET}")
                print(f"    Capacity   : ~{BOLD}{q['tokens_per_1pct']:,}{RESET} tokens per 1% quota")

    # 3. Implementation Plan Efficiency (if active plan detected)
    impl = data.get("implementation_plan_metrics")
    if impl:
        prog = impl["progress_pct"]
        prog_bar = format_bar(prog, length=20, color=GREEN)
        print(f"\n{BOLD}Implementation Plan Progress Efficiency:{RESET}")
        print(f"  Plan File  : {DIM}{impl['plan_file']}{RESET}")
        print(f"  Tasks Done : {BOLD}{impl['completed_tasks']}/{impl['total_tasks']}{RESET} [{prog_bar}] {BOLD}{GREEN}{prog:.1f}%{RESET}")
        print(f"  Efficiency : {BOLD}{CYAN}{impl['plan_pct_completed_per_1k_tokens']:.3f}%{RESET} of plan completed per 1k tokens")
        print(f"  Pace       : ~{BOLD}{impl['tokens_per_1pct_progress']:,}{RESET} tokens per 1% plan progress")
        if prog < 100:
            print(f"  Est. Left  : ~{BOLD}{impl['est_tokens_to_finish']:,}{RESET} tokens to finish remaining tasks")

    # 4. Composition Breakdown
    print(f"\n{BOLD}Prompt Composition Breakdown:{RESET}")
    print(f"  • {MAGENTA}Base System & Tool Schemas Overhead:{RESET} ~{baseline:,} tokens {DIM}(initial prompt overhead){RESET}")
    print(f"  • {BLUE}User Prompts Content:{RESET}                 ~{user_tok:,} tokens {DIM}({data['total_user_messages']} messages, {data['total_user_chars']:,} chars){RESET}")
    print(f"  • {YELLOW}Accumulated Tool Outputs:{RESET}             ~{tool_tok:,} tokens {DIM}({data['total_tool_output_chars']:,} chars across {len(data['tool_results'])} calls){RESET}")

    # 5. Tool categories
    cat_totals = data["tool_category_totals"]
    if cat_totals:
        print(f"\n{BOLD}Tool Output Usage by Type:{RESET}")
        sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)
        max_chars = sorted_cats[0][1] if sorted_cats else 1
        for tool, chars in sorted_cats:
            tok = chars // 4
            pct_bar = format_bar(chars / max_chars * 100, length=14, color=YELLOW)
            print(f"  {tool:<20} : {BOLD}{tok:>6,}{RESET} tokens [{pct_bar}] {DIM}({chars:,} chars){RESET}")

    # 6. Top Context Hogs
    tool_results = sorted(data["tool_results"], key=lambda x: x["chars"], reverse=True)
    if tool_results:
        print(f"\n{BOLD}Top Context Consumers (Context Hogs):{RESET}")
        for idx, item in enumerate(tool_results[:top_hogs], 1):
            t_name = item["tool"]
            t_target = item["target"]
            if len(t_target) > 50:
                t_target = t_target[:47] + "..."
            t_tok = item["est_tokens"]
            t_chars = item["chars"]
            step_info = f"Step {item['result_step']}"
            print(f"  {idx}. {BOLD}{t_name:<14}{RESET} {DIM}[{step_info:>7}]{RESET} ~{t_tok:,} tokens {DIM}({t_chars:,} chars){RESET}")
            if t_target:
                print(f"     {DIM}↳ {t_target}{RESET}")

    # 7. Timeline if requested
    if show_timeline and len(data["turns_timeline"]) > 1:
        print(f"\n{BOLD}Turn-by-Turn Context Progression:{RESET}")
        for t in data["turns_timeline"]:
            sidx = t["step_index"]
            tot = t["total_context"]
            c = t["cached_tokens"]
            print(f"  Turn @ Step {sidx:<3} : {tot:>6,} tokens {DIM}({c:>6,} cached){RESET}")

    print(f"\n{CYAN}{'─' * 64}{RESET}\n")


def main():
    parser = argparse.ArgumentParser(description="Antigravity Context & Token Analyzer")
    parser.add_argument("-p", "--path", help="Path to transcript.jsonl or transcript_full.jsonl")
    parser.add_argument("--plan", help="Explicit path to an implementation plan/tasks markdown file")
    parser.add_argument("-j", "--json", action="store_true", help="Output raw JSON data")
    parser.add_argument("-H", "--hogs", type=int, default=5, help="Number of context hogs to show (default: 5)")
    parser.add_argument("-t", "--timeline", action="store_true", help="Show turn-by-turn context growth")

    args = parser.parse_args()

    try:
        target_path = find_latest_transcript(args.path)
        if not target_path:
            print(f"{RED}Error: No active Antigravity session transcript found.{RESET}", file=sys.stderr)
            sys.exit(1)

        steps, resolved_path = parse_transcript(target_path)
        data = analyze_steps(steps, resolved_path, explicit_plan_path=args.plan)

        if not data:
            print(f"{RED}Error: Unable to parse planner steps from transcript.{RESET}", file=sys.stderr)
            sys.exit(1)

        if args.json:
            print(json.dumps(data, indent=2))
        else:
            print_cli_report(data, top_hogs=args.hogs, show_timeline=args.timeline)

    except Exception as e:
        print(f"{RED}Error analyzing session context: {e}{RESET}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
