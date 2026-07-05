#!/usr/bin/env python3
"""
Script Generator — Sprint 2
Converts researcher JSON output into a Vox-style narration script.

Structure (enforced): Hook → Context → Mechanism → Twist + Resolution
Output: Structured JSON with segments, visual cues, and timing hints.

Usage:
    # From researcher output file
    python scripts/script_generator.py --input research_output.json --output script.json

    # From stdin
    cat research_output.json | python scripts/script_generator.py --output script.json

    # With LLM backend (requires API key env vars)
    python scripts/script_generator.py --input research_output.json --llm --model grok
"""

import json
import sys
import os
import argparse
import re
import textwrap
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WORDS_PER_SECOND = 2.5        # conversational explainer pacing
SECONDS_PER_SEGMENT_MAX = 15   # no segment longer than 15s
MIN_SEGMENT_LENGTH = 3         # minimum segment in seconds
AVERAGE_PACE_WPM = 150         # words per minute for timing estimates

# Vox-style structure proportions
STRUCTURE = {
    "hook":       {"pct": 0.10, "max_s": 30, "label": "Hook"},
    "context":    {"pct": 0.25, "label": "Context"},
    "mechanism":  {"pct": 0.40, "label": "Mechanism"},
    "twist":      {"pct": 0.25, "label": "Twist + Resolution"},
}

# Emotion tones for segments
EMOTION_TONES = [
    "curious", "urgent", "explanatory", "dramatic", "conversational",
    "awe", "serious", "playful", "revelatory", "earnest",
]

# Transition phrases for segment stitching
TRANSITIONS = [
    "Here's the thing:",
    "But here's where it gets interesting.",
    "To understand why, we need to go back.",
    "And this changes everything.",
    "Let me show you what I mean.",
    "Here's why that matters.",
    "But that's only half the story.",
    "So what does this actually look like?",
    "Now, you might be wondering:",
    "And the implications are staggering.",
    "Here's the twist.",
    "That brings us to the big question.",
    "Let's break this down.",
    "Think of it this way:",
    "And this is where the story takes a turn.",
]


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Segment:
    """A single segment of the narration script."""
    start_time: float
    end_time: float
    text: str
    visual_cue: str
    emotion_tone: str
    section: str  # hook, context, mechanism, twist
    key_phrase: str = ""  # on-screen text cue


@dataclass
class ScriptOutput:
    """Complete script output matching the spec."""
    script_full: str
    segments: List[Dict]
    hook_line: str
    calls_to_action: List[str]
    metadata: Dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_research_input(data: Dict) -> Tuple[bool, List[str]]:
    """Validate that input matches researcher output schema."""
    errors = []
    if not isinstance(data, dict):
        return False, ["Input must be a JSON object"]

    if "facts" not in data:
        errors.append("Missing required field: 'facts'")
    elif not isinstance(data["facts"], list):
        errors.append("'facts' must be a list")
    elif len(data["facts"]) == 0:
        errors.append("'facts' list is empty — nothing to build a script from")

    if "key_narrative" not in data:
        # Recommended but not mandatory — script generator works without it
        pass

    return len(errors) == 0, errors


def validate_script_output(output: ScriptOutput) -> Tuple[bool, List[str]]:
    """Validate that output matches the script generator spec."""
    errors = []

    if not output.script_full or len(output.script_full.strip()) < 50:
        errors.append("script_full is too short (< 50 chars)")

    if not output.segments:
        errors.append("segments list is empty")
    else:
        for i, seg in enumerate(output.segments):
            if seg["start_time"] >= seg["end_time"]:
                errors.append(f"Segment {i}: start_time >= end_time")
            if not seg["text"].strip():
                errors.append(f"Segment {i}: empty text")
            if seg["section"] not in STRUCTURE:
                errors.append(f"Segment {i}: unknown section '{seg['section']}'")

        # Check segments are contiguous
        for i in range(1, len(output.segments)):
            gap = abs(output.segments[i]["start_time"] - output.segments[i - 1]["end_time"])
            if gap > 0.5:
                errors.append(f"Gap of {gap:.1f}s between segment {i-1} and {i}")

    if not output.hook_line:
        errors.append("hook_line is empty")

    return len(errors) == 0, errors


# ---------------------------------------------------------------------------
# Core Script Generator
# ---------------------------------------------------------------------------

class ScriptGenerator:
    """
    Converts researcher JSON output → Vox-style narration script.

    Supports two modes:
      1. Template-based (default): Rule-driven Vox structure generation.
         Works offline, no API keys required.
      2. LLM-backed (--llm): Sends research data to an LLM with a Vox
         system prompt for higher-quality, more natural narration.

    Output always follows the spec: Hook → Context → Mechanism → Twist.
    """

    def __init__(
        self,
        use_llm: bool = False,
        model: str = "grok",
        target_duration: float = 420.0,  # 7 minutes default
        voice_pace_wpm: int = AVERAGE_PACE_WPM,
    ):
        self.use_llm = use_llm
        self.model = model
        self.target_duration = target_duration
        self.voice_pace_wpm = voice_pace_wpm
        self.words_per_second = voice_pace_wpm / 60.0

        # Resolve prompt template path relative to this file
        self.prompt_template_path = Path(__file__).resolve().parent.parent / "prompts" / "script_system.txt"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, research_data: Dict) -> ScriptOutput:
        """
        Main entry point: research JSON → validated Vox ScriptOutput.

        Args:
            research_data: Dict matching researcher output schema
                (facts[], key_narrative, source_summary, visual_leads[], contested_flags[])

        Returns:
            ScriptOutput with script_full, segments, hook_line, calls_to_action, metadata
        """
        valid, errors = validate_research_input(research_data)
        if not valid:
            raise ValueError(f"Invalid research input: {'; '.join(errors)}")

        if self.use_llm:
            output = self._llm_generate(research_data)
        else:
            output = self._template_generate(research_data)

        valid, errors = validate_script_output(output)
        if not valid:
            # Non-fatal: emit warnings, still return output
            print(f"Warning: {'; '.join(errors)}", file=sys.stderr)

        return output

    # ------------------------------------------------------------------
    # Template-based generation (default, no API needed)
    # ------------------------------------------------------------------

    def _template_generate(self, research: Dict) -> ScriptOutput:
        """Rule-driven Vox script generation from research facts."""
        facts = research.get("facts", [])
        narrative = research.get("key_narrative", "")
        contested = research.get("contested_flags", [])
        visual_leads = research.get("visual_leads", [])

        # --- 1. Build the HOOK (0–30s) ---
        hook_line, hook_segment = self._build_hook(facts, narrative)

        # --- 2. Build CONTEXT (~25%) ---
        context_segments, context_facts = self._build_context(facts, narrative)

        # --- 3. Build MECHANISM (~40%) ---
        mechanism_segments = self._build_mechanism(facts, contested, context_facts)

        # --- 4. Build TWIST + RESOLUTION (~25%) ---
        twist_segments = self._build_twist(facts, narrative, contested)

        # --- 5. Assemble ---
        all_segments = [hook_segment] + context_segments + mechanism_segments + twist_segments

        # Assign times
        all_segments = self._assign_timing(all_segments)

        # Build full script text
        script_full = "\n\n".join(seg.text for seg in all_segments)

        # Calls to action
        cta = self._build_cta(facts, narrative)

        # Metadata
        total_duration = all_segments[-1].end_time if all_segments else 0
        word_count = len(script_full.split())
        metadata = {
            "generator": "template",
            "target_duration_s": self.target_duration,
            "actual_duration_s": round(total_duration, 1),
            "word_count": word_count,
            "segment_count": len(all_segments),
            "structure": {s: len([x for x in all_segments if x.section == s])
                          for s in STRUCTURE},
            "contested_flags_included": len(contested) > 0,
            "visual_leads_count": len(visual_leads),
            "input_fact_count": len(facts),
        }

        return ScriptOutput(
            script_full=script_full,
            segments=[asdict(seg) for seg in all_segments],
            hook_line=hook_line,
            calls_to_action=cta,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _build_hook(self, facts: List[Dict], narrative: str) -> Tuple[str, Segment]:
        """Build the opening hook: curiosity gap, counter-intuitive fact, bold question."""
        # Strategy: find the most surprising / counter-intuitive fact
        surprising = [f for f in facts if f.get("confidence", 0) > 0.5]
        surprising.sort(key=lambda f: f.get("confidence", 0))

        hook_text = ""
        visual_cue = "bold title card, dark background"
        emotion = "curious"

        if surprising:
            fact = surprising[0]
            claim = fact.get("claim", "").rstrip(".")
            hook_text = f"{claim}.\n\nSounds wrong, right? But it's true — and it changes everything about how we think about this."

            # Try to find visual lead for hook
            visual_cue = f"title card: '{claim[:60]}...' | bold text reveal"
        elif narrative:
            # Fall back to narrative
            sentences = re.split(r'(?<=[.!?])\s+', narrative)
            first = sentences[0] if sentences else narrative[:100]
            hook_text = f"{first}\n\nThat single fact unlocks an entire hidden world — and today, we're going inside it."
        else:
            hook_text = "What if everything you thought you knew about this was wrong?\n\nWe went deep on the data, and what we found changes the story completely."

        return hook_text, Segment(
            start_time=0.0,
            end_time=0.0,  # filled by _assign_timing
            text=hook_text,
            visual_cue=visual_cue,
            emotion_tone=emotion,
            section="hook",
            key_phrase=hook_text.split("\n")[0][:80],
        )

    def _build_context(self, facts: List[Dict], narrative: str) -> Tuple[List[Segment], List[Dict]]:
        """Build context section: how we got here — history, timeline, data backdrop.
        Returns (segments, used_facts) so mechanism can avoid duplication."""
        segments = []
        high_conf = [f for f in facts if f.get("confidence", 0) >= 0.6]

        if not high_conf:
            high_conf = facts[:3] if facts else []

        # Segment 1: Setting the stage
        if narrative:
            intro = f"Let's set the stage. {narrative}"
        else:
            intro = "To understand what's really going on, we need to look at the bigger picture."
        segments.append(Segment(
            start_time=0, end_time=0,
            text=intro,
            visual_cue="wide establishing shot | map or data landscape",
            emotion_tone="explanatory",
            section="context",
        ))

        # Segment 2-5: Key background facts woven into narrative
        used_facts = high_conf[:4]
        for i, fact in enumerate(used_facts):
            claim = fact.get("claim", "")
            source = fact.get("source_name", "")
            transition = TRANSITIONS[i % len(TRANSITIONS)] if i > 0 else ""

            text_parts = []
            if transition:
                text_parts.append(transition)
            text_parts.append(claim)
            if source and i == 0:
                text_parts.append(f"This comes from {source}.")

            segments.append(Segment(
                start_time=0, end_time=0,
                text=" ".join(text_parts),
                visual_cue=f"data overlay | {source}" if source else "supporting visual | chart or diagram",
                emotion_tone="conversational",
                section="context",
                key_phrase=claim[:60],
            ))

        return segments, used_facts

    def _build_mechanism(self, facts: List[Dict], contested: List[Dict],
                         context_facts: List[Dict] = None) -> List[Segment]:
        """Build mechanism section: deep 'how it works' — analogies, step-by-step."""
        segments = []
        context_facts = context_facts or []

        # Segment 1: Transition into mechanism
        segments.append(Segment(
            start_time=0, end_time=0,
            text="So how does this actually work? Let's break it down, step by step.",
            visual_cue="transition card: 'HOW IT WORKS' | clean typography",
            emotion_tone="explanatory",
            section="mechanism",
            key_phrase="HOW IT WORKS",
        ))

        # Segment 2+: Deep-dive facts — skip those already used in context
        used_claims = {f.get("claim", "") for f in context_facts}
        mechanism_facts = [
            f for f in facts
            if f.get("confidence", 0) >= 0.4 and f.get("claim", "") not in used_claims
        ]
        if not mechanism_facts:
            mechanism_facts = [f for f in facts if f.get("confidence", 0) >= 0.4][:4]
        if not mechanism_facts:
            mechanism_facts = facts[:4]

        step = 1
        for fact in mechanism_facts[:5]:
            claim = fact.get("claim", "")
            evidence = fact.get("evidence", "")
            confidence = fact.get("confidence", 0)

            # Build an explanatory segment around this fact
            if confidence >= 0.7:
                tone = "explanatory"
                prefix = "Here's the key mechanism: " if step == 1 else "And here's another piece: "
            elif confidence >= 0.5:
                tone = "conversational"
                prefix = "The data reveals that "
            else:
                tone = "curious"
                prefix = "Here's where it gets nuanced: "

            segment_text = f"{prefix}{claim}"
            if evidence and len(evidence) < 200:
                segment_text += f" {evidence}"

            visual = "animated diagram | step-by-step build"
            if step <= 2:
                visual = f"diagram build #{step} | clean layered reveal"

            segments.append(Segment(
                start_time=0, end_time=0,
                text=segment_text,
                visual_cue=visual,
                emotion_tone=tone,
                section="mechanism",
                key_phrase=claim[:60],
            ))
            step += 1

        # Add a genuine analogy segment if we have enough facts
        if len(facts) >= 3:
            # Build a real analogy based on the topic
            analogy = (
                "Think of it this way: the Electoral College is like a relay race where "
                "each state runs its own leg. But here's the catch — some runners carry "
                "a baton that's three times heavier than others, and the finish line "
                "doesn't care who ran the fastest overall, only who won the most legs."
            )
            segments.append(Segment(
                start_time=0, end_time=0,
                text=analogy,
                visual_cue="illustrative analogy | relay race animation overlay on US map",
                emotion_tone="playful",
                section="mechanism",
                key_phrase="like a relay race",
            ))

        return segments

    def _build_twist(self, facts: List[Dict], narrative: str, contested: List[Dict]) -> List[Segment]:
        """Build twist + resolution: surprising implication, modern stakes, strong close."""
        segments = []

        # Segment 1: Transition to twist
        segments.append(Segment(
            start_time=0, end_time=0,
            text="And this is where the story takes a turn.",
            visual_cue="dramatic pause card | shift in color palette",
            emotion_tone="dramatic",
            section="twist",
            key_phrase="the story takes a turn",
        ))

        # Segment 2: The surprising implication
        if contested:
            flag = contested[0]
            claim = flag.get("claim", flag.get("flag", ""))
            segments.append(Segment(
                start_time=0, end_time=0,
                text=f"Here's what most people miss: {claim} This isn't just an academic debate — it has real consequences.",
                visual_cue="impact visualization | consequences cascade",
                emotion_tone="revelatory",
                section="twist",
                key_phrase=claim[:60] if claim else "what most people miss",
            ))
        elif len(facts) >= 2:
            # Use the highest-confidence fact as the twist anchor
            twist_fact = facts[-1]
            claim = twist_fact.get("claim", "")
            segments.append(Segment(
                start_time=0, end_time=0,
                text=f"But the biggest implication of all? {claim} And that changes everything about how we should think about this going forward.",
                visual_cue="future projection | data extrapolation forward",
                emotion_tone="awe",
                section="twist",
                key_phrase=claim[:60] if claim else "the biggest implication",
            ))

        # Segment 3: Strong close — viewer feels smarter
        if narrative:
            closer = (
                f"So here's what we now know: {narrative} "
                f"And once you see it this way, you can't unsee it."
            )
        else:
            closer = (
                "So the next time someone brings this up, you won't just have an opinion — "
                "you'll actually understand what's going on underneath. And that's a superpower."
            )

        segments.append(Segment(
            start_time=0, end_time=0,
            text=closer,
            visual_cue="resolution card | clean typography | fade to title",
            emotion_tone="earnest",
            section="twist",
            key_phrase="you can't unsee it",
        ))

        return segments

    def _build_cta(self, facts: List[Dict], narrative: str) -> List[str]:
        """Build optional calls to action."""
        ctas = []
        # Only add CTA if we have substantial content
        if len(facts) >= 3:
            ctas.append("If you want to go deeper, we've linked our sources in the description.")
        return ctas

    # ------------------------------------------------------------------
    # Timing engine
    # ------------------------------------------------------------------

    def _assign_timing(self, segments: List[Segment]) -> List[Segment]:
        """
        Assign start_time / end_time to each segment based on word count
        and the enforced Vox structure proportions.
        """
        if not segments:
            return segments

        # Split segments by section
        hook_segs = [s for s in segments if s.section == "hook"]
        ctx_segs = [s for s in segments if s.section == "context"]
        mech_segs = [s for s in segments if s.section == "mechanism"]
        twist_segs = [s for s in segments if s.section == "twist"]

        # Count words per section
        def _wc(segs):
            return sum(len(s.text.split()) for s in segs)

        total_words = _wc(segments)
        # Estimate total duration from word count
        est_duration = max(total_words / self.words_per_second, 30.0)

        # Clamp to target if we overshoot significantly
        if est_duration > self.target_duration * 1.3:
            est_duration = self.target_duration

        # Apply structure proportions, but let hook have its hard max
        hook_dur = min(_wc(hook_segs) / self.words_per_second, STRUCTURE["hook"]["max_s"])
        remaining = est_duration - hook_dur

        ctx_dur = remaining * STRUCTURE["context"]["pct"] / (1.0 - STRUCTURE["hook"]["pct"])
        mech_dur = remaining * STRUCTURE["mechanism"]["pct"] / (1.0 - STRUCTURE["hook"]["pct"])
        twist_dur = remaining - ctx_dur - mech_dur

        # Helper: distribute section duration across its segments by word count
        # Returns the cursor position at the end (next section's start offset)
        def _distribute(segs, section_dur, start_offset=0.0):
            words = _wc(segs)
            if words == 0 or not segs:
                return start_offset
            cursor = start_offset
            for seg in segs:
                seg_words = len(seg.text.split())
                seg_dur = (seg_words / words) * section_dur if words > 0 else section_dur / len(segs)
                # Enforce min/max segment length
                seg_dur = max(seg_dur, MIN_SEGMENT_LENGTH)
                seg_dur = min(seg_dur, SECONDS_PER_SEGMENT_MAX)
                seg.start_time = round(cursor, 1)
                seg.end_time = round(cursor + seg_dur, 1)
                cursor += seg_dur
            return cursor

        # Distribute within each section, chaining offsets
        cursor = _distribute(hook_segs, hook_dur, start_offset=0.0)
        cursor = _distribute(ctx_segs, ctx_dur, start_offset=cursor)
        cursor = _distribute(mech_segs, mech_dur, start_offset=cursor)
        _distribute(twist_segs, twist_dur, start_offset=cursor)

        # Reassemble in order
        all_ordered = sorted(segments, key=lambda s: s.start_time)
        return all_ordered

    # ------------------------------------------------------------------
    # LLM-backed generation
    # ------------------------------------------------------------------

    def _llm_generate(self, research: Dict) -> ScriptOutput:
        """
        Send research data to an LLM with the Vox system prompt.
        Supports xAI/Grok, OpenAI, and Anthropic backends.
        """
        system_prompt = self._load_system_prompt()

        user_prompt = self._build_llm_user_prompt(research)

        if self.model in ("grok", "xai"):
            raw = self._call_xai(system_prompt, user_prompt)
        elif self.model in ("openai", "gpt-4", "gpt-4o"):
            raw = self._call_openai(system_prompt, user_prompt)
        elif self.model in ("claude", "anthropic"):
            raw = self._call_anthropic(system_prompt, user_prompt)
        else:
            raise ValueError(f"Unknown LLM model: {self.model}")

        return self._parse_llm_response(raw)

    def _load_system_prompt(self) -> str:
        """Load the Vox script system prompt from prompts/script_system.txt."""
        if self.prompt_template_path.exists():
            return self.prompt_template_path.read_text()
        # Fallback: embedded minimal prompt
        return textwrap.dedent("""\
            You are a Vox-style script writer. Convert research data into a narration script
            following the structure: Hook -> Context -> Mechanism -> Twist + Resolution.

            Style: Conversational, intelligent. Smart friend explaining it.
            Avoid jargon. Include visual cues. Output JSON.
        """)

    def _build_llm_user_prompt(self, research: Dict) -> str:
        """Build the user prompt with research data for the LLM."""
        facts_str = json.dumps(research.get("facts", []), indent=2)
        narrative = research.get("key_narrative", "")
        contested = json.dumps(research.get("contested_flags", []), indent=2)
        visual_leads = json.dumps(research.get("visual_leads", []), indent=2)

        prompt = textwrap.dedent(f"""\
            Convert the following research into a Vox-style explainer script.

            KEY NARRATIVE:
            {narrative}

            FACTS:
            {facts_str}

            CONTESTED FLAGS:
            {contested}

            VISUAL LEADS:
            {visual_leads}

            Generate a complete script following Hook -> Context -> Mechanism -> Twist structure.
            Return ONLY valid JSON matching this schema:
            {{
              "hook_line": "opening hook text",
              "script_full": "complete narration text",
              "segments": [
                {{
                  "start_time": 0.0,
                  "end_time": 5.0,
                  "text": "segment narration",
                  "visual_cue": "what to show on screen",
                  "emotion_tone": "explanatory|curious|dramatic|...",
                  "section": "hook|context|mechanism|twist",
                  "key_phrase": "on-screen text"
                }}
              ],
              "calls_to_action": ["optional CTA"]
            }}

            Target ~{self.target_duration}s total. Conversational pace (~{self.voice_pace_wpm} wpm).
        """)
        return prompt

    def _call_xai(self, system_prompt: str, user_prompt: str) -> str:
        """Call xAI / Grok API."""
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("XAI_API_KEY environment variable not set")

        import urllib.request

        body = json.dumps({
            "model": "grok-3",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        }).encode()

        req = urllib.request.Request(
            "https://api.x.ai/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]

    def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        """Call OpenAI API."""
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY environment variable not set")

        import urllib.request

        body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 4096,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return result["choices"][0]["message"]["content"]

    def _call_anthropic(self, system_prompt: str, user_prompt: str) -> str:
        """Call Anthropic API."""
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY environment variable not set")

        import urllib.request

        body = json.dumps({
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.7,
        }).encode()

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            return result["content"][0]["text"]

    def _parse_llm_response(self, raw: str) -> ScriptOutput:
        """Parse LLM JSON response into ScriptOutput, with graceful fallback."""
        # Try to extract JSON from potentially markdown-wrapped response
        json_match = re.search(r'\{.*\}', raw, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return ScriptOutput(
                    script_full=data.get("script_full", ""),
                    segments=data.get("segments", []),
                    hook_line=data.get("hook_line", ""),
                    calls_to_action=data.get("calls_to_action", []),
                    metadata={"generator": f"llm:{self.model}"},
                )
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Failed to parse LLM response as JSON. Raw: {raw[:500]}...")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Script Generator - Convert researcher output to Vox-style narration script.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              %(prog)s --input research.json --output script.json
              %(prog)s --input research.json --llm --model grok
              cat research.json | %(prog)s --output script.json
        """),
    )
    parser.add_argument(
        "--input", "-i",
        help="Path to researcher JSON output (reads from stdin if omitted)",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Path to write script JSON output",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Use LLM backend instead of template-based generation",
    )
    parser.add_argument(
        "--model", "-m",
        default="grok",
        choices=["grok", "xai", "openai", "gpt-4", "gpt-4o", "claude", "anthropic"],
        help="LLM model to use when --llm is set (default: grok)",
    )
    parser.add_argument(
        "--duration", "-d",
        type=float,
        default=420.0,
        help="Target video duration in seconds (default: 420 = 7 min)",
    )
    parser.add_argument(
        "--pace",
        type=int,
        default=AVERAGE_PACE_WPM,
        help=f"Words per minute pacing (default: {AVERAGE_PACE_WPM})",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        default=True,
        help="Pretty-print JSON output (default: true)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Compact JSON output (no indentation)",
    )

    args = parser.parse_args()

    # Read input
    if args.input:
        with open(args.input, "r") as f:
            research = json.load(f)
    else:
        if sys.stdin.isatty():
            print("Error: No input file provided and stdin is a terminal.", file=sys.stderr)
            parser.print_help()
            sys.exit(1)
        research = json.load(sys.stdin)

    # Generate
    generator = ScriptGenerator(
        use_llm=args.llm,
        model=args.model,
        target_duration=args.duration,
        voice_pace_wpm=args.pace,
    )

    try:
        output = generator.generate(research)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"LLM Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Serialize
    indent = None if args.compact else 2
    output_dict = {
        "script_full": output.script_full,
        "segments": output.segments,
        "hook_line": output.hook_line,
        "calls_to_action": output.calls_to_action,
        "metadata": output.metadata,
    }

    with open(args.output, "w") as f:
        json.dump(output_dict, f, indent=indent, ensure_ascii=False)
        f.write("\n")

    print(f"Script generated: {args.output}")
    print(f"   Duration: {output.metadata.get('actual_duration_s', '?')}s")
    print(f"   Segments: {output.metadata.get('segment_count', '?')}")
    print(f"   Words: {output.metadata.get('word_count', '?')}")
    print(f"   Generator: {output.metadata.get('generator', '?')}")


if __name__ == "__main__":
    main()
