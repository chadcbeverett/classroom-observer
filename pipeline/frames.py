"""Sample frames from a classroom observation video."""
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List


# Frame extraction on a 90-min lesson at 1fpm produces ~90 JPEGs and
# typically finishes in seconds; 30 min is generous headroom for a
# pathological codec that seeks slowly.
FFMPEG_FRAMES_TIMEOUT = 60 * 30


def _scrub_path(text: str, video_path: Path) -> str:
    """Same scrub as pipeline.audio — keep the deploy user's home dir and
    the internal uploads tree out of failure_reason strings that end up
    in the coach UI.
    """
    if not text:
        return text
    abs_str = str(video_path)
    if abs_str in text:
        text = text.replace(abs_str, video_path.name)
    parent = str(video_path.parent)
    if parent in text:
        text = text.replace(parent, "<uploads>")
    return text


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
        raise FileNotFoundError(f"Video not found: {video_path.name}")
    if interval_seconds <= 0:
        raise ValueError(f"interval_seconds must be > 0 (got {interval_seconds})")

    output_dir.mkdir(parents=True, exist_ok=True)
    # Clean up stale frame_*.jpg from any prior run in the same output_dir.
    # ffmpeg -y overwrites frame_0001..N.jpg but does NOT delete frame_N+1..
    # from an earlier, longer run — leaving stale frames that the glob-based
    # timestamp math would then absorb with fabricated timestamps past the
    # real video end. The current caller uses per-UUID dirs so this rarely
    # fires, but a retry / resume / manual re-run against the same obs_id
    # would trip it. Cleanup is cheap and belt-and-suspenders.
    for stale in output_dir.glob("frame_*.jpg"):
        try:
            stale.unlink()
        except OSError:
            pass
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
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_FRAMES_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg frame extraction hit the {FFMPEG_FRAMES_TIMEOUT // 60}-min "
            f"timeout on {video_path.name}. Likely a corrupt or unusual container."
        )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg frame extraction failed:\n"
            + _scrub_path(result.stderr, video_path)
        )

    frames: List[SampledFrame] = []
    for i, path in enumerate(sorted(output_dir.glob("frame_*.jpg"))):
        # ffmpeg's fps filter places frame i at time i * interval_seconds (approximately).
        # First frame lands at t=0 with this filter.
        ts = i * interval_seconds
        frames.append(SampledFrame(timestamp_seconds=ts, path=path))
    return frames
