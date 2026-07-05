"""
Kokoro TTS wrapper for on-demand video narration.

Each invocation: load model -> synthesize -> unload -> free memory.
Model cached in ~/.cache/video-pipeline/kokoro/.
Supports word-level timings for subtitle synchronization.
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# Cache directory for Kokoro model
KOKORO_CACHE = Path.home() / ".cache" / "video-pipeline" / "kokoro"
KOKORO_CACHE.mkdir(parents=True, exist_ok=True)


class KokoroTTS:
    """
    Wrapper around Kokoro-82M TTS model.
    Loads/synthesizes/unloads per call — no persistent state.
    """

    def __init__(self, voice: str = "af_heart", lang: str = "en-us"):
        """
        Args:
            voice: Voice preset name (e.g. 'af_heart', 'am_adam', 'bf_emma')
            lang: Language code (en-us, en-gb, etc.)
        """
        self.voice = voice
        self.lang = lang
        self._pipeline = None
        self._model_loaded = False

    def load(self):
        """Load the Kokoro model into memory. Called once per build."""
        if self._model_loaded:
            return

        try:
            from kokoro import KPipeline

            self._pipeline = KPipeline(lang_code=self.lang)
            self._model_loaded = True
            print(f"[TTS] Kokoro model loaded (voice={self.voice}, lang={self.lang})",
                  file=sys.stderr)
        except ImportError:
            raise RuntimeError(
                "Kokoro package not installed. Install with: pip install kokoro"
            )
        except Exception as e:
            raise RuntimeError(f"Failed to load Kokoro model: {e}")

    def unload(self):
        """Release the Kokoro model from memory."""
        if self._pipeline is not None:
            del self._pipeline
            self._pipeline = None
        self._model_loaded = False
        import gc
        gc.collect()
        print("[TTS] Kokoro model unloaded from memory", file=sys.stderr)

    def synthesize(
        self,
        text: str,
        output_path: Optional[str] = None,
        speed: float = 1.0,
    ) -> dict:
        """
        Synthesize speech from text. Returns dict with audio path + word timings.

        Args:
            text: Narration text to synthesize
            output_path: Where to save the WAV/MP3. Auto-generated if None.
            speed: Playback speed multiplier (1.0 = normal)

        Returns:
            dict with keys:
                - audio_path: str — path to generated audio file
                - duration_seconds: float — total audio duration
                - word_timings: list of {word, start, end} dicts
                - sample_rate: int
        """
        if not self._model_loaded:
            self.load()

        if output_path is None:
            import tempfile
            fd, output_path = tempfile.mkstemp(suffix=".wav", prefix="kokoro_")
            os.close(fd)

        # Split text into manageable chunks (Kokoro handles ~500 chars well)
        chunks = self._split_text(text, max_chars=400)
        all_audio = []
        all_timings = []
        sample_rate = 24000

        time_offset = 0.0

        for chunk_idx, chunk in enumerate(chunks):
            generator = self._pipeline(
                chunk,
                voice=self.voice,
                speed=speed,
                split_pattern=r"\n+|(?<=[.!?])\s+",
            )

            chunk_audio_segments = []
            for gs, ps, audio in generator:
                if audio is None:
                    continue
                chunk_audio_segments.append(audio)
                duration = len(audio) / sample_rate

                # Extract words from the grapheme/phoneme segments
                words = self._extract_words(gs)
                if words:
                    word_timings = self._distribute_timings(
                        words, time_offset, duration
                    )
                    all_timings.extend(word_timings)
                    time_offset += duration

            if chunk_audio_segments:
                import numpy as np
                chunk_audio = np.concatenate(chunk_audio_segments)
                all_audio.append(chunk_audio)

        if not all_audio:
            raise RuntimeError("Kokoro produced no audio output for the given text")

        import numpy as np
        import soundfile as sf

        full_audio = np.concatenate(all_audio)
        total_duration = len(full_audio) / sample_rate

        # Write audio file
        sf.write(output_path, full_audio, sample_rate)

        result = {
            "audio_path": output_path,
            "duration_seconds": round(total_duration, 3),
            "word_timings": all_timings,
            "sample_rate": sample_rate,
        }

        print(
            f"[TTS] Synthesized {total_duration:.1f}s audio to {output_path} "
            f"({len(all_timings)} words timed)",
            file=sys.stderr,
        )
        return result

    def synthesize_segments(
        self,
        segments: list,
        output_dir: Optional[str] = None,
    ) -> list:
        """
        Synthesize multiple script segments, returning per-segment audio + timings.

        Each segment should be a dict with at least: {'text': str, 'start_time': float}

        Returns list of dicts matching input segments + audio_path and word_timings.
        """
        if output_dir is None:
            import tempfile
            output_dir = tempfile.mkdtemp(prefix="kokoro_segments_")

        results = []
        cumulative_offset = 0.0

        for i, segment in enumerate(segments):
            text = segment.get("text", "")
            if not text.strip():
                # Silent segment — still track timing
                results.append({
                    **segment,
                    "audio_path": None,
                    "duration_seconds": segment.get("end_time", 0) - segment.get("start_time", 0),
                    "word_timings": [],
                })
                continue

            seg_path = os.path.join(output_dir, f"segment_{i:04d}.wav")
            result = self.synthesize(text, output_path=seg_path)
            results.append({
                **segment,
                "audio_path": result["audio_path"],
                "duration_seconds": result["duration_seconds"],
                "word_timings": result["word_timings"],
            })
            cumulative_offset += result["duration_seconds"]

        return results

    def _split_text(self, text: str, max_chars: int = 400) -> list:
        """Split text into chunks that Kokoro can handle."""
        if len(text) <= max_chars:
            return [text]

        # Split on sentence boundaries
        import re
        sentences = re.split(r"(?<=[.!?])\s+", text)
        chunks = []
        current = ""

        for sentence in sentences:
            if len(current) + len(sentence) + 1 <= max_chars:
                current = (current + " " + sentence).strip() if current else sentence
            else:
                if current:
                    chunks.append(current)
                # If a single sentence exceeds max_chars, split on word boundaries
                if len(sentence) > max_chars:
                    words = sentence.split()
                    sub_chunk = ""
                    for word in words:
                        if len(sub_chunk) + len(word) + 1 <= max_chars:
                            sub_chunk = (sub_chunk + " " + word).strip() if sub_chunk else word
                        else:
                            chunks.append(sub_chunk)
                            sub_chunk = word
                    if sub_chunk:
                        current = sub_chunk
                    else:
                        current = ""
                else:
                    current = sentence

        if current:
            chunks.append(current)

        return chunks

    def _extract_words(self, graphemes: str) -> list:
        """Extract words from grapheme text."""
        if not graphemes:
            return []
        return graphemes.strip().split()

    def _distribute_timings(
        self, words: list, start_offset: float, total_duration: float
    ) -> list:
        """
        Distribute word timings evenly across the segment duration.
        This is an approximation — Kokoro doesn't provide per-word timings natively.
        For higher precision, a forced-alignment step could be added.
        """
        if not words:
            return []

        word_count = len(words)
        word_duration = total_duration / word_count

        timings = []
        for i, word in enumerate(words):
            start = start_offset + i * word_duration
            end = start + word_duration
            timings.append({
                "word": word,
                "start": round(start, 3),
                "end": round(end, 3),
            })

        return timings

    def __enter__(self):
        self.load()
        return self

    def __exit__(self, *args):
        self.unload()


# Standalone CLI for testing
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Kokoro TTS CLI test")
    parser.add_argument("text", nargs="?", default="Hello world. This is a test of the Kokoro TTS system.")
    parser.add_argument("--voice", default="af_heart")
    parser.add_argument("--output", "-o", default=None)
    args = parser.parse_args()

    with KokoroTTS(voice=args.voice) as tts:
        result = tts.synthesize(args.text, output_path=args.output)
        print(json.dumps({
            "audio_path": result["audio_path"],
            "duration_seconds": result["duration_seconds"],
            "sample_rate": result["sample_rate"],
            "word_count": len(result["word_timings"]),
        }, indent=2))
