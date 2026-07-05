#!/usr/bin/env python3

"""
build_video.py: End-to-end CLI for the video pipeline.

Converts a single topic into an MP4 Vox-style explainer video by
automatically chaining:
  1. Research (topic -> fact-checked data with provenance)
  2. Script generation (research -> Vox narration: Hook->Context->Mechanism->Twist)
  3. Optional review (interactive pause to inspect the script)
  4. Asset pipeline (slide prompts + curation)
  5. Video assembly (Kokoro TTS + MoviePy Ken Burns walks + burned subtitles)
  6. Wind-down (temp cleanup, resource release)

Modes:
  --topic     : Full auto pipeline from a single topic string
  --research  : Skip research, use a pre-researched JSON file
  --slides    : Skip everything, feed slides + script directly to the assembler
"""

import argparse, json, os, shutil, struct, sys, tempfile, zlib
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from researcher import research, list_backends
from script_generator import ScriptGenerator, ScriptOutput
from asset_pipeline import AssetPipeline
from utils.cleanup import LockFile, TempDirManager, resource_guard, wind_down
from utils.tts import KokoroTTS
from utils.walks import build_video_track
from utils.subtitles import generate_srt, burn_subtitles, write_srt


def build_video(slides, script, output_path, resolution=(1920,1080), fps=24, voice="af_heart", crossfade=0.5, no_subtitles=False):
    temp_dirs = []
    try:
        tempdir = Path(tempfile.mkdtemp(prefix="video_build_"))
        temp_dirs.append(tempdir)
        audio_dir = tempdir / "audio"; audio_dir.mkdir()
        print(f"\n[Build] Starting video build: {len(slides)} slides", file=sys.stderr)
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
        print(f"[Build] TTS complete: {audio_duration:.1f}s ({len(word_timings)} words)", file=sys.stderr)
        if not no_subtitles:
            print("[Build] Generating subtitles...", file=sys.stderr)
            srt_text = generate_srt(word_timings)
            write_srt(srt_text, str(tempdir / "subtitles.srt"))
        else:
            srt_text = ""
        print("[Build] Building video track with Ken Burns walks...", file=sys.stderr)
        with resource_guard(max_cores=2, nice_level=19):
            video_clip = build_video_track(slides, output_size=resolution, fps=fps, crossfade_duration=crossfade)
        if srt_text and not no_subtitles:
            print("[Build] Burning subtitles into video...", file=sys.stderr)
            font_size = int(resolution[1] * 0.04)
            video_clip = burn_subtitles(video_clip, srt_text, font_size=font_size, position=("center", resolution[1]*0.87), output_size=resolution)
        print(f"[Build] Encoding final MP4 to {output_path}...", file=sys.stderr)
        video_clip = video_clip.with_audio(audio_path)
        video_clip.write_videofile(output_path, fps=fps, codec="libx264", audio_codec="aac", preset="ultrafast", threads=1)
        output_bytes = os.path.getsize(output_path)
        print(f"[Build] OK Complete! {output_bytes/1048576:.1f} MB  |  {audio_duration:.1f}s  |  {len(slides)} slides", file=sys.stderr)
        return {"output":output_path, "duration_seconds":audio_duration, "size_bytes":output_bytes, "resolution":list(resolution), "fps":fps}
    finally:
        for d in temp_dirs:
            try: shutil.rmtree(d, ignore_errors=True)
            except: pass


def _run_research(topic, max_sources=4, backend=None, verbose=True):
    print(f"\n{'='*60}", file=sys.stderr)
    print(f"[Research] Topic: {topic}", file=sys.stderr)
    print(f"[Research] Backend: {backend or 'auto-detect'}, max sources: {max_sources}", file=sys.stderr)
    result = research(topic, max_sources=max_sources, include_x=True, verbose=verbose, search_backend=backend)
    data = result.to_dict()
    print(f"[Research] OK {len(data['facts'])} facts  |  {len(data['visual_leads'])} visual leads", file=sys.stderr)
    print(f"[Research]   Narrative: {data['key_narrative'][:120]}...", file=sys.stderr)
    if data.get("contested_flags"):
        print(f"[Research]   WARNING {len(data['contested_flags'])} contested claims flagged", file=sys.stderr)
    return data


def _run_script_generation(research_data, target_duration=420.0):
    print(f"\n{'-'*60}", file=sys.stderr)
    print("[Script] Generating Vox-style narration (Hook->Context->Mechanism->Twist)...", file=sys.stderr)
    gen = ScriptGenerator(use_llm=False, target_duration=target_duration)
    output = gen.generate(research_data)
    script_dict = asdict(output)
    n_segments = len(script_dict.get("segments", []))
    duration = output.metadata.get("total_duration_seconds", 0)
    print(f"[Script] OK {n_segments} segments  |  ~{duration:.0f}s  |  {output.metadata.get('total_words', 0)} words", file=sys.stderr)
    print(f"[Script]   Hook: '{output.hook_line[:100]}...'", file=sys.stderr)
    return script_dict


def _print_script_review(script):
    print(f"\n{'-'*60}", file=sys.stderr)
    print("[Review] -- SCRIPT FOR REVIEW --", file=sys.stderr)
    print(f"[Review] Duration: ~{script['metadata'].get('total_duration_seconds', 0):.0f}s", file=sys.stderr)
    print(f"[Review] Words: {script['metadata'].get('total_words', 0)}", file=sys.stderr)
    print(f"[Review] Hook: {script.get('hook_line', '')}", file=sys.stderr)
    print("[Review] --------------------------", file=sys.stderr)
    print("=" * 60)
    print("FULL SCRIPT:")
    print("=" * 60)
    print(script.get("script_full", ""))
    print("=" * 60)
    print("\nSEGMENT BREAKDOWN:")
    for seg in script.get("segments", []):
        section = seg.get("section", "?").upper()[:12]
        t0, t1 = seg.get("start_time", 0), seg.get("end_time", 0)
        cue = seg.get("visual_cue", "")[:50]
        print(f"  {section:12s} [{t0:5.1f}s->{t1:5.1f}s] {cue}")
    print("=" * 60)


def _run_asset_pipeline(script, research, tempdir, topic=""):
    print(f"\n{'-'*60}", file=sys.stderr)
    print("[Assets] Generating image prompts and curating assets...", file=sys.stderr)
    asset_pipeline = AssetPipeline(output_dir=tempdir, style="editorial")
    slides = asset_pipeline.run(script.get("segments", []), research.get("visual_leads", []))
    has_empty = not slides or all(s.get("path", "") == "" for s in slides)
    if has_empty:
        placeholder = Path(tempdir) / "placeholder.png"
        _create_placeholder_image(placeholder, text=topic, resolution=(1920, 1080))
        for s in slides:
            s["path"] = str(placeholder)
    print(f"[Assets] OK {len(slides)} slides prepared", file=sys.stderr)
    return slides


def _build_from_topic(args, tempdir):
    research_data = _run_research(args.topic, max_sources=getattr(args, "max_sources", 4), backend=getattr(args, "backend", None))
    script = _run_script_generation(research_data, target_duration=getattr(args, "target_duration", 420.0))
    _print_script_review(script)
    if getattr(args, "review", False):
        print("\n[Review] Press Enter to continue to video build, or Ctrl+C to abort...", file=sys.stderr)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n[Review] Build aborted by user.", file=sys.stderr)
            sys.exit(0)
    slides = _run_asset_pipeline(script, research_data, str(tempdir), topic=args.topic)
    return slides, script


def _build_from_research(args, tempdir):
    with open(args.research) as f:
        research_data = json.load(f)
    print(f"\n[Pipeline] Loaded research: {len(research_data.get('facts', []))} facts", file=sys.stderr)
    script = _run_script_generation(research_data, target_duration=getattr(args, "target_duration", 420.0))
    _print_script_review(script)
    if getattr(args, "review", False):
        print("\n[Review] Press Enter to continue to video build, or Ctrl+C to abort...", file=sys.stderr)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            print("\n[Review] Build aborted by user.", file=sys.stderr)
            sys.exit(0)
    topic = research_data.get("topic", research_data.get("key_narrative", ""))
    slides = _run_asset_pipeline(script, research_data, str(tempdir), topic=topic)
    return slides, script


def _build_from_slides(args):
    if not args.script:
        raise SystemExit("--script is required when using --slides")
    dir_path = Path(args.slides)
    slides_file = dir_path / "slides.json"
    if slides_file.exists():
        with open(slides_file) as f:
            slides = json.load(f)
    else:
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        images = sorted([f for f in dir_path.iterdir() if f.suffix.lower() in image_exts])
        slides = []
        for img in images:
            slides.append({"path": str(img), "duration": 5.0, "zoom_amt": 0.08, "pan_direction": "random", "text_overlay": ""})
    with open(args.script) as f:
        script = json.load(f)
    return slides, script


def _create_placeholder_image(path, text="", resolution=(1920, 1080)):
    try:
        from PIL import Image, ImageDraw, ImageFont
        img = Image.new("RGB", resolution, color=(30, 30, 40))
        draw = ImageDraw.Draw(img)
        font = None
        for fp in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"]:
            if os.path.exists(fp):
                font = ImageFont.truetype(fp, size=60)
                break
        if text:
            draw.text((resolution[0]//2, resolution[1]//2), text, fill=(255,255,255), font=font, anchor="mm")
        img.save(path)
    except ImportError:
        _create_minimal_png(path, resolution)
    except Exception as e:
        print(f"[Build] Failed to create placeholder: {e}", file=sys.stderr)
        _create_minimal_png(path, resolution)


def _create_minimal_png(path, resolution=(1920, 1080)):
    w, h = resolution
    raw = bytearray(w * h * 3)
    # Create scanlines (filter byte 0 + pixel data per row)
    scanlines = b''.join(b'\x00' + bytes(raw[i:i+w*3]) for i in range(0, len(raw), w*3))
    compressor = zlib.compressobj(level=9)
    compressed = compressor.compress(scanlines) + compressor.flush()
    def _png_chunk(ctype, d):
        chunk = struct.pack(">I", len(d)) + ctype + d
        crc = zlib.crc32(chunk) & 0xFFFFFFFF
        return chunk + struct.pack(">I", crc)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", compressed) + _png_chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(png)


def main():
    parser = argparse.ArgumentParser(description="Build a Vox-style explainer video -- auto pipeline from topic to MP4.")
    input_group = parser.add_mutually_exclusive_group()
    input_group.add_argument("--topic", help="Build from a topic string (full auto pipeline)")
    input_group.add_argument("--research", help="Path to pre-researched JSON file")
    input_group.add_argument("--slides", help="Directory of pre-made slides + optional slides.json")
    input_group.add_argument("--list-backends", action="store_true", help="List available search backends and exit")
    parser.add_argument("--output", "-o", help="Output MP4 file path")
    parser.add_argument("--backend", help="Research search backend (see --list-backends)")
    parser.add_argument("--max-sources", type=int, default=4)
    parser.add_argument("--target-duration", type=float, default=420.0, help="Target video duration in seconds")
    parser.add_argument("--review", action="store_true", help="Pause for script review before building video")
    parser.add_argument("--script", help="Path to script JSON (required for --slides mode)")
    parser.add_argument("--resolution", default="1920x1080")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--crossfade", type=float, default=0.5)
    parser.add_argument("--no-subtitles", action="store_true")
    parser.add_argument("--dry", action="store_true", help="Dry run: skip MP4 encoding")
    args = parser.parse_args()

    if args.list_backends:
        print("Available search backends:")
        for b in list_backends():
            print(f"  - {b}")
        return

    if not args.output:
        parser.error("--output is required")

    if "x" in args.resolution:
        w, h = args.resolution.split("x")
        resolution = (int(w), int(h))
    else:
        resolution = (1920, 1080)

    with LockFile() as lock:
        with TempDirManager(prefix="video_pipeline_") as tempdir:
            temp_path = Path(tempdir.path)
            print(f"[Pipeline] Temp dir: {tempdir.path}", file=sys.stderr)

            if args.topic:
                slides, script = _build_from_topic(args, temp_path)
            elif args.research:
                slides, script = _build_from_research(args, temp_path)
            elif args.slides:
                slides, script = _build_from_slides(args)
            else:
                parser.error("Must specify --topic, --research, --slides, or --list-backends")

            if not slides:
                print("Error: No slides generated", file=sys.stderr)
                sys.exit(1)

            if args.dry:
                print(json.dumps({"mode": "dry_run", "num_slides": len(slides), "script_preview": script.get("script_full", "")[:300], "resolution": list(resolution)}, indent=2))
                print("\n[Dry Run] OK Everything looks good. Skipping MP4 encode.", file=sys.stderr)
                return

            result = build_video(slides=slides, script=script, output_path=args.output, resolution=resolution, fps=args.fps, voice=args.voice, crossfade=args.crossfade, no_subtitles=args.no_subtitles)
            print(json.dumps(result, indent=2), file=sys.stderr)
            print(f"\n{'='*60}", file=sys.stderr)
            print(f"OK Video created: {args.output}", file=sys.stderr)
            print(f"  Duration: {result['duration_seconds']:.1f}s  |  Size: {result['size_bytes']/1048576:.1f} MB", file=sys.stderr)
            print(f"{'='*60}", file=sys.stderr)

    wind_down()


if __name__ == "__main__":
    main()
