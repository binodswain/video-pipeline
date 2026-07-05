# TTS Decision Record: Kokoro-82M vs Piper

**Date:** 2026-07-05  
**Context:** On-demand VM (4-core ARM, 23 GB RAM, no GPU), Vox-style explainer narration, strictly burst-mode

## Options Considered

### Kokoro-82M
- 82M parameter model, runs comfortably on CPU
- Apache 2.0 license (very permissive)
- ~54 voices across 8 languages
- Noticeably more natural/expressive than Piper for narration
- Moderate RAM footprint during synthesis (<1 GB)
- Good speed on CPU (real-time or faster for most voices)

### Piper
- Lightweight, fast TTS engine
- MIT license
- Many voices (30+ languages)
- More robotic/mechanical sound
- Extremely small footprint (<200 MB)

## Decision

**Chosen: Kokoro-82M**

### Rationale
1. **Quality matters** — For Vox-style explainers with fact-checked data, voice quality directly affects how seriously the content is received. Kokoro is notably more natural.
2. **Still lightweight** — At 82M params, Kokoro runs comfortably on 4-core ARM CPU without persistent load.
3. **Licensing** — Apache 2.0 is production-friendly.
4. **On-demand model** — Model loaded only during build, fully released after. No footprint concern when idle.
5. **Credibility** — Better voice quality supports the "accurate, trustworthy data" positioning.

### When Piper Would Be Preferred
- Extremely constrained environments (<1 GB RAM total)
- Maximum speed over quality
- Non-narration use (quick responses, accessibility overlays)

## Implementation Note
- Wrapper: `scripts/utils/tts.py`
- Model downloaded on first use, cached in `~/.cache/video-pipeline/kokoro/`
- Each invocation: load → synthesize → unload → cleanup

## Update: 2026-07-05 — Kokoro Successfully Installed

Kokoro-82M installed via `pip install kokoro` (v0.9.4). End-to-end synthesis tested and working:

- **Dependencies**: torch 2.12.1, transformers 5.13.0, spacy 3.8.14, soundfile 0.14.0
- **Synthesize test**: "This is a test of the Kokoro TTS system" → 6.2s WAV, 24kHz mono 16-bit PCM
- **Segments test**: Multi-segment batch synthesis working with auto-created output dirs
- **Voice**: af_heart (default), all Kokoro voices available
- **No Piper fallback needed** — Kokoro installed and fully functional

### Fixes Applied
- `synthesize_segments()` now auto-creates `output_dir` via `os.makedirs(output_dir, exist_ok=True)`
- `requirements.txt`: widened `soundfile` constraint from `<0.13` to `<1.0` (installed 0.14.0)
