"""In-process background job runner for the local FastAPI app.

Single-worker ThreadPoolExecutor. Adequate for one-user-on-one-Mac. For a
hosted multi-user deployment, replace with RQ + Redis (the ``submit_job``
interface stays the same).

Each job runs the full pipeline against an ``observations`` row identified by
its DB id, updating ``status`` at each phase so the UI can poll for progress.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.audio import extract_audio, probe_duration_seconds
from pipeline.context import assemble_teacher_context, render_context_for_prompt
from pipeline.db import connect as db_connect, record_bite_sized_action
from pipeline.frames import sample_frames
from pipeline.rubric import get_rubric
from pipeline.score import score_observation
from pipeline.transcribe import serialize, transcribe

_executor: Optional[ThreadPoolExecutor] = None


def get_executor() -> ThreadPoolExecutor:
    """Lazily construct the shared single-worker executor."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="observer-job")
    return _executor


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _set_status(db_path: Path, observation_id: str, status: str,
                failure_reason: Optional[str] = None) -> None:
    conn = db_connect(db_path)
    try:
        conn.execute(
            "UPDATE observations SET status = ?, failure_reason = ? WHERE id = ?",
            (status, failure_reason, observation_id),
        )
        conn.commit()
    finally:
        conn.close()


def submit_job(
    db_path: Path,
    observation_id: str,
    video_path: Path,
    rubric_id: str,
    whisper_model: str = "medium",
    frame_interval: float = 60.0,
) -> None:
    """Queue a scoring job for an existing observation row.

    Fire-and-forget from the caller's perspective. UI polls
    ``GET /observations/{id}/status`` for progress.
    """
    get_executor().submit(
        _run_job,
        db_path, observation_id, video_path, rubric_id,
        whisper_model, frame_interval,
    )


def _run_job(
    db_path: Path,
    observation_id: str,
    video_path: Path,
    rubric_id: str,
    whisper_model: str,
    frame_interval: float,
) -> None:
    """The actual pipeline runner — invoked on the executor thread."""
    work_dir = video_path.parent / f"_work_{observation_id}"
    work_dir.mkdir(exist_ok=True, parents=True)
    try:
        rubric = get_rubric(rubric_id)

        # Probe
        _set_status(db_path, observation_id, "transcribing")
        duration_s = probe_duration_seconds(video_path)

        # Audio + transcript
        audio_path = extract_audio(video_path, work_dir / "audio.wav")
        segments = transcribe(audio_path, model_size=whisper_model)
        transcript_path = work_dir / "transcript.json"
        transcript_path.write_text(json.dumps(serialize(segments), indent=2))

        # Frames
        frames_dir = work_dir / "frames"
        frames = sample_frames(video_path, frames_dir, interval_seconds=frame_interval)

        # Assemble relational context for THIS teacher: profile, goals, prior
        # trajectory, open bite-sized actions, district arc + priorities. This
        # is what makes the scoring context-aware — the same lesson yields
        # different reports for different teachers.
        conn = db_connect(db_path)
        try:
            obs_row = conn.execute(
                "SELECT teacher_id, org_id FROM observations WHERE id = ?",
                (observation_id,),
            ).fetchone()
            if obs_row is None:
                raise RuntimeError(f"Observation not found: {observation_id}")
            teacher_context = assemble_teacher_context(
                conn,
                teacher_id=obs_row["teacher_id"],
                org_id=obs_row["org_id"],
            )
        finally:
            conn.close()
        context_block = render_context_for_prompt(teacher_context)

        # Score
        _set_status(db_path, observation_id, "scoring")
        report = score_observation(
            rubric=rubric,
            transcript=segments,
            frames=frames,
            video_filename=video_path.name,
            duration_seconds=duration_s,
            teacher_context_block=context_block,
        )

        # Persist the report as a new report_version row.
        _persist_report(
            db_path=db_path,
            observation_id=observation_id,
            report=report,
            rubric_id=rubric_id,
            duration_s=duration_s,
            frame_count=len(frames),
            frame_interval_s=frame_interval,
            transcription_model=f"faster-whisper:{whisper_model}",
        )

        # Cleanup the big audio file, keep frames + transcript for inspection.
        (work_dir / "audio.wav").unlink(missing_ok=True)

    except Exception as e:
        traceback.print_exc()
        _set_status(db_path, observation_id, "failed",
                    failure_reason=f"{type(e).__name__}: {e}")


def _persist_report(
    *,
    db_path: Path,
    observation_id: str,
    report,
    rubric_id: str,
    duration_s: float,
    frame_count: int,
    frame_interval_s: float,
    transcription_model: str,
) -> None:
    """Insert the completed report as the next version and mark observation complete.

    Also:
      - Snapshot every new bite-sized action into ``bite_sized_action_tracking``
        so the next observation can assess implementation.
      - Persist any ``prior_action_assessments`` the AI produced by updating
        the corresponding tracking rows.
      - Persist ``highest_leverage_move`` as an extra JSON blob alongside the
        normal report fields (schema stores it inline in the domain_assessments
        JSON via the report field — no schema change needed).
    """
    import uuid
    conn = db_connect(db_path)
    try:
        # Update observation with pipeline metadata.
        conn.execute(
            """UPDATE observations SET
                   status = 'complete',
                   video_duration_s = ?,
                   frame_count = ?,
                   frame_interval_s = ?,
                   transcript_model = ?,
                   scored_at = ?,
                   failure_reason = NULL
               WHERE id = ?""",
            (duration_s, frame_count, frame_interval_s,
             transcription_model, _now_iso(), observation_id),
        )
        obs_row = conn.execute(
            "SELECT teacher_id, org_id FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()

        # Persist prior-action assessments (mark the tracking rows as followed-up).
        for pa in getattr(report, "prior_action_assessments", []) or []:
            conn.execute(
                """UPDATE bite_sized_action_tracking SET
                       followup_observation_id = ?,
                       implementation = ?,
                       evidence_notes = ?,
                       assessed_by_user_id = NULL,
                       assessed_at = ?
                   WHERE id = ?""",
                (
                    observation_id,
                    pa.implementation,
                    pa.evidence_notes,
                    _now_iso(),
                    pa.tracking_id,
                ),
            )

        # Snapshot new bite-sized actions into tracking so the NEXT observation
        # can be assessed against them.
        for rec in report.coaching_recommendations:
            record_bite_sized_action(
                conn,
                org_id=obs_row["org_id"],
                teacher_id=obs_row["teacher_id"],
                source_observation_id=observation_id,
                core_teacher_skill=rec.core_teacher_skill,
                related_domain=rec.related_domain,
                bite_sized_action_text=rec.bite_sized_action,
            )

        # Compute next version_number (starts at 1).
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS m FROM report_versions WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        next_ver = row["m"] + 1

        # Unpublish any prior versions — the uq_rv_one_published partial unique
        # index enforces at most one published version per observation.
        conn.execute(
            "UPDATE report_versions SET published_at = NULL WHERE observation_id = ?",
            (observation_id,),
        )

        # We serialize the entire report (including highest_leverage_move) into
        # the report_versions row. The dedicated column for coaching_recs stays
        # populated for backwards compatibility; the newer fields go into an
        # extra column the reader can pick up.
        # SQLite: schema doesn't have a column for these new fields, so we
        # stash them inside the coaching_recommendations JSON envelope as a
        # {"recommendations": [...], "highest_leverage_move": {...}} wrapper.
        # Backwards-compatible reader: if the JSON parses as a list, treat as
        # legacy; if it parses as a dict with 'recommendations', unwrap.
        coaching_wrapper = {
            "recommendations": [cr.model_dump() for cr in report.coaching_recommendations],
            "highest_leverage_move": (
                report.highest_leverage_move.model_dump()
                if report.highest_leverage_move else None
            ),
            "prior_action_assessments": [
                pa.model_dump() for pa in getattr(report, "prior_action_assessments", []) or []
            ],
        }

        conn.execute(
            """INSERT INTO report_versions
                   (id, observation_id, version_number, authored_by,
                    opening_paragraph, overall_summary,
                    domain_assessments, coaching_recommendations,
                    rendered_markdown, published_at, created_at)
               VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                observation_id,
                next_ver,
                report.opening_paragraph,
                report.overall_summary,
                json.dumps([da.model_dump() for da in report.domain_assessments]),
                json.dumps(coaching_wrapper),
                None,
                _now_iso(),
                _now_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
