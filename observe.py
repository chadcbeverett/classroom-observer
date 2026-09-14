#!/usr/bin/env python3
"""Classroom observation → scored rubric + coaching narrative.

Rubric-agnostic: takes a rubric id (default TNTP Core 4-point 2014) and produces
a structured assessment plus a rubric-aligned coaching report.

Usage:
    python observe.py path/to/video.mp4
    python observe.py path/to/video.mp4 --rubric tntp_core_4pt_2014 --out reports/jane
    python observe.py path/to/video.mp4 --reuse --whisper-model medium

What it does:
    1. Extract mono 16kHz WAV audio from the video (ffmpeg).
    2. Transcribe with faster-whisper, producing timestamped segments.
    3. Sample one frame per minute (ffmpeg, resized to <=1568px long edge).
    4. Send rubric PDF (cached) + transcript + frames to Claude Opus 4.7
       with adaptive thinking; parse the response into a typed
       ObservationReport with rubric-specific validation.
    5. Write three files to the output directory:
         - scores.json      structured rubric scores + coaching recs
         - report.md        rubric-aligned narrative coaching report
         - transcript.json  timestamped transcript (for reference)

Requirements:
    - ffmpeg on PATH
    - ANTHROPIC_API_KEY env var
    - pip install -r requirements.txt
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from pipeline.audio import extract_audio, probe_duration_seconds
from pipeline.frames import SampledFrame, sample_frames
from pipeline.rubric import DEFAULT_RUBRIC_ID, RUBRICS, Rubric, get_rubric
from pipeline.schemas import ObservationReport
from pipeline.score import score_observation
from pipeline.transcribe import TranscriptSegment, serialize, transcribe


def _report_to_dict_with_scores(report: ObservationReport, rubric: Rubric) -> dict:
    """Serialize the report and inject derived integer scores everywhere a rating appears."""
    data = report.model_dump()
    for da in data["domain_assessments"]:
        da["overall_score"] = rubric.score_for(da["overall_rating"])
        for d in da["descriptor_scores"]:
            d["score"] = rubric.score_for(d["rating"])
    return data


def _compose_report_markdown(
    report: ObservationReport, teacher_name: str, rubric: Rubric
) -> str:
    """Compose the final coaching report from the structured fields.

    Structure mirrors the selected rubric's own organization: one section per
    performance area, each opened with the Essential Question, anchored in the
    rubric's exact descriptor language, followed by what was observed (in rubric
    language), the distance from the next rating level, and the coaching-construct
    entries implicated.
    """
    assessments_by_domain = {da.domain: da for da in report.domain_assessments}

    out = []
    out.append(f"# Observation Report — {teacher_name}")
    out.append(f"*Scored against: {rubric.name}*")
    out.append("")
    out.append(report.opening_paragraph.strip())
    out.append("")
    out.append("## Overall")
    out.append(report.overall_summary.strip())
    out.append("")
    out.append("---")
    out.append("")

    for domain in rubric.domains:
        da = assessments_by_domain[domain]
        score = rubric.score_for(da.overall_rating)
        out.append(f"## {domain} — {da.overall_rating} ({score}/{rubric.num_levels})")
        out.append(f"**Essential Question:** {da.essential_question.strip()}")
        out.append("")
        out.append(f"**Rubric descriptor at {da.overall_rating}:**")
        out.append(f"> {da.rubric_descriptor_text.strip()}")
        out.append("")
        out.append("**What was observed:**")
        out.append(da.what_was_observed.strip())
        out.append("")
        out.append(f"**Distance from target:** {da.distance_from_target.strip()}")
        out.append("")
        if da.relevant_core_teacher_skills:
            out.append("**Coaching-construct entries implicated:**")
            for skill in da.relevant_core_teacher_skills:
                out.append(f"- {skill}")
            out.append("")
        out.append(f"*Preponderance breakdown:* {da.preponderance_summary.strip()}")
        out.append("")
        out.append("---")
        out.append("")

    out.append("## Coaching Priorities")
    out.append("")
    for i, rec in enumerate(report.coaching_recommendations, 1):
        out.append(f"### Priority {i}: {rec.core_teacher_skill}")
        out.append(f"**Domain:** {rec.related_domain}")
        out.append("")
        out.append(f"**Why:** {rec.rationale.strip()}")
        out.append("")
        out.append(f"**Bite-sized action this week:** {rec.bite_sized_action.strip()}")
        out.append("")
        if rec.success_indicators:
            out.append("**Success indicators next visit:**")
            for indicator in rec.success_indicators:
                out.append(f"- {indicator}")
            out.append("")

    return "\n".join(out)


def _print_step(step: str) -> None:
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] {step}", flush=True)


def run(
    video_path: Path,
    rubric: Rubric,
    out_dir: Path,
    whisper_model: str,
    frame_interval: float,
    keep_intermediate: bool,
    reuse: bool,
) -> Path:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(2)
    if not video_path.exists():
        print(f"ERROR: video not found: {video_path}", file=sys.stderr)
        sys.exit(2)
    if not rubric.pdf_path.exists():
        print(f"ERROR: rubric PDF not found: {rubric.pdf_path}", file=sys.stderr)
        sys.exit(2)

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir = out_dir / "_work"
    work_dir.mkdir(exist_ok=True)
    transcript_path = out_dir / "transcript.json"
    frames_dir = work_dir / "frames"

    _print_step(f"Probing video: {video_path.name}")
    duration_s = probe_duration_seconds(video_path)
    print(f"  Duration: {int(duration_s // 60):02d}:{int(duration_s % 60):02d}")

    have_transcript = reuse and transcript_path.exists()
    have_frames = reuse and frames_dir.exists() and any(frames_dir.glob("frame_*.jpg"))

    if have_transcript:
        _print_step("Reusing cached transcript (--reuse)")
        segments = [
            TranscriptSegment(**s) for s in json.loads(transcript_path.read_text())
        ]
        print(f"  Segments: {len(segments)}")
    else:
        _print_step("Extracting audio (ffmpeg)")
        audio_path = extract_audio(video_path, work_dir / "audio.wav")
        print(f"  Wrote: {audio_path}")

        _print_step(f"Transcribing with faster-whisper (model={whisper_model})")
        segments = transcribe(audio_path, model_size=whisper_model)
        transcript_path.write_text(json.dumps(serialize(segments), indent=2))
        print(f"  Segments: {len(segments)}")
        print(f"  Wrote: {transcript_path}")

    if have_frames:
        _print_step("Reusing cached frames (--reuse)")
        frames = [
            SampledFrame(timestamp_seconds=i * frame_interval, path=p)
            for i, p in enumerate(sorted(frames_dir.glob("frame_*.jpg")))
        ]
        print(f"  Frames: {len(frames)}")
    else:
        _print_step(f"Sampling frames (one per {frame_interval:.0f}s, ffmpeg)")
        frames = sample_frames(video_path, frames_dir, interval_seconds=frame_interval)
        print(f"  Frames: {len(frames)}")

    _print_step(f"Scoring against {rubric.name} (Claude Opus 4.7, adaptive thinking)")
    report = score_observation(
        rubric=rubric,
        transcript=segments,
        frames=frames,
        video_filename=video_path.name,
        duration_seconds=duration_s,
    )

    # Wrap the typed report with runtime metadata (including rubric_id) so old
    # outputs remain interpretable if a rubric evolves.
    metadata = {
        "rubric_id": rubric.id,
        "rubric_name": rubric.name,
        "video_filename": video_path.name,
        "duration_seconds": duration_s,
        "transcription_model": f"faster-whisper:{whisper_model}",
        "frame_count": len(frames),
        "frame_interval_seconds": frame_interval,
        "scored_at": datetime.now().isoformat(timespec="seconds"),
    }
    scores_path = out_dir / "scores.json"
    scores_path.write_text(json.dumps(
        {"metadata": metadata, "report": _report_to_dict_with_scores(report, rubric)},
        indent=2,
    ))
    print(f"\n  Wrote: {scores_path}")

    teacher_name = out_dir.name.split("_")[0].replace("-", " ").title()
    report_path = out_dir / "report.md"
    report_path.write_text(_compose_report_markdown(report, teacher_name, rubric))
    print(f"  Wrote: {report_path}")

    if not keep_intermediate:
        audio_path = work_dir / "audio.wav"
        if audio_path.exists():
            audio_path.unlink()
            print(f"  Cleaned: {audio_path}")

    print("\n=== Summary ===")
    for da in report.domain_assessments:
        print(f"  {da.domain:30s} {da.overall_rating:20s} "
              f"({rubric.score_for(da.overall_rating)}/{rubric.num_levels})")
    print("\n=== Coaching priorities ===")
    for rec in report.coaching_recommendations:
        print(f"  - {rec.core_teacher_skill}  ({rec.related_domain})")
        print(f"    Action this week: {rec.bite_sized_action}")

    return out_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Score a classroom observation video against a teaching rubric.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", type=Path, help="Path to the observation video (mp4, mov, etc.).")
    parser.add_argument(
        "--rubric", default=DEFAULT_RUBRIC_ID,
        choices=sorted(RUBRICS.keys()),
        help="Rubric id to score against. Add new rubrics in pipeline/rubric.py.",
    )
    parser.add_argument(
        "--out", type=Path,
        default=Path("reports") / datetime.now().strftime("%Y%m%d_%H%M%S"),
        help="Output directory.",
    )
    parser.add_argument(
        "--whisper-model", default="medium",
        choices=["tiny", "base", "small", "medium", "large-v3"],
        help="faster-whisper model size. Larger = more accurate but slower. "
             "`medium` is the default — it catches negations and hallucinations that "
             "`small` misses (see docs/data_model.md background). Use `small` on very "
             "constrained hardware; `large-v3` for the highest accuracy.",
    )
    parser.add_argument(
        "--frame-interval", type=float, default=60.0,
        help="Seconds between sampled frames.",
    )
    parser.add_argument(
        "--keep-intermediate", action="store_true",
        help="Keep extracted audio and frames in <out>/_work after scoring.",
    )
    parser.add_argument(
        "--reuse", action="store_true",
        help="If <out> already has a transcript and frames, reuse them and skip transcription/sampling.",
    )
    args = parser.parse_args()

    rubric = get_rubric(args.rubric)

    out = run(
        video_path=args.video,
        rubric=rubric,
        out_dir=args.out,
        whisper_model=args.whisper_model,
        frame_interval=args.frame_interval,
        keep_intermediate=args.keep_intermediate,
        reuse=args.reuse,
    )
    print(f"\nDone. Outputs in: {out}")


if __name__ == "__main__":
    main()
