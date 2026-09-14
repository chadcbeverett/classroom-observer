"""Transcribe audio with faster-whisper, producing timestamped segments."""
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class TranscriptSegment:
    start: float  # seconds
    end: float    # seconds
    text: str
    speaker: Optional[str] = None  # reserved for future diarization


def _fmt_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def transcribe(audio_path: Path, model_size: str = "small") -> List[TranscriptSegment]:
    """Run faster-whisper on the audio file and return timestamped segments.

    model_size options (speed vs. accuracy tradeoff):
      - tiny / base: fastest, lower accuracy
      - small (default): good balance for CPU
      - medium / large-v3: highest accuracy, GPU recommended
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc

    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segments = [
        TranscriptSegment(start=s.start, end=s.end, text=s.text.strip())
        for s in segments_iter
    ]
    return segments


def to_dialogue_text(segments: List[TranscriptSegment]) -> str:
    """Format segments as a single string with [MM:SS] timestamps for use in prompts."""
    lines = []
    for seg in segments:
        lines.append(f"[{_fmt_timestamp(seg.start)}] {seg.text}")
    return "\n".join(lines)


def serialize(segments: List[TranscriptSegment]) -> list:
    """JSON-serializable list of segments."""
    return [asdict(s) for s in segments]
