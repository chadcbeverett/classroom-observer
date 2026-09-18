"""Extract audio from a classroom observation video."""
import shutil
import subprocess
from pathlib import Path


# Timeouts in seconds. A 90-minute classroom video at real-time decode is
# ~90 min of wall time on a busy shared box; we allow 2x headroom, capped
# so a hung ffmpeg (truncated MP4 missing moov, pathological codec) can't
# starve the single-worker executor for hours. The startup sweep is a
# backstop but only fires on restart — these timeouts fire mid-request.
FFMPEG_EXTRACT_TIMEOUT = 60 * 30   # 30 min for audio extract on a full lesson
FFPROBE_TIMEOUT = 30               # duration probe should take < 2 sec normally


def _scrub_path(text: str, video_path: Path) -> str:
    """Replace the absolute host path with its basename in a stderr blob
    before that blob lands in a DB failure_reason column and eventually the
    coach's UI. ffmpeg always names the input path in its "Input #0, ... from
    '<absolute path>':" header — leaking /Users/<deploy user>/... and the
    internal <obs_id>/<filename> tree to whoever opens the observation.
    """
    if not text:
        return text
    abs_str = str(video_path)
    if abs_str in text:
        text = text.replace(abs_str, video_path.name)
    # Also scrub the parent dir if it happens to appear alone (defensive).
    parent = str(video_path.parent)
    if parent in text:
        text = text.replace(parent, "<uploads>")
    return text


def extract_audio(video_path: Path, output_path: Path) -> Path:
    """Extract mono 16kHz WAV audio from a video file.

    16kHz mono is the format Whisper expects; resampling here avoids doing it
    inside faster-whisper for every call.
    """
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is not installed or not on PATH.")
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path.name}")

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
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=FFMPEG_EXTRACT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"ffmpeg audio extraction hit the {FFMPEG_EXTRACT_TIMEOUT // 60}-min "
            f"timeout on {video_path.name} — likely a truncated file or an unusual "
            f"codec. Re-upload or convert to MP4/H.264 first."
        )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg audio extraction failed:\n"
            + _scrub_path(result.stderr, video_path)
        )
    return output_path


def probe_duration_seconds(video_path: Path) -> float:
    """Return the duration of a video in seconds via ffprobe.

    Falls back to a full-file stream decode when the container header has
    no duration (common with OBS-recorded webm, live captures, MPEG-TS).
    Raises RuntimeError with a scrubbed message on hard failure.
    """
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe is not installed or not on PATH.")

    def _run(cmd):
        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"ffprobe timed out on {video_path.name}. "
                f"The file may be corrupt or missing container metadata."
            )

    # First pass: fast, uses container-header duration.
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result = _run(cmd)
    if result.returncode != 0:
        raise RuntimeError("ffprobe failed:\n" + _scrub_path(result.stderr, video_path))
    raw = (result.stdout or "").strip()
    # ffprobe emits "N/A" for streams without a knowable duration — webm
    # from OBS, live captures, MPEG-TS. Fall through to the slow probe.
    if raw and raw.upper() != "N/A":
        try:
            return float(raw)
        except ValueError:
            pass  # fall through to slow probe

    # Fallback: decode the file's packet timestamps to find the real end.
    # Slower — for a 90-min video this can take ~30 sec — but works on
    # containers that lie about duration.
    slow = [
        "ffprobe",
        "-v", "error",
        "-count_packets",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(video_path),
    ]
    result2 = _run(slow)
    if result2.returncode == 0 and result2.stdout:
        try:
            last = max(
                float(line) for line in result2.stdout.splitlines() if line.strip()
            )
            return last
        except (ValueError, StopIteration):
            pass
    raise RuntimeError(
        f"ffprobe could not determine duration for {video_path.name}. "
        f"The file's container may be missing timing metadata."
    )
