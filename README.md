# Video Pipeline (Vox-Style Explainier)

On-demand, fact-checked, versioned pipeline for turning topics into accurate Vox-style videos.

## Principles
- Researcher-first (accuracy + provenance mandatory)
- Vox structure: Hook → Context → Mechanism → Twist
- External images (xAI Grok / OpenAI) + real assets
- On-demand only on VM (no persistent load)
- Full cleanup after every run
- Everything tracked in Git

## Current Status
- [ ] Researcher agent
- [ ] Fact-check loop
- [ ] Script + direction generator
- [ ] Asset handling
- [ ] On-demand assembler (TTS + MoviePy walks + subs)
- [ ] Wind-down + cleanup

## How to Run (once built)
```bash
# Example on-demand trigger
python scripts/build_video.py --topic "Your topic here" --output /tmp/video.mp4
```

See docs/ for architecture and workflows/.
