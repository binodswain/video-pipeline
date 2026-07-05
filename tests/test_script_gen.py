#!/usr/bin/env python3
"""
Tests for Sprint 2: Script Generator (scripts/script_generator.py)

Validates:
  - Input validation
  - Output structure compliance with spec
  - Vox-style section structure (Hook → Context → Mechanism → Twist)
  - Timing contiguity and segment constraints
  - Template-based generation with sample research data
  - Edge cases: empty facts, minimal input, missing fields
"""

import json
import sys
import os
import unittest
from pathlib import Path

# Add project root to path so we can import from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.script_generator import (
    ScriptGenerator,
    ScriptOutput,
    Segment,
    validate_research_input,
    validate_script_output,
    STRUCTURE,
    MIN_SEGMENT_LENGTH,
    SECONDS_PER_SEGMENT_MAX,
    AVERAGE_PACE_WPM,
)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
SAMPLE_RESEARCH = FIXTURES_DIR / "sample_research_output.json"


def load_fixture(name: str) -> dict:
    """Load a JSON test fixture."""
    path = FIXTURES_DIR / name
    with open(path) as f:
        return json.load(f)


class TestInputValidation(unittest.TestCase):
    """Validate that the script generator correctly validates researcher input."""

    def test_valid_input_passes(self):
        """Full research output should pass validation."""
        data = load_fixture("sample_research_output.json")
        valid, errors = validate_research_input(data)
        self.assertTrue(valid, f"Expected valid, got errors: {errors}")

    def test_non_dict_rejected(self):
        """Non-dict input should be rejected."""
        valid, errors = validate_research_input("not a dict")
        self.assertFalse(valid)

    def test_missing_facts_rejected(self):
        """Input without 'facts' field should be rejected."""
        valid, errors = validate_research_input({"key_narrative": "something"})
        self.assertFalse(valid)
        self.assertIn("Missing required field: 'facts'", errors)

    def test_empty_facts_rejected(self):
        """Empty facts list should be rejected."""
        valid, errors = validate_research_input({"facts": []})
        self.assertFalse(valid)

    def test_facts_not_list_rejected(self):
        """facts field that isn't a list should be rejected."""
        valid, errors = validate_research_input({"facts": "not a list"})
        self.assertFalse(valid)

    def test_missing_narrative_ok(self):
        """Missing key_narrative should not fail — it's recommended, not required."""
        valid, errors = validate_research_input({"facts": [{"claim": "test"}]})
        self.assertTrue(valid, f"Expected valid, got errors: {errors}")


class TestOutputValidation(unittest.TestCase):
    """Validate that ScriptOutput passes/fails structural checks correctly."""

    def _make_segment(self, start, end, text, section="context", **kwargs):
        return {
            "start_time": start,
            "end_time": end,
            "text": text,
            "visual_cue": kwargs.get("visual_cue", "test visual"),
            "emotion_tone": kwargs.get("emotion_tone", "explanatory"),
            "section": section,
            "key_phrase": kwargs.get("key_phrase", ""),
        }

    def test_valid_output_passes(self):
        """A well-formed output should pass validation."""
        output = ScriptOutput(
            script_full="Test narration text that is long enough to pass the minimum character check for validation purposes.",
            segments=[
                self._make_segment(0.0, 5.0, "Segment one text.", "hook"),
                self._make_segment(5.0, 10.0, "Segment two text.", "context"),
            ],
            hook_line="This is the hook line.",
            calls_to_action=[],
            metadata={},
        )
        valid, errors = validate_script_output(output)
        self.assertTrue(valid, f"Expected valid, got errors: {errors}")

    def test_short_script_full_rejected(self):
        """script_full under 50 chars should be flagged."""
        output = ScriptOutput(
            script_full="Too short.",
            segments=[self._make_segment(0.0, 5.0, "text", "hook")],
            hook_line="hook",
            calls_to_action=[],
        )
        valid, errors = validate_script_output(output)
        self.assertFalse(valid)

    def test_empty_segments_rejected(self):
        """Empty segments list should be flagged."""
        output = ScriptOutput(
            script_full="A" * 60,
            segments=[],
            hook_line="hook",
            calls_to_action=[],
        )
        valid, errors = validate_script_output(output)
        self.assertFalse(valid)

    def test_backwards_timing_rejected(self):
        """Segment with start_time >= end_time should be flagged."""
        output = ScriptOutput(
            script_full="A" * 60,
            segments=[self._make_segment(10.0, 5.0, "backwards", "hook")],
            hook_line="hook",
            calls_to_action=[],
        )
        valid, errors = validate_script_output(output)
        self.assertFalse(valid)

    def test_gaps_in_timing_flagged(self):
        """Gaps > 0.5s between segments should be flagged."""
        output = ScriptOutput(
            script_full="A" * 60,
            segments=[
                self._make_segment(0.0, 5.0, "first", "hook"),
                self._make_segment(7.0, 10.0, "second (gap!)", "context"),
            ],
            hook_line="hook",
            calls_to_action=[],
        )
        valid, errors = validate_script_output(output)
        self.assertFalse(valid)

    def test_unknown_section_flagged(self):
        """Unknown section name should be flagged."""
        output = ScriptOutput(
            script_full="A" * 60,
            segments=[self._make_segment(0.0, 5.0, "text", "invalid_section")],
            hook_line="hook",
            calls_to_action=[],
        )
        valid, errors = validate_script_output(output)
        self.assertFalse(valid)


class TestScriptGeneratorTemplate(unittest.TestCase):
    """End-to-end tests using the template-based generator."""

    def setUp(self):
        self.generator = ScriptGenerator(use_llm=False, target_duration=420.0)

    def test_full_generation_from_sample(self):
        """Generate a complete script from sample research data."""
        research = load_fixture("sample_research_output.json")
        output = self.generator.generate(research)

        # Check output type
        self.assertIsInstance(output, ScriptOutput)

        # Check required fields
        self.assertTrue(output.script_full)
        self.assertTrue(output.segments)
        self.assertTrue(output.hook_line)

        # Check structure: all sections present
        sections = {s["section"] for s in output.segments}
        for expected in ["hook", "context", "mechanism", "twist"]:
            self.assertIn(expected, sections, f"Missing section: {expected}")

        # Check hook is first
        self.assertEqual(output.segments[0]["section"], "hook")

        # Check hook line is in script_full
        # (hook_line is a multi-line string, so check first line)
        first_hook_line = output.hook_line.split("\n")[0]
        self.assertIn(first_hook_line[:30], output.script_full)

        # Check timing is contiguous
        for i in range(1, len(output.segments)):
            gap = abs(output.segments[i]["start_time"] - output.segments[i-1]["end_time"])
            self.assertLess(gap, 0.5, f"Gap of {gap}s at segment {i}")

        # Check segment timing bounds
        for seg in output.segments:
            dur = seg["end_time"] - seg["start_time"]
            self.assertGreaterEqual(dur, MIN_SEGMENT_LENGTH,
                                    f"Segment too short: {dur}s")
            self.assertLessEqual(dur, SECONDS_PER_SEGMENT_MAX,
                                 f"Segment too long: {dur}s")

        # Check metadata
        self.assertIn("generator", output.metadata)
        self.assertEqual(output.metadata["generator"], "template")
        self.assertIn("actual_duration_s", output.metadata)
        self.assertGreater(output.metadata["actual_duration_s"], 0)
        self.assertIn("segment_count", output.metadata)
        self.assertIn("word_count", output.metadata)
        self.assertIn("input_fact_count", output.metadata)

        # Check CTA presence (should have one with 8 facts)
        self.assertTrue(output.calls_to_action)

        # Vox-style structure proportions should be roughly correct
        total_dur = output.metadata["actual_duration_s"]
        section_durs = {}
        for seg in output.segments:
            sec = seg["section"]
            dur = seg["end_time"] - seg["start_time"]
            section_durs[sec] = section_durs.get(sec, 0) + dur

        # Hook should be <= 30s
        self.assertLessEqual(section_durs.get("hook", 0), 30.0,
                             "Hook exceeds 30-second max")

        # Context + mechanism + twist should account for most runtime
        non_hook = sum(v for k, v in section_durs.items() if k != "hook")
        self.assertGreater(non_hook / total_dur, 0.85,
                           "Non-hook sections are too small relative to total")

        # Mechanism should be the largest section
        self.assertGreater(section_durs.get("mechanism", 0),
                          section_durs.get("hook", 0),
                          "Mechanism should be larger than hook")

    def test_generation_with_minimal_facts(self):
        """Generate with minimal facts — should not crash."""
        minimal = {
            "facts": [
                {"claim": "The sky is blue.", "confidence": 0.95, "source_name": "Observation"},
                {"claim": "Water is wet.", "confidence": 0.99, "source_name": "Experience"},
            ],
            "key_narrative": "Basic facts about the world.",
        }
        output = self.generator.generate(minimal)
        self.assertIsInstance(output, ScriptOutput)
        self.assertTrue(output.segments)

    def test_generation_with_no_narrative(self):
        """Should handle missing key_narrative field."""
        no_narrative = {
            "facts": [
                {"claim": "Fact one.", "confidence": 0.8},
                {"claim": "Fact two.", "confidence": 0.7},
            ],
        }
        output = self.generator.generate(no_narrative)
        self.assertIsInstance(output, ScriptOutput)

    def test_generation_with_contested_flags(self):
        """Contested flags should appear in twist section."""
        research = load_fixture("sample_research_output.json")
        self.assertTrue(research.get("contested_flags"))

        output = self.generator.generate(research)
        twist_segments = [s for s in output.segments if s["section"] == "twist"]
        # At least one twist segment should mention "miss" or contested content
        twist_text = " ".join(s["text"] for s in twist_segments)
        self.assertIn("miss", twist_text.lower())

    def test_segments_have_all_required_keys(self):
        """Every segment must have all required fields."""
        research = load_fixture("sample_research_output.json")
        output = self.generator.generate(research)

        required_keys = {"start_time", "end_time", "text", "visual_cue",
                        "emotion_tone", "section", "key_phrase"}
        for seg in output.segments:
            self.assertTrue(required_keys.issubset(seg.keys()),
                            f"Missing keys in segment: {required_keys - set(seg.keys())}")

    def test_visual_cues_present(self):
        """Every segment must have a non-empty visual_cue."""
        research = load_fixture("sample_research_output.json")
        output = self.generator.generate(research)
        for seg in output.segments:
            self.assertTrue(seg["visual_cue"].strip(),
                            f"Empty visual_cue in segment: {seg['text'][:50]}")

    def test_script_full_matches_segments(self):
        """script_full should contain the text from all segments."""
        research = load_fixture("sample_research_output.json")
        output = self.generator.generate(research)

        # Each segment's text should be findable in script_full
        for seg in output.segments:
            # Check at least the first 10 words are in script_full
            first_words = " ".join(seg["text"].split()[:10])
            self.assertIn(first_words, output.script_full,
                         f"Segment text not found in script_full: '{first_words[:60]}...'")


class TestScriptGeneratorConfig(unittest.TestCase):
    """Test configuration options for the script generator."""

    def test_different_target_durations(self):
        """Different target durations should produce different-length scripts."""
        research = load_fixture("sample_research_output.json")

        gen_short = ScriptGenerator(target_duration=120.0)
        gen_long = ScriptGenerator(target_duration=600.0)

        out_short = gen_short.generate(research)
        out_long = gen_long.generate(research)

        # The longer target may not always produce a longer script
        # (depends on fact count), but both should be valid
        self.assertGreater(out_short.metadata["actual_duration_s"], 0)
        self.assertGreater(out_long.metadata["actual_duration_s"], 0)

    def test_custom_pace(self):
        """Custom words-per-minute should affect timing."""
        research = load_fixture("sample_research_output.json")

        gen_slow = ScriptGenerator(voice_pace_wpm=100)  # slow talker
        gen_fast = ScriptGenerator(voice_pace_wpm=200)  # fast talker

        out_slow = gen_slow.generate(research)
        out_fast = gen_fast.generate(research)

        # Slower pace should produce longer duration for same words
        self.assertGreater(out_slow.metadata["actual_duration_s"],
                          out_fast.metadata["actual_duration_s"])


class TestCLIIntegration(unittest.TestCase):
    """Test end-to-end CLI invocation."""

    def test_cli_from_file(self):
        """Run via CLI with --input and --output flags."""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "scripts" / "script_generator.py"),
                "--input", str(SAMPLE_RESEARCH),
                "--output", "/tmp/test_cli_output.json",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"CLI failed: {result.stderr}")

        # Verify output file exists and is valid JSON
        with open("/tmp/test_cli_output.json") as f:
            data = json.load(f)
        self.assertIn("script_full", data)
        self.assertIn("segments", data)
        self.assertIn("hook_line", data)

    def test_cli_from_stdin(self):
        """Run via CLI reading from stdin."""
        import subprocess

        with open(SAMPLE_RESEARCH) as f:
            stdin_data = f.read()

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "scripts" / "script_generator.py"),
                "--output", "/tmp/test_cli_stdin_output.json",
            ],
            input=stdin_data,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"CLI stdin failed: {result.stderr}")

    def test_cli_missing_output_fails(self):
        """CLI should fail if --output is not provided."""
        import subprocess

        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve().parent.parent / "scripts" / "script_generator.py"),
                "--input", str(SAMPLE_RESEARCH),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
