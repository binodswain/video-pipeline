"""
SRT subtitle generation and burn-in for the video pipeline.

Provides:
- generate_srt(): create SRT-format subtitles from word timings
- burn_subtitles(): burn subtitles into a MoviePy VideoClip
- parse_srt(): parse existing SRT files
"""

import os
import sys
from pathlib import Path
from typing import Optional


def generate_srt(
    word_timings: list,
    max_chars_per_line: int = 42,
    max_lines: int = 2,
    min_duration: float = 0.8,
    gap_threshold: float = 0.5,
) -> str:
    """
    Generate SRT-format subtitles from word-level timings.

    Groups words into subtitle blocks, respecting max chars per line
    and splitting at natural pause points.

    Args:
        word_timings: list of {word, start, end} dicts from TTS
        max_chars_per_line: Maximum characters per subtitle line
        max_lines: Maximum number of lines per subtitle block (1 or 2)
        min_duration: Minimum duration for a subtitle block in seconds
        gap_threshold: Treat gaps larger than this as split points

    Returns:
        SRT-formatted string
    """
    if not word_timings:
        return ""

    blocks = _group_words_into_blocks(
        word_timings, max_chars_per_line, max_lines, min_duration, gap_threshold
    )

    srt_lines = []
    for i, block in enumerate(blocks, 1):
        start_ts = _format_timestamp(block["start"])
        end_ts = _format_timestamp(block["end"])
        srt_lines.append(str(i))
        srt_lines.append(f"{start_ts} --> {end_ts}")
        srt_lines.append(block["text"])
        srt_lines.append("")  # Blank line between blocks

    return "\n".join(srt_lines)


def _group_words_into_blocks(
    word_timings, max_chars, max_lines, min_duration, gap_threshold
):
    """Group words into subtitle blocks."""
    blocks = []
    current_words = []
    current_start = None
    current_chars = 0

    for i, wt in enumerate(word_timings):
        word = wt["word"]
        start = wt["start"]
        end = wt["end"]

        if current_start is None:
            current_start = start

        # Check if we should split: gap before this word
        if i > 0 and current_words:
            gap = start - word_timings[i - 1]["end"]
            if gap > gap_threshold:
                # Natural pause — commit current block
                if current_words:
                    blocks.append(_make_block(current_words, current_start))
                current_words = []
                current_start = start
                current_chars = 0

        # Check if adding this word would exceed limits
        would_exceed_chars = current_chars + len(word) + (1 if current_words else 0) > max_chars * max_lines
        # Also check if current block is long enough
        duration_so_far = end - current_start

        if would_exceed_chars and current_words and duration_so_far >= min_duration:
            blocks.append(_make_block(current_words, current_start))
            current_words = []
            current_start = start
            current_chars = 0

        current_words.append(wt)
        current_chars += len(word) + (1 if len(current_words) > 1 else 0)

    # Commit final block
    if current_words:
        blocks.append(_make_block(current_words, current_start))

    # Post-process: merge very short blocks with neighbors
    blocks = _merge_short_blocks(blocks, min_duration)

    # Split blocks into lines if needed
    blocks = _split_into_lines(blocks, max_chars, max_lines)

    return blocks


def _make_block(words, start_time):
    """Create a subtitle block from a list of word timing dicts."""
    text = " ".join(w["word"] for w in words)
    end_time = words[-1]["end"]
    return {
        "text": text,
        "start": start_time,
        "end": end_time,
        "words": words,
    }


def _merge_short_blocks(blocks, min_duration):
    """Merge blocks that are too short with their neighbors."""
    if len(blocks) <= 1:
        return blocks

    merged = []
    i = 0
    while i < len(blocks):
        duration = blocks[i]["end"] - blocks[i]["start"]

        if duration < min_duration and (merged or i + 1 < len(blocks)):
            if merged:
                # Merge with previous
                prev = merged.pop()
                combined_text = prev["text"] + " " + blocks[i]["text"]
                merged.append({
                    "text": combined_text,
                    "start": prev["start"],
                    "end": blocks[i]["end"],
                    "words": prev.get("words", []) + blocks[i].get("words", []),
                })
            elif i + 1 < len(blocks):
                # Merge with next
                combined_text = blocks[i]["text"] + " " + blocks[i + 1]["text"]
                merged.append({
                    "text": combined_text,
                    "start": blocks[i]["start"],
                    "end": blocks[i + 1]["end"],
                    "words": blocks[i].get("words", []) + blocks[i + 1].get("words", []),
                })
                i += 1  # Skip the next one
            else:
                merged.append(blocks[i])
        else:
            merged.append(blocks[i])
        i += 1

    return merged


def _split_into_lines(blocks, max_chars, max_lines):
    """Split blocks into multiple lines if they exceed max_chars per line."""
    result = []
    for block in blocks:
        words = block["text"].split()
        if max_lines == 1 or len(block["text"]) <= max_chars:
            result.append(block)
            continue

        lines = []
        current_line = []
        current_len = 0

        for word in words:
            would_exceed = current_len + len(word) + (1 if current_line else 0) > max_chars
            if would_exceed and current_line and len(lines) < max_lines - 1:
                lines.append(" ".join(current_line))
                current_line = [word]
                current_len = len(word)
            else:
                current_line.append(word)
                current_len += len(word) + (1 if len(current_line) > 1 else 0)

        if current_line:
            lines.append(" ".join(current_line))

        block["text"] = "\n".join(lines)
        result.append(block)

    return result


def _format_timestamp(seconds: float) -> str:
    """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def burn_subtitles(
    clip,
    srt_text: str,
    font: str = "DejaVu-Sans-Bold",
    font_size: int = 40,
    color: str = "white",
    stroke_color: str = "black",
    stroke_width: int = 2,
    position: tuple = ("center", "center"),
    output_size: tuple = (1920, 1080),
):
    """
    Burn subtitles from an SRT string into a MoviePy VideoClip.

    Args:
        clip: MoviePy VideoClip to add subtitles to
        srt_text: SRT-formatted subtitle text
        font: Font name or path
        font_size: Font size in pixels
        color: Text color
        stroke_color: Outline/stroke color
        stroke_width: Outline width
        position: ('center', 'center') or (x_pct, y_pct)
        output_size: (width, height) of the output

    Returns:
        CompositeVideoClip with subtitles burned in
    """
    from moviepy import TextClip, CompositeVideoClip
    import numpy as np

    blocks = parse_srt(srt_text)
    if not blocks:
        return clip

    subtitle_clips = []
    for block in blocks:
        start = block["start"]
        end = block["end"]
        duration = end - start

        # Replace line breaks with actual newlines for TextClip
        text = block["text"].replace("\\n", "\n")

        # Stroke (shadow) layer
        if stroke_width > 0:
            stroke_clip = TextClip(
                text=text,
                font_size=font_size,
                color=stroke_color,
                font=font,
                method="caption",
                size=(int(output_size[0] * 0.9), None),
                stroke_color=stroke_color,
                stroke_width=stroke_width,
            ).with_position(position).with_start(start).with_duration(duration)
            subtitle_clips.append(stroke_clip)

        # Main text layer
        txt = TextClip(
            text=text,
            font_size=font_size,
            color=color,
            font=font,
            method="caption",
            size=(int(output_size[0] * 0.9), None),
        ).with_position(position).with_start(start).with_duration(duration)
        subtitle_clips.append(txt)

    return CompositeVideoClip([clip] + subtitle_clips, size=output_size)


def parse_srt(srt_text: str) -> list:
    """
    Parse SRT text into a list of subtitle blocks.

    Returns list of dicts: {index, start, end, text}
    """
    blocks = []
    lines = srt_text.strip().split("\n")
    i = 0

    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Try to parse as block index
        try:
            index = int(line)
        except ValueError:
            i += 1
            continue

        if i + 1 >= len(lines):
            break

        # Parse timestamp line
        ts_line = lines[i + 1].strip()
        if "-->" not in ts_line:
            i += 1
            continue

        start_str, end_str = ts_line.split("-->")
        start = _parse_timestamp(start_str.strip())
        end = _parse_timestamp(end_str.strip())

        # Collect text lines until blank line or end
        text_lines = []
        j = i + 2
        while j < len(lines) and lines[j].strip():
            text_lines.append(lines[j].strip())
            j += 1

        blocks.append({
            "index": index,
            "start": start,
            "end": end,
            "text": " ".join(text_lines),
        })

        i = j

    return blocks


def _parse_timestamp(ts: str) -> float:
    """Parse SRT timestamp (HH:MM:SS,mmm) to seconds."""
    # Handle both ',' and '.' as millisecond separator
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    if len(parts) == 3:
        h, m, s = parts
        return int(h) * 3600 + int(m) * 60 + float(s)
    raise ValueError(f"Invalid timestamp: {ts}")


def write_srt(srt_text: str, output_path: str):
    """Write SRT text to a file."""
    with open(output_path, "w") as f:
        f.write(srt_text)


def read_srt(input_path: str) -> str:
    """Read SRT text from a file."""
    with open(input_path) as f:
        return f.read()


# Standalone CLI for testing
if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="SRT subtitle generator")
    parser.add_argument("--word-timings", "-w", help="JSON file with word timings")
    parser.add_argument("--output", "-o", default=None, help="Output SRT file path")
    parser.add_argument("--parse", "-p", help="Parse an SRT file and output JSON")
    args = parser.parse_args()

    if args.parse:
        srt = read_srt(args.parse)
        blocks = parse_srt(srt)
        print(json.dumps(blocks, indent=2))
    elif args.word_timings:
        with open(args.word_timings) as f:
            timings = json.load(f)
        srt = generate_srt(timings)
        if args.output:
            write_srt(srt, args.output)
            print(f"Wrote SRT to {args.output}")
        else:
            print(srt)
    else:
        # Demo with sample timings
        demo = [
            {"word": "Hello", "start": 0.0, "end": 0.5},
            {"word": "world,", "start": 0.5, "end": 1.0},
            {"word": "this", "start": 1.2, "end": 1.5},
            {"word": "is", "start": 1.5, "end": 1.7},
            {"word": "a", "start": 1.7, "end": 1.8},
            {"word": "test", "start": 1.8, "end": 2.1},
            {"word": "of", "start": 2.1, "end": 2.3},
            {"word": "the", "start": 2.3, "end": 2.5},
            {"word": "subtitle", "start": 2.5, "end": 3.0},
            {"word": "system.", "start": 3.0, "end": 3.5},
        ]
        srt = generate_srt(demo)
        print(srt)
