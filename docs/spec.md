# Video Pipeline — Technical Specification

> **Status:** Ready for implementation | **Repo:** ~/workspace/video-pipeline

## Overview

On-demand, researcher-first pipeline that converts a **topic** into an accurate, engaging **Vox-style explainer video** (MP4). Nothing runs persistently — all resources spin up and fully wind down per job.

## Architecture (End-to-End Flow)

```
Topic / Brief
     ↓
[Researcher Agent] ← Entry Point
     ↓
Fact-Check Loop + Source Validation
     ↓
Research Output (facts, sources, confidence, raw assets)
     ↓
├── Script Generator (Vox style)
├── Image Prompt Generator (→ xAI Grok / OpenAI)
└── Asset Curator (real images from research)
     ↓
Approved Assets + Script + Direction
     ↓
[On-Demand Video Assembler] ← VM only
     ↓ (Kokoro TTS + MoviePy Ken Burns + subtitles)
Final MP4 → Cleanup → Wind-Down
```

## Component Specs

### 1. Researcher Agent

**Purpose:** Research a topic from multiple sources, produce fact-checked data with provenance.

**Input:** Topic string + optional brief/angle.

**Sources:**
- xAI / Grok (search + real-time knowledge)
- Web search (Google / general)
- X (Twitter) search for current discussion

**Output (structured JSON + Markdown):**
- `facts[]` — {claim, evidence, source_url, source_name, date, confidence (0–1)}
- `source_summary` — overall reliability assessment
- `key_narrative` — 2–3 sentence TL;DR
- `visual_leads[]` — any real images, maps, charts found with URLs + suggested crop/highlight
- `contested_flags[]` — any claims that are disputed or low-confidence

**Fact-Check Rules:**
- Always cross-verify against primary sources (official reports, datasets, reputable outlets)
- Lateral reading: check what other sources say about the claim
- Flag anything opinion, outdated, or single-source
- No claim advances without ≥2 sources or 1 primary
- Source reliability labels: government/official > peer-reviewed > major news > blog > social

### 2. Script Generator

**Purpose:** Convert research output into a Vox-style narration script.

**Structure (enforced):**

| Section | Duration | Content |
|---------|----------|---------|
| Hook | 0–30s | Curiosity gap, counter-intuitive fact, bold question |
| Context | ~25% | How we got here (history, timeline, maps, data) |
| Mechanism | ~50% | Deep "how it works" — analogies, step-by-step, building visuals |
| Twist + Resolution | ~25% | Surprising implication, modern stakes, paradigm shift, strong close |

**Style Rules:**
- Conversational, intelligent tone ("smart friend explaining it")
- Avoid jargon or define immediately
- On-screen text cues for key phrases (include in script output)
- Frequent open loops / micro-payoffs
- Ends with viewer feeling smarter

**Output (JSON):**
- `script_full` — complete narration text
- `segments[]` — [{start_time, end_time, text, visual_cue, emotion_tone}]
- `hook_line` — opening hook text
- `calls_to_action` — optional end CTAs

### 3. Asset Pipeline

#### 3a. AI-Generated Images
- Script Generator produces image prompts per segment
- Sent to external API: xAI Grok image gen or OpenAI
- Downloaded locally to assets dir
- Tracked: prompt + model + seed for reproducibility

#### 3b. Real Assets (from research)
- Images found during research → downloaded
- Auto or semi-auto crop/highlight relevant portions
- Used as-is or lightly enhanced

**Output:** Ordered list of `{slide_path, type (ai|real), duration_hint, pan_direction}`

### 4. Video Direction Layer

**Purpose:** Enforce consistent Vox visual language.

**Rules (applied by assembler):**
- **Constant motion** — never static for >3 seconds
- **Clean editorial style** — limited palette (2–3 colors + 1 accent)
- **Typography** — clean sans-serif, 2 weights, on-screen text matches key phrases
- **Parallax** — layered foreground/midground/background for depth
- **Visual density** — new visual stimulus every 2–7 seconds
- **Subtle effects** — smooth long ease-ins, light chromatic aberration/texture on historical pieces
- **Perfect sync** — visuals lockstep with narration
- **Chaptering** — clear visual progression and implicit sections

**Engagement Hooks (baked in):**
- Opening curiosity loop (pays off later)
- Frequent micro-"aha" moments
- Relatable analogies + real-world stakes
- Tight pacing, no dead air
- Ending that makes viewer feel smarter

### 5. On-Demand Video Assembler

**Purpose:** Take approved assets + script + direction, produce final MP4. **Runs ONLY on VM, ONLY when triggered. Full cleanup after.**

**Tech Stack (VM):**
| Component | Choice | Why |
|-----------|--------|-----|
| TTS | Kokoro-82M | Natural, expressive, Apache 2.0, runs on CPU, tiny footprint |
| Motion / Walks | MoviePy v2 + imageio-ffmpeg | Ken Burns (zoom+pan+crossfade), pure Python |
| Slides / Text | Pillow, python-pptx | Slide prep, PPT-to-text extraction |
| Subtitles | MoviePy built-in + SRT | Burn-in with timing from TTS |
| Encoding | imageio-ffmpeg (bundled) | No system FFmpeg required |

**Inputs:**
- `slides[]` — [{path, duration, pan_direction, text_overlay}]
- `script` — full narration text or `segments[]`
- `config` — resolution (default 1920×1080), fps (24), quality presets

**Processing (per slide):**
1. Load image
2. Apply Ken Burns walk (zoom from 1.0 to 1.0+zoom_amt, pan in direction)
3. Duration = per-slide seconds + crossfade overlap
4. Crossfade transition between slides
5. Burn text overlays (key phrases from script)
6. Composite all into final clip

**Audio:**
1. Generate TTS audio (Kokoro) from full narration
2. Get word timings for subtitle sync
3. Mux audio track with video

**Subtitles:**
1. Generate SRT from TTS word timings
2. Burn into video as overlay (2-line max, centered bottom)

**Resource Guards (ALWAYS applied):**
- `nice -n 19` (lowest CPU priority)
- `taskset -c 0,1` (max 2 of 4 cores)
- MoviePy: `threads=1`, `preset='ultrafast'` or `'veryfast'`
- Temp files in unique dir, deleted on exit (even on error)
- One job at a time (lock file)

**Output:** MP4 at target resolution.

**Cleanup:**
- Delete all temp files and intermediate outputs
- Release memory (process exits)
- VM returns to idle

### 6. Wind-Down Protocol

After every video build:
1. Delete temp directories
2. Close all file handles
3. Process exits (no daemons, no loaded models)
4. Lock file removed
5. Verify idle state: load average < 0.2, free RAM > 15 GB

## API / CLI Interface

```bash
# From topic (research → script → video)
python scripts/build_video.py \
  --topic "How the Electoral College actually works" \
  --output /tmp/electoral_college.mp4 \
  --style vox \
  --resolution 1920x1080

# From pre-researched data
python scripts/build_video.py \
  --research research_output.json \
  --output /tmp/video.mp4

# From existing slides + script
python scripts/build_video.py \
  --slides ./slides/ \
  --script script.json \
  --output /tmp/video.mp4
```

## Directory Structure

```
~/workspace/video-pipeline/
├── README.md
├── docs/
│   ├── spec.md                    # This file
│   ├── architecture.md            # High-level architecture
│   └── tts-decision-record.md     # Why Kokoro chosen
├── scripts/
│   ├── build_video.py             # Main on-demand assembler
│   ├── researcher.py              # Researcher agent
│   ├── script_generator.py        # Vox script generation
│   ├── asset_pipeline.py          # Image gen + curation
│   └── utils/
│       ├── tts.py                 # Kokoro TTS wrapper
│       ├── walks.py               # Ken Burns effect (MoviePy)
│       ├── subtitles.py           # SRT generation + burn-in
│       └── cleanup.py             # Resource guards + wind-down
├── prompts/
│   ├── researcher_system.txt      # System prompt for researcher
│   ├── script_system.txt          # System prompt for script gen
│   └── image_prompt_template.txt  # Template for AI image prompts
├── workflows/                     # Hermes workflow definitions
│   └── full_pipeline.yaml         # Complete pipeline workflow
├── assets/                        # Reference images, style examples
├── tests/
│   ├── test_researcher.py
│   ├── test_script_gen.py
│   ├── test_assembler.py
│   └── fixtures/
└── .gitignore
```

## Constraints (Non-Negotiable)

1. **No persistent processes** — everything exits after job completion
2. **No GPU/VRAM** — CPU-only on 4-core ARM (Neoverse-N1)
3. **No constant memory** — models loaded only during job execution
4. **Accuracy before speed** — research must be fact-checked before script generation
5. **All source code in ~/workspace/video-pipeline/**
6. **Git versioned** — every meaningful change committed
7. **Wind-down verified** — idle state confirmed after every run

## Success Criteria

1. Researcher produces cited, fact-checked data from real sources
2. Script follows Vox structure with engagement hooks
3. Video assembler runs on-demand on VM, <4 cores, <4 GB RAM during render
4. Final MP4 has smooth Ken Burns walks, crossfades, burned subtitles, and Kokoro narration
5. Full cleanup after every run, VM returns to idle
6. Pipeline fully versioned in Git with clear commit history
7. Hermes skills/workflows defined for repeatable use

## Implementation Order (Recommended)

1. **Sprint 1:** Researcher agent (fact-checked research from topic)
2. **Sprint 2:** Script generator (Vox-style from research)
3. **Sprint 3:** Asset pipeline (AI gen + curation)
4. **Sprint 4:** Video assembler (TTS + Ken Burns walks + subtitles + cleanup)
5. **Sprint 5:** Integration + end-to-end `build_video.py` + Hermes workflow
