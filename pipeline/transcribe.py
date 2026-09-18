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


# Belt-and-suspenders bounds for transcribe(). faster-whisper has no
# built-in wall-clock timeout, and a pathological input (quiet audio that
# looks like speech, or a codec residue that survives audio.py's resample)
# can cascade the model into unbounded hallucinated segments. If the
# transcript exceeds either bound we bail with a clear failure_reason
# instead of freezing the worker or handing the AI 50 000 gibberish lines.
MAX_TRANSCRIPT_SEGMENTS = 8000     # ~8000 segments = ~2h of dense classroom talk
MAX_TRANSCRIPT_WALL_SECONDS = 60 * 45   # 45 min hard cap on transcription wall time


def _fmt_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes:02d}:{secs:02d}"


def transcribe(
    audio_path: Path,
    model_size: str = "small",
    language: str = "en",
) -> List[TranscriptSegment]:
    """Run faster-whisper on the audio file and return timestamped segments.

    model_size options (speed vs. accuracy tradeoff):
      - tiny / base: fastest, lower accuracy
      - small (default): good balance for CPU
      - medium / large-v3: highest accuracy, GPU recommended

    ``language`` defaults to English. faster-whisper's auto-detect uses a
    short probe window; on classroom audio that opens with music, PE class
    noise, or extended silence it can misclassify as Chinese / Welsh /
    Nynorsk and transcribe English speech into hallucinated tokens of that
    language. The coaching product assumes English throughout — the AI
    prompt, the rubric, and every downstream template all render English —
    so we pin the language and refuse to let a bad probe silently poison
    the transcript. Callers with a non-English classroom should pass
    ``language=`` explicitly (and every downstream surface will need work
    to match).
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ImportError(
            "faster-whisper not installed. Run: pip install faster-whisper"
        ) from exc

    import time as _t
    model = WhisperModel(model_size, device="auto", compute_type="auto")
    segments_iter, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        language=language,
        # condition_on_previous_text=False stops the model from feeding its
        # own prior output back as context — the single biggest source of
        # cascade hallucination in Whisper transcripts.
        condition_on_previous_text=False,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    segments: List[TranscriptSegment] = []
    _t0 = _t.monotonic()
    for s in segments_iter:
        # Belt-and-suspenders: cap wall time and segment count. faster-
        # whisper streams lazily — the elapsed check fires each iteration
        # so a hung generator surfaces as a clean RuntimeError instead of
        # a wedged worker thread that only the startup sweep can reclaim.
        if (_t.monotonic() - _t0) > MAX_TRANSCRIPT_WALL_SECONDS:
            raise RuntimeError(
                f"Transcription exceeded the {MAX_TRANSCRIPT_WALL_SECONDS // 60}-min "
                f"wall-time cap on {audio_path.name}. Likely an unusually long "
                f"audio, a slow CPU without GPU acceleration, or a pathological "
                f"input causing runaway segmentation."
            )
        if len(segments) >= MAX_TRANSCRIPT_SEGMENTS:
            raise RuntimeError(
                f"Transcription produced >{MAX_TRANSCRIPT_SEGMENTS} segments — "
                f"almost certainly runaway hallucination. Bailing before it "
                f"reaches the AI scoring prompt."
            )
        segments.append(TranscriptSegment(start=s.start, end=s.end, text=s.text.strip()))
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
