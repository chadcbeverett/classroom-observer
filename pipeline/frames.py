"""Sample frames from a classroom observation video."""
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class SampledFrame:
    timestamp_seconds: float
    path: Path

    @property
    def timestamp_label(self) -> str:
        minutes, secs = divmod(int(self.timestamp_seconds), 60)
        return f"{minutes:02d}:{secs:02d}"


def sample_frames(
    video_path: Path,
    output_dir: Path,
    interval_seconds: float = 60.0,
    max_long_edge_px: int = 1568,
) -> List[SampledFrame]:
    """Extract one JPEG every `interval_seconds`, resizing to keep tokens reasonable.

    Defaults to one frame per minute → ~20-25 frames for a typical observation.
    Resizes so the long edge is <= 1568px (Claude's vision sweet spot pre-Opus-4.7).
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    fps = 1.0 / interval_seconds
    pattern = output_dir / "frame_%04d.jpg"

    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-vf", f"fps={fps},scale='min({max_long_edge_px},iw)':'-2'",
        "-q:v", "3",  # JPEG quality: 1 (best) – 31 (worst); 3 is high quality
        str(pattern),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed:\n{result.stderr}")

    frames: List[SampledFrame] = []
    for i, path in enumerate(sorted(output_dir.glob("frame_*.jpg"))):
        # ffmpeg's fps filter places frame i at time i * interval_seconds (approximately).
        # First frame lands at t=0 with this filter.
        ts = i * interval_seconds
        frames.append(SampledFrame(timestamp_seconds=ts, path=path))
    return frames
