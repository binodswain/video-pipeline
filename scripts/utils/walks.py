"""
Ken Burns effect (slow zoom + pan) for video slides using MoviePy v2.

All MoviePy operations run with resource guards:
- nice -n 19 (lowest CPU priority)
- taskset -c 0,1 (max 2 cores)
- threads=1
- preset='ultrafast' for encoding

Provides:
- ken_burns_clip(): create a single clip with zoom/pan
- crossfade_sequence(): sequence clips with crossfade transitions
- build_video_track(): assemble full video track from slides config
"""

import os
import sys
from pathlib import Path
from typing import Optional


def _resource_guard():
    """Apply CPU resource limits before any heavy operation."""
    # Set niceness
    try:
        os.nice(19)
    except PermissionError:
        pass

    # Set CPU affinity
    pid = os.getpid()
    try:
        os.sched_setaffinity(pid, {0, 1})
    except (OSError, AttributeError, PermissionError):
        pass


def ken_burns_clip(
    image_path,
    duration: float,
    zoom_amt: float = 0.1,
    pan_direction: str = "random",
    output_size: tuple = (1920, 1080),
    fps: int = 24,
) -> "VideoClip":
    """
    Create a single Ken Burns clip from an image.

    Applies smooth zoom from 1.0 to 1.0+zoom_amt and optional pan.

    Args:
        image_path: Path to the image file
        duration: Clip duration in seconds
        zoom_amt: Amount to zoom in (0.0 = no zoom, 0.1 = 10% zoom)
        pan_direction: 'left', 'right', 'up', 'down', 'random', or 'none'
        output_size: (width, height) target resolution
        fps: Frames per second

    Returns:
        MoviePy VideoClip with Ken Burns effect applied
    """
    _resource_guard()

    from moviepy import ImageClip
    import numpy as np

    clip = ImageClip(str(image_path), duration=duration)

    # Resize to fill output size (cover approach: scale to fit height or width, then crop)
    clip = clip.resized(output_size)

    w, h = output_size

    # Determine pan direction
    if pan_direction == "random":
        import random
        pan_direction = random.choice(["left", "right", "up", "down", "none"])

    # Build the zoom+pan effect
    def make_frame_transform(t):
        """Return a function that applies zoom+pan at time t."""
        progress = t / duration if duration > 0 else 1.0

        # Zoom: linear from 1.0 to 1.0+zoom_amt
        current_zoom = 1.0 + zoom_amt * progress

        # Pan offset
        pan_x = 0
        pan_y = 0

        if pan_direction == "left":
            pan_x = -zoom_amt * w * 0.3 * progress
        elif pan_direction == "right":
            pan_x = zoom_amt * w * 0.3 * progress
        elif pan_direction == "up":
            pan_y = -zoom_amt * h * 0.3 * progress
        elif pan_direction == "down":
            pan_y = zoom_amt * h * 0.3 * progress

        return current_zoom, pan_x, pan_y

    if zoom_amt > 0 or pan_direction != "none":
        clip = clip.with_effects([
            _ZoomPanEffect(make_frame_transform, output_size)
        ])

    return clip


class _ZoomPanEffect:
    """Custom MoviePy effect for smooth zoom+pan with sub-pixel accuracy."""

    def __init__(self, transform_fn, output_size):
        self.transform_fn = transform_fn
        self.output_size = output_size

    def copy(self):
        return _ZoomPanEffect(self.transform_fn, self.output_size)

    def __call__(self, clip):
        """Apply the zoom-pan transformation to the clip."""
        from moviepy import VideoClip
        import numpy as np
        from PIL import Image

        w, h = self.output_size

        def make_frame(t):
            frame = clip.get_frame(t)
            img = Image.fromarray(frame)

            zoom, pan_x, pan_y = self.transform_fn(t)

            # Calculate crop region in source coordinates
            src_w = w / zoom
            src_h = h / zoom
            src_x = (w - src_w) / 2 + pan_x / zoom
            src_y = (h - src_h) / 2 + pan_y / zoom

            # Ensure crop stays within bounds
            src_x = max(0, min(w - src_w, src_x))
            src_y = max(0, min(h - src_h, src_y))

            # Crop and resize back to output
            crop = img.crop((
                int(src_x), int(src_y),
                int(src_x + src_w), int(src_y + src_h)
            ))
            resized = crop.resize((w, h), Image.LANCZOS)
            return np.array(resized)

        return clip.with_updated_frame_function(make_frame)


def crossfade_sequence(
    clips: list,
    crossfade_duration: float = 0.5,
    fps: int = 24,
):
    """
    Sequence multiple clips with crossfade transitions.

    Args:
        clips: List of (clip, duration) tuples — actual clip and its intended solo duration
        crossfade_duration: Duration of the crossfade transition in seconds
        fps: Frames per second

    Returns:
        Single concatenated VideoClip with crossfades
    """
    from moviepy import concatenate_videoclips, CompositeVideoClip

    if len(clips) == 0:
        raise ValueError("No clips provided for sequence")
    if len(clips) == 1:
        return clips[0]

    # For crossfades, we need to overlap clips and fade opacity
    # Each clip's effective duration = solo_duration + crossfade_duration (except last)
    processed_clips = []
    total_duration = 0.0

    for i, clip in enumerate(clips):
        solo_duration = clip.duration

        if i < len(clips) - 1:
            # Extend this clip to overlap with the next
            clip = clip.with_duration(solo_duration + crossfade_duration)

        # Add fade-in and fade-out
        if i > 0:
            clip = clip.with_effects([
                _CrossFadeIn(crossfade_duration)
            ])
        if i < len(clips) - 1:
            clip = clip.with_effects([
                _CrossFadeOut(crossfade_duration)
            ])

        clip = clip.with_start(total_duration)
        processed_clips.append(clip)
        total_duration += solo_duration

    final = CompositeVideoClip(processed_clips, size=clips[0].size)
    final = final.with_duration(total_duration)
    return final


class _CrossFadeIn:
    """Fade in from transparent at the start of a clip."""

    def __init__(self, duration):
        self.duration = duration

    def __call__(self, clip):
        import numpy as np

        def make_frame(t):
            frame = clip.get_frame(t)
            if t < self.duration:
                alpha = t / self.duration
                frame = (frame * alpha).astype(np.uint8)
            return frame

        return clip.with_updated_frame_function(make_frame)


class _CrossFadeOut:
    """Fade out to transparent at the end of a clip."""

    def __init__(self, duration):
        self.duration = duration

    def __call__(self, clip):
        import numpy as np

        def make_frame(t):
            frame = clip.get_frame(t)
            remaining = clip.duration - t
            if remaining < self.duration:
                alpha = remaining / self.duration
                frame = (frame * alpha).astype(np.uint8)
            return frame

        return clip.with_updated_frame_function(make_frame)


def build_video_track(
    slides: list,
    output_size: tuple = (1920, 1080),
    fps: int = 24,
    crossfade_duration: float = 0.5,
) -> "VideoClip":
    """
    Build the full video track from a slide configuration.

    Each slide dict:
        {
            "path": str,            # Path to image
            "duration": float,      # Duration in seconds
            "zoom_amt": float,      # Optional zoom amount (default 0.08)
            "pan_direction": str,   # Optional pan direction
            "text_overlay": str,    # Optional text to burn in
            "type": str,            # "ai" or "real" (metadata)
        }

    Args:
        slides: List of slide configuration dicts
        output_size: (width, height) in pixels
        fps: Frames per second
        crossfade_duration: Cross-fade transition time

    Returns:
        MoviePy VideoClip — the full video track
    """
    _resource_guard()

    from moviepy import ImageClip

    clips = []
    for slide in slides:
        path = slide["path"]
        duration = float(slide.get("duration", 5.0))
        zoom_amt = float(slide.get("zoom_amt", 0.08))
        pan = slide.get("pan_direction", "random")

        clip = ken_burns_clip(
            path,
            duration=duration,
            zoom_amt=zoom_amt,
            pan_direction=pan,
            output_size=output_size,
            fps=fps,
        )

        # Burn text overlay if present
        text = slide.get("text_overlay", "")
        if text:
            clip = _burn_text_overlay(clip, text, output_size)

        clips.append(clip)

    return crossfade_sequence(clips, crossfade_duration, fps)


def _burn_text_overlay(clip, text: str, output_size: tuple):
    """
    Burn text overlay onto a clip (centered bottom, white text with dark shadow).
    """
    from moviepy import TextClip, CompositeVideoClip

    w, h = output_size
    font_size = int(h * 0.045)

    # Shadow layer
    txt_shadow = TextClip(
        text=text,
        font_size=font_size,
        color="black",
        font="DejaVu-Sans-Bold",
        method="caption",
        size=(int(w * 0.85), None),
    ).with_position(("center", int(h * 0.82))).with_duration(clip.duration)

    # Main text layer
    txt_main = TextClip(
        text=text,
        font_size=font_size,
        color="white",
        font="DejaVu-Sans-Bold",
        method="caption",
        size=(int(w * 0.85), None),
    ).with_position(("center", int(h * 0.82) - 2)).with_duration(clip.duration)

    return CompositeVideoClip([clip, txt_shadow, txt_main], size=output_size)


# Standalone CLI for testing
if __name__ == "__main__":
    import argparse
    import tempfile

    parser = argparse.ArgumentParser(description="Ken Burns video track builder")
    parser.add_argument("image", help="Test image path")
    parser.add_argument("--output", "-o", default=None, help="Output video path")
    parser.add_argument("--duration", "-d", type=float, default=5.0)
    parser.add_argument("--zoom", "-z", type=float, default=0.1)
    args = parser.parse_args()

    if args.output is None:
        fd, args.output = tempfile.mkstemp(suffix=".mp4", prefix="kenburns_")
        os.close(fd)

    slides = [{
        "path": args.image,
        "duration": args.duration,
        "zoom_amt": args.zoom,
        "pan_direction": "right",
        "text_overlay": "Ken Burns Test Clip",
    }]

    print(f"Building Ken Burns clip: {args.image} -> {args.output}", file=sys.stderr)
    clip = build_video_track(slides)

    clip.write_videofile(
        args.output,
        fps=24,
        codec="libx264",
        preset="ultrafast",
        threads=1,
        audio=False,
    )
    print(f"Output: {args.output}")
