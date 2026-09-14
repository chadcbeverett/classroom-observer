"""Extract audio from a classroom observation video."""
import shutil
import subprocess
from pathlib import Path


def extract_audio(video_path: Path, output_path: Path) -> Path:
    """Extract mono 16kHz WAV audio from a video file.

    16kHz mono is the format Whisper expects; resampling here avoids doing it
    inside faster-whisper for every call.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed:\n{result.stderr}")
    return output_path


def probe_duration_seconds(video_path: Path) -> float:
    """Return the duration of a video in seconds via ffprobe."""
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed or not on PATH.")
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed:\n{result.stderr}")
    return float(result.stdout.strip())
