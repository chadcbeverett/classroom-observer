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


# Watchdog: reclaim observations left in an in-flight status by a crashed
# worker or a killed process. Called at app startup (see app.main).
#
# Because the executor is in-process and stateless across restarts, any job
# alive at the moment uvicorn was signaled is gone forever — but the DB
# row still says 'transcribing' or 'scoring', spinning forever in every
# in-flight aggregate and blocking the coach from re-uploading. This sweep
# marks such rows 'failed' with a clear reason so the coach can retry.
#
# Threshold is generous (30 min) because Whisper on a full-lesson video can
# legitimately take that long. Adjust if the pipeline gets faster.
STUCK_JOB_MINUTES = 30


def sweep_stuck_jobs(db_path: Path) -> int:
    """Mark observations stuck in transcribing/scoring older than the
    threshold as failed. Returns the number reclaimed. Safe to call at
    every startup — idempotent when nothing is stuck.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_JOB_MINUTES)).isoformat()
    conn = db_connect(db_path)
    try:
        # `uploaded_at` is the closest to "when did the pipeline start" — the
        # status column has no timestamp of its last transition, so we use
        # uploaded_at as a coarse lower bound. A newly-uploaded obs won't
        # trip the sweep even if it lands in transcribing immediately.
        rows = conn.execute(
            """SELECT id FROM observations
               WHERE status IN ('transcribing', 'scoring')
                 AND uploaded_at < ?""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE observations
                   SET status = 'failed',
                       failure_reason = ?
                   WHERE id = ?""",
                (f"Job did not finish within {STUCK_JOB_MINUTES} minutes — "
                 f"likely a worker crash or app restart. Re-upload to try again.",
                 row["id"]),
            )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


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
        # Look up the observation's teacher + org FIRST — every scoped write
        # below needs them, and the tracking updates are the entry point for
        # a cross-teacher poisoning bug when the AI hallucinates a tracking_id.
        obs_row = conn.execute(
            "SELECT teacher_id, org_id FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if not obs_row:
            raise RuntimeError(f"Observation {observation_id!r} disappeared during persist")

        # All writes below run in ONE transaction — we do NOT commit between
        # them and we do NOT call record_bite_sized_action's default committing
        # path. If anything after this point raises, sqlite discards every
        # partial write and the observation stays in whatever pre-persist state
        # the caller had set (typically 'scoring'), so the outer handler in
        # _run_job can mark it 'failed' cleanly.
        #
        # Prior ordering committed 'observations.status=complete' + tracking
        # snapshots via record_bite_sized_action's internal commit BEFORE the
        # report_versions INSERT — a mid-persist crash left the observation
        # marked complete with no report row, and phantom tracking rows for
        # a report that never landed. Now: everything or nothing.

        # 1. Prior-action assessments — mark the tracking rows as followed-up.
        # The tracking_id from the AI is UNTRUSTED input: hallucinated or
        # cross-teacher ids would silently update another teacher's action.
        # Scope every UPDATE to (id AND teacher_id) and confirm rowcount==1;
        # skip anything else and log it so the coach can see the drift.
        _now = _now_iso()
        _persisted_pa: list = []
        _skipped_pa: list = []
        for pa in getattr(report, "prior_action_assessments", []) or []:
            cur = conn.execute(
                """UPDATE bite_sized_action_tracking SET
                       followup_observation_id = ?,
                       implementation = ?,
                       evidence_notes = ?,
                       assessed_by_user_id = NULL,
                       assessed_at = ?
                   WHERE id = ? AND teacher_id = ?""",
                (
                    observation_id,
                    pa.implementation,
                    pa.evidence_notes,
                    _now,
                    pa.tracking_id,
                    obs_row["teacher_id"],
                ),
            )
            if cur.rowcount == 1:
                _persisted_pa.append(pa)
            else:
                # AI referenced an action that isn't on this teacher's ledger
                # (hallucinated UUID, cross-teacher collision, or an action
                # already reassigned). Do NOT let it land: the JSON payload
                # below would otherwise show the coach a rating for an
                # action that doesn't exist on this teacher.
                _skipped_pa.append({
                    "tracking_id": pa.tracking_id,
                    "reason": "not-on-teacher-or-missing",
                })

        # 2. Snapshot new bite-sized actions into tracking so the NEXT
        # observation can be assessed against them. commit=False so the
        # tracking rows live or die with the report.
        for rec in report.coaching_recommendations:
            record_bite_sized_action(
                conn,
                org_id=obs_row["org_id"],
                teacher_id=obs_row["teacher_id"],
                source_observation_id=observation_id,
                core_teacher_skill=rec.core_teacher_skill,
                related_domain=rec.related_domain,
                bite_sized_action_text=rec.bite_sized_action,
                commit=False,
            )

        # 3. Compute next version_number (starts at 1).
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) AS m FROM report_versions WHERE observation_id = ?",
            (observation_id,),
        ).fetchone()
        next_ver = row["m"] + 1

        # 4. Unpublish any prior versions — uq_rv_one_published partial index
        # enforces at most one published version per observation.
        conn.execute(
            "UPDATE report_versions SET published_at = NULL WHERE observation_id = ?",
            (observation_id,),
        )

        # 5. Write the report row. Only the prior-action assessments we
        # actually persisted end up in the JSON envelope — dropping the
        # hallucinated ones so the reader can trust every id in the wrapper
        # points at a real tracking row.
        coaching_wrapper = {
            "recommendations": [cr.model_dump() for cr in report.coaching_recommendations],
            "highest_leverage_move": (
                report.highest_leverage_move.model_dump()
                if report.highest_leverage_move else None
            ),
            "prior_action_assessments": [pa.model_dump() for pa in _persisted_pa],
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
                _now,
                _now,
            ),
        )

        # 6. Flip the observation to complete LAST — after every payload has
        # been written but before the commit. If any of the above raised,
        # the observation stays in 'scoring' and the outer handler marks it
        # 'failed'. When we reach here, the whole thing lands together.
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
             transcription_model, _now, observation_id),
        )
        conn.commit()

        if _skipped_pa:
            # Not a failure — the report landed, but flag the drift for a
            # future audit that watches for AI id-hallucination trends.
            print(f"[jobs] {observation_id[:8]}: skipped {len(_skipped_pa)} "
                  f"prior-action assessment(s) with unknown/cross-teacher ids: {_skipped_pa}")
    except Exception:
        # Roll back any pending writes so the observation state stays whatever
        # the caller had. sqlite auto-rolls-back on next commit-or-close, but
        # explicit is friendlier for a shared connection in future refactors.
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()
