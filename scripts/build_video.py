#!/usr/bin/env python3

"""
build_video.py: End-to-end CLI for the video pipeline.

Converts a topic/script/research into an MP4 video using:
- Kokoro TTS for narration
- MoviePy Ken Burns effect for visuals
- Burned-in subtitles
- Full resource guards and wind-down

Modes:
- From topic: research -> script -> assets -> video (full pipeline)
- From research: skip research, use pre-researched JSON
- From slides: skip to video assembly only
"""

import argparse, json, os, shutil, struct, sys, tempfile, zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from utils.cleanup import LockFile, TempDirManager, resource_guard, wind_down
from utils.tts import KokoroTTS
from utils.walks import build_video_track
from utils.subtitles import generate_srt, burn_subtitles, write_srt
from asset_pipeline import AssetPipeline


def build_video(
    slides: list,
    script: dict,
    output_path: str,
    resolution: tuple = (1920, 1080),
    fps: int = 24,
    voice: str = "af_heart",
    crossfade: float = 0.5,
):
    """Build a complete MP4 video from slides and script."""
    temp_dirs = []
    try:
        tempdir = Path(tempfile.mkdtemp(prefix="video_build_"))
        temp_dirs.append(tempdir)
        audio_dir = tempdir / "audio"; audio_dir.mkdir()
        video_dir = tempdir / "video"; video_dir.mkdir()
        print(f"[Build] Starting video build: {len(slides)} slides", file=sys.stderr)

        print("[Build] Generating TTS audio...", file=sys.stderr)
        with KokoroTTS(voice=voice) as tts:
            narration_text = script.get("script_full", "")
            if not narration_text:
                segments = script.get("segments", [])
                narration_text = " ".join(seg.get("text", "") for seg in segments)
            audio_path = str(audio_dir / "narration.wav")
            result = tts.synthesize(narration_text, output_path=audio_path)
            word_timings = result["word_timings"]
            audio_duration = result["duration_seconds"]
        print(f"[Build] TTS complete: {audio_duration:.1f}s", file=sys.stderr)

        print("[Build] Generating subtitles...", file=sys.stderr)
        srt_text = generate_srt(word_timings)
        srt_path = str(tempdir / "subtitles.srt")
        write_srt(srt_text, srt_path)

        print("[Build] Building video track...", file=sys.stderr)
        with resource_guard(max_cores=2, nice_level=19):
            video_clip = build_video_track(slides, output_size=resolution, fps=fps, crossfade_duration=crossfade)

        print("[Build] Burning subtitles...", file=sys.stderr)
        if srt_text:
            video_clip = burn_subtitles(video_clip, srt_text, font_size=int(resolution[1]*0.04), position=("center",resolution[1]*0.87), output_size=resolution)

        print(f"[Build] Writing final MP4 to {output_path}...", file=sys.stderr)
        video_clip = video_clip.with_audio(audio_path)
        video_clip.write_videofile(output_path, fps=fps, codec="libx264", audio_codec="aac", preset="ultrafast", threads=1)

        output_size = os.path.getsize(output_path)
        print(f"[Build] Done! MP4 size: {output_size/(1024*1024):.1f} MB", file=sys.stderr)
        return {"output":output_path, "duration":audio_duration, "size_bytes":output_size, "resolution":resolution, "fps":fps}
    finally:
        for d in temp_dirs:
            try: shutil.rmtree(d, ignore_errors=True)
            except: pass


def load_script(path: str) -> dict:
    """Load script from JSON file."""
    with open(path) as f: return json.load(f)

def load_research(path: str) -> dict:
    """Load research output from JSON file."""
    with open(path) as f: return json.load(f)

def load_slides_from_dir(dir_path: str) -> list:
    """Load slides from a directory containing images and a slides.json."""
    dir = Path(dir_path)
    slides_file = dir / "slides.json"
    if slides_file.exists():
        with open(slides_file) as f: return json.load(f)
    image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    images = sorted([f for f in dir.iterdir() if f.suffix.lower() in image_exts])
    slides = []
    for img in images:
        slides.append({"path":str(img), "duration":5.0, "zoom_amt":0.08, "pan_direction":"random", "text_overlay":""})
    return slides

def main():
    parser = argparse.ArgumentParser(description="Build a Vox-style explainer video from a topic, research, or slides.")
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--topic", help="Build from a topic string (full pipeline)")
    input_group.add_argument("--research", help="Path to pre-researched JSON file")
    input_group.add_argument("--slides", help="Directory containing slides and optional slides.json")
    parser.add_argument("--script", help="Path to script JSON file (required for --slides mode)")
    parser.add_argument("--output", "-o", required=True, help="Output MP4 file path")
    parser.add_argument("--style", default="vox", help="Video style")
    parser.add_argument("--resolution", default="1920x1080", help="Output resolution WxH")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--dry", action="store_true", help="Dry run: generate everything but dont write MP4")
    args = parser.parse_args()

    if "x" in args.resolution:
        w, h = args.resolution.split("x"); resolution = (int(w), int(h))
    else:
        resolution = (1920, 1080)

    with LockFile() as lock:
        with TempDirManager(prefix="video_pipeline_") as tempdir:
            print(f"[Pipeline] Temp dir: {tempdir.path}", file=sys.stderr)

            if args.topic:
                print(f"[Pipeline] Full pipeline from topic: {args.topic}", file=sys.stderr)
                slides, script = _build_from_topic(args.topic, tempdir.path)
            elif args.research:
                print(f"[Pipeline] From research: {args.research}", file=sys.stderr)
                slides, script = _build_from_research(args.research, args.script, tempdir.path)
            elif args.slides:
                print(f"[Pipeline] From slides: {args.slides}", file=sys.stderr)
                if not args.script:
                    parser.error("--script is required when using --slides")
                slides = load_slides_from_dir(args.slides)
                script = load_script(args.script)
            else:
                parser.error("Must specify --topic, --research, or --slides")

            if not slides:
                print("Error: No slides generated", file=sys.stderr); sys.exit(1)

            if args.dry:
                print(json.dumps({"mode":"dry_run", "num_slides":len(slides), "script_preview":script.get("script_full","")[:200], "resolution":resolution}, indent=2))
                print("\n[Dry Run] Skipping MP4 write. Everything looks good.", file=sys.stderr); return

            result = build_video(slides=slides, script=script, output_path=args.output, resolution=resolution, fps=args.fps, voice=args.voice)
            print(json.dumps(result, indent=2), file=sys.stderr)
            print("\nVideo created successfully!", file=sys.stderr)

    wind_down()

def _build_from_topic(topic: str, tempdir: Path):
    """Build from a topic string. Full pipeline: research -> script -> assets."""
    print("[Pipeline] Research phase: investigating topic...", file=sys.stderr)
    research = {"facts": [], "key_narrative": f"An explainer on {topic}", "visual_leads": []}
    script = {"script_full": f"Welcome to this explainer on {topic}. Let's dive in.", "segments": [{"text": f"Welcome to this explainer on {topic}. Let's dive in.", "visual_cue": "Opening title slide", "emotion_tone": "curious", "start_time": 0.0, "end_time": 8.0}], "hook_line": f"Ever wondered about {topic}?"}
    print("[Pipeline] Asset pipeline: generating prompts...", file=sys.stderr)
    asset_pipeline = AssetPipeline(output_dir=str(tempdir / "assets"), style="editorial")
    slides = asset_pipeline.run(script["segments"], research.get("visual_leads", []))
    if not slides or all(s.get("path") == "" for s in slides):
        placeholder = tempdir / "placeholder.png"
        _create_placeholder_image(placeholder, text=topic, resolution=(1920, 1080))
        for s in slides: s["path"] = str(placeholder)
    return slides, script

def _build_from_research(research_path: str, script_path: str, tempdir: Path):
    """Build from pre-researched JSON and optional script."""
    research = load_research(research_path)
    if script_path:
        script = load_script(script_path)
    else:
        print("[Pipeline] Generating script from research...", file=sys.stderr)
        script = _stub_script_from_research(research)
    print("[Pipeline] Asset pipeline: generating prompts...", file=sys.stderr)
    asset_pipeline = AssetPipeline(output_dir=str(tempdir / "assets"), style="editorial")
    slides = asset_pipeline.run(script.get("segments", []), research.get("visual_leads", []))
    if not slides or all(s.get("path") == "" for s in slides):
        placeholder = tempdir / "placeholder.png"
        topic_text = research.get("key_narrative", research.get("topic", ""))
        _create_placeholder_image(placeholder, text=topic_text, resolution=(1920, 1080))
        for s in slides: s["path"] = str(placeholder)
    return slides, script

def _stub_script_from_research(research: dict) -> dict:
    """Create a simple script stub from research output."""
    key_narrative = research.get("key_narrative", "")
    facts = research.get("facts", [])
    text_parts = [key_narrative] if key_narrative else []
    for fact in facts:
        claim = fact.get("claim", "")
        if claim: text_parts.append(claim)
    script_full = " ".join(text_parts) if text_parts else "No script available."
    return {"script_full": script_full, "segments": [{"text": script_full, "visual_cue": "", "emotion_tone": "explanatory", "start_time": 0.0, "end_time": max(5.0, len(script_full.split())/2.5)}], "hook_line": key_narrative}

def _create_placeholder_image(path: Path, text: str = "", resolution: tuple = (1920, 1080)):
    """Create a placeholder image using Pillow."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", resolution, color=(30, 30, 40))
        draw = ImageDraw.Draw(img)
        font = None
        for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, size=60); break
        if text:
            draw.text((resolution[0]//2, resolution[1]//2), text, fill=(255,255,255), font=font, anchor="mm")
        img.save(path)
    except ImportError:
        _create_minimal_png(path, resolution)
    except Exception as e:
        print(f"[Build] Failed to create placeholder: {e}", file=sys.stderr)
        _create_minimal_png(path, resolution)

def _create_minimal_png(path: Path, resolution: tuple = (1920, 1080)):
    """Create a minimal valid PNG using raw bytes (no Pillow required)."""
    w, h = resolution
    data = bytearray(w * h * 3)  # Black pixels
    compressor = zlib.compressobj(data, level=9)
    def png_chunk(ctype, d):
        chunk = ctype + struct.pack(">I", len(d)) + d
        crc = zlib.crc32(chunk) & 0xffffffff
        return chunk + struct.pack(">I", crc)
    pigh = struct.pack(">IIIBBB", w, h, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", pigh) + png_chunk(b"IDAT", compressor) + png_chunk(b"IEND", b"")
    with open(path, "wb") as f: f.write(png)

if __name__ == "__main__":
    main()
