#!/usr/bin/env python3
"""Tests for analyze_context.py.

Run with:  python3 -m unittest discover -s tests -v
"""

import importlib.util
import json
import os
import tempfile
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_MODULE_PATH = os.path.join(_HERE, "..", "skills", "context-lens", "scripts", "analyze_context.py")
_spec = importlib.util.spec_from_file_location("analyze_context", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None, "cannot load analyze_context.py"
ac = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ac)

# Tests must be hermetic: never shell out to a real `agy` binary, and never
# read/write the user's real baseline state directory.
ac.fetch_quota_telemetry = lambda: None
ac.STATE_DIR = tempfile.mkdtemp(prefix="context-lens-state-")


def write_transcript(steps, name="transcript_full.jsonl"):
    """Write steps to a temp transcript laid out with a `brain/<id>/` segment."""
    root = tempfile.mkdtemp(prefix="context-lens-test-")
    conv = os.path.join(root, "brain", "conv-test", ".system_generated", "logs")
    os.makedirs(conv, exist_ok=True)
    path = os.path.join(conv, name)
    with open(path, "w", encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")
    return path


def planner(**kw):
    base = {"type": "PLANNER_RESPONSE", "step_index": kw.pop("step_index", 1),
            "source": "MODEL", "status": "OK", "tool_calls": kw.pop("tool_calls", [])}
    base.update(kw)
    return base


class TestMetering(unittest.TestCase):
    def test_unmetered_session_is_flagged_not_zeroed(self):
        # A session where no planner turn carries token fields must NOT claim
        # 0 tokens -- it must report metering as unavailable.
        steps = [
            {"type": "USER_INPUT", "step_index": 1, "content": "hello"},
            planner(step_index=2, content="hi there"),
            {"type": "RUN_COMMAND", "step_index": 3, "content": "x" * 400},
            planner(step_index=4, content="done"),
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        self.assertFalse(data["token_metering_available"])
        self.assertIsNone(data["current_context_tokens"])
        self.assertIsNone(data["cache_hit_rate_pct"])
        self.assertIsNone(data["system_baseline_tokens"])

    def test_latest_metered_turn_skips_unmetered_tail(self):
        # The most recent token-bearing turn wins, even if later planner turns
        # omit token fields (common in real transcripts).
        steps = [
            {"type": "USER_INPUT", "step_index": 1, "content": "hi"},
            planner(step_index=2, input_tokens=1000, cache_read_tokens=9000, output_tokens=100),
            planner(step_index=3, content="no metering here"),
            planner(step_index=4, content="still none"),
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        self.assertTrue(data["token_metering_available"])
        self.assertEqual(data["current_context_tokens"], 10000)
        self.assertEqual(data["latest_metered_step"], 2)
        self.assertEqual(data["unmetered_planner_turns"], 2)
        self.assertAlmostEqual(data["cache_hit_rate_pct"], 90.0, places=2)


class TestToolAttribution(unittest.TestCase):
    def test_view_file_result_pairs_with_view_file_call(self):
        steps = [
            planner(step_index=1, tool_calls=[{"name": "view_file", "args": {}}]),
            {"type": "VIEW_FILE", "step_index": 2,
             "content": "File Path: `file:///home/n100/run_all_evals.py`\nTotal Lines: 96\n"},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        hogs = [s for s in data["tool_results"] if s["type"] == "VIEW_FILE"]
        self.assertEqual(len(hogs), 1)
        self.assertEqual(hogs[0]["requested_call"], "view_file")
        self.assertEqual(hogs[0]["match_quality"], "typed")
        self.assertEqual(hogs[0]["target"], "/home/n100/run_all_evals.py")

    def test_assistant_narration_does_not_steal_the_call(self):
        # Regression: a PLANNER_RESPONSE with assistant text (not a tool result)
        # between a call and its real result used to desync the FIFO pairing and
        # credit the tool with the narration's length.
        steps = [
            planner(step_index=1, tool_calls=[{"name": "view_file", "args": {}}]),
            planner(step_index=2, content="I will now read the file."),
            {"type": "VIEW_FILE", "step_index": 3,
             "content": "File Path: `file:///home/n100/big.txt`\n" + "Z" * 20000},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        vf = [s for s in data["tool_results"] if s["type"] == "VIEW_FILE"][0]
        narration = [s for s in data["tool_results"]
                     if s["type"] == "PLANNER_RESPONSE" and s["target"]][0]
        self.assertEqual(vf["requested_call"], "view_file")
        self.assertGreater(vf["chars"], 20000)
        self.assertEqual(narration["category"], "assistant")
        self.assertNotEqual(narration.get("requested_call"), "view_file")

    def test_generic_is_a_notice_when_typed_results_exist(self):
        # In a transcript that serializes tool results as typed steps, a GENERIC
        # step is a task/background notice and must not steal the pending call.
        steps = [
            planner(step_index=1, tool_calls=[{"name": "view_file", "args": {}}]),
            {"type": "GENERIC", "step_index": 2,
             "content": "Tool is running as a background task with task id: t-1"},
            {"type": "VIEW_FILE", "step_index": 3,
             "content": "File Path: `file:///home/n100/x.py`\n"},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        self.assertEqual(data["tool_result_encoding"], "typed")
        gen = [s for s in data["tool_results"] if s["type"] == "GENERIC"][0]
        self.assertEqual(gen["label"], "task_notice")
        self.assertIsNone(gen["requested_call"])
        vf = [s for s in data["tool_results"] if s["type"] == "VIEW_FILE"][0]
        self.assertEqual(vf["requested_call"], "view_file")

    def test_generic_only_transcript_pairs_results_to_tool_calls(self):
        # Variant where every tool result serializes as GENERIC: results align
        # 1:1 with the requested calls, so they must get the real tool name and
        # a target from the call arguments (not a bare "task_notice").
        steps = [
            planner(step_index=1, tool_calls=[
                {"name": "view_file", "args": {"AbsolutePath": "/home/n100/run.py"}}]),
            {"type": "GENERIC", "step_index": 2,
             "content": "File Path: `file:///home/n100/run.py`\n" + "z" * 500},
            planner(step_index=3, tool_calls=[
                {"name": "run_command", "args": {"CommandLine": "pytest -q"}}]),
            {"type": "GENERIC", "step_index": 4,
             "content": "The command completed successfully.\nOutput:\nok"},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        self.assertEqual(data["tool_result_encoding"], "generic")
        by_step = {s["step_index"]: s for s in data["tool_results"] if s["type"] == "GENERIC"}
        self.assertEqual(by_step[2]["label"], "view_file")
        self.assertEqual(by_step[2]["match_quality"], "fifo")
        self.assertEqual(by_step[2]["target"], "/home/n100/run.py")
        self.assertEqual(by_step[4]["label"], "run_command")
        self.assertEqual(by_step[4]["target"], "pytest -q")

    def test_unpaired_generic_in_generic_transcript_is_task_notice(self):
        steps = [
            planner(step_index=1, content="no calls here"),
            {"type": "GENERIC", "step_index": 2, "content": "orphan notice"},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        gen = [s for s in data["tool_results"] if s["type"] == "GENERIC"][0]
        self.assertEqual(gen["label"], "task_notice")
        self.assertIsNone(gen["requested_call"])


class TestCompositionAndHogs(unittest.TestCase):
    def test_categories_and_hog_ranking(self):
        steps = [
            {"type": "USER_INPUT", "step_index": 1, "content": "u" * 40},
            planner(step_index=2, input_tokens=100, cache_read_tokens=0, output_tokens=10,
                    content="thinking"),
            {"type": "VIEW_FILE", "step_index": 3, "content": "v" * 400},
            {"type": "EPHEMERAL_MESSAGE", "step_index": 4, "content": "e" * 800},
            {"type": "CHECKPOINT", "step_index": 5, "content": "{{ CHECKPOINT 7 }}\n" + "c" * 200},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        self.assertEqual(data["composition"]["user_chars"], 40)
        self.assertEqual(data["composition"]["tool_chars"], 400)
        self.assertEqual(data["composition"]["injected_chars"], 800 + len("{{ CHECKPOINT 7 }}\n") + 200)
        hogs = sorted(data["tool_results"], key=lambda x: x["chars"], reverse=True)
        self.assertEqual(hogs[0]["type"], "EPHEMERAL_MESSAGE")
        self.assertEqual(hogs[0]["category"], "injected")

    def test_missing_path_raises(self):
        with self.assertRaises(FileNotFoundError):
            ac.find_latest_transcript("/no/such/file.jsonl")


class TestTargetExtraction(unittest.TestCase):
    def test_embedded_task_description_marker_does_not_hijack_target(self):
        # Regression: a payload that merely *contains* the literal
        # "Task Description:" (e.g. viewing this script's own source, or a
        # transcript dump) used to be matched mid-line and returned garbage.
        src = '    m = re.search(r"Task Description:\\s*(.+)", c)'
        self.assertEqual(
            ac.extract_target("VIEW_FILE", src,
                              {"AbsolutePath": "/home/n100/analyze_context.py"}),
            "/home/n100/analyze_context.py")
        # No args and no line-leading marker -> nothing, not garbage.
        self.assertEqual(ac.extract_target("GENERIC", src, {}), "")
        # A genuine background notice (line-leading marker) still works.
        self.assertEqual(
            ac.extract_target("GENERIC", "Created At: x\nTask Description: pytest -q\n", {}),
            "pytest -q")

    def test_generic_view_file_uses_file_path_from_payload(self):
        # A GENERIC-serialized view_file result should still parse "File Path:"
        # via the paired call's effective type.
        steps = [
            planner(step_index=1, tool_calls=[
                {"name": "view_file", "args": {"AbsolutePath": "/home/n100/other.py"}}]),
            {"type": "GENERIC", "step_index": 2,
             "content": "File Path: `file:///home/n100/real.py`\nTotal Lines: 3\n"},
        ]
        path = write_transcript(steps)
        data = ac.analyze_steps(*ac.parse_transcript(path))
        gen = [s for s in data["tool_results"] if s["type"] == "GENERIC"][0]
        self.assertEqual(gen["label"], "view_file")
        self.assertEqual(gen["target"], "/home/n100/real.py")


class TestQuotaBurnRate(unittest.TestCase):
    """The burn-rate metric must come from a measured delta, never from
    account-wide usage divided by one session's tokens."""

    def setUp(self):
        ac.STATE_DIR = tempfile.mkdtemp(prefix="context-lens-state-")
        self._orig = ac.fetch_quota_telemetry

    def tearDown(self):
        ac.fetch_quota_telemetry = self._orig

    @staticmethod
    def _quota(remaining_pct):
        return [{"family": "Gemini Models", "limit": "Weekly Limit",
                 "remaining_pct": remaining_pct, "used_pct": 100 - remaining_pct,
                 "reset_time": "soon"}]

    def test_first_run_records_baseline_then_measures_delta(self):
        steps = [planner(step_index=1, input_tokens=1000, cache_read_tokens=0, output_tokens=0)]

        # First run: no baseline yet -> no invented burn rate.
        ac.fetch_quota_telemetry = lambda: self._quota(90)
        first = ac.analyze_steps(*ac.parse_transcript(write_transcript(steps)))
        q0 = first["plan_quota_metrics"][0]
        self.assertIsNone(q0["measured_tokens_per_1pct"])
        ac.save_quota_baseline(first["conversation_id"], first["plan_quota_metrics"],
                               first["cumulative_billed_tokens"])

        # Second run: quota moved 90 -> 89 (1%) after 10,000 more billed tokens.
        steps2 = [planner(step_index=1, input_tokens=11000, cache_read_tokens=0, output_tokens=0)]
        ac.fetch_quota_telemetry = lambda: self._quota(89)
        second = ac.analyze_steps(*ac.parse_transcript(write_transcript(steps2)))
        q1 = second["plan_quota_metrics"][0]
        self.assertEqual(q1["measured_tokens_per_1pct"], 10000)


if __name__ == "__main__":
    unittest.main()
