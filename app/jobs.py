"""Persistent job queue + in-process worker for the AI scoring pipeline.

Old shape: ThreadPoolExecutor(max_workers=1) inside uvicorn. A crash /
SIGKILL / container restart lost every queued job with no trace, and a
mid-flight job left observations.status pinned at 'transcribing' or
'scoring' forever (the startup sweep reclaimed those to 'failed', but
the QUEUED backlog was gone).

New shape: submit_job INSERTs a row into `job_queue` (see the schema in
pipeline/db.py::_apply_additive_migrations). A background thread —
JobWorker, structurally identical to app.smtp_sender.SmtpSender — polls
the table, atomically claims one job at a time, runs the pipeline, and
records outcome. Restarts resume: 'pending' rows stay pending (worker
picks up on next boot), 'running' rows older than STUCK_JOB_MINUTES get
reset by the startup sweep (retried if attempts < max, else 'failed').

Kept SQLite-backed so the pilot deploy doesn't need Redis. The
interface — ``submit_job(...)`` — is identical to what RQ would offer,
so swapping the worker for a proper queue later is a class replacement,
not a route rewrite.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import threading
import traceback
import uuid
from datetime import datetime, timedelta, timezone
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

import logging
_log = logging.getLogger("uvicorn.error")

# Poll cadence for the worker thread. Cheap SELECT (partial index on
# status IN ('pending','running')). Latency to job start on an idle
# queue is at most this interval.
WORKER_POLL_INTERVAL_SECONDS = 2.0
MAX_JOB_ATTEMPTS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Watchdog: reclaim jobs + observations left in-flight by a crashed
# worker or a killed process. Called at app startup (see app.main).
#
# Two things to reclaim:
#   (a) job_queue rows in 'running' state older than the threshold —
#       reset to 'pending' for retry (or 'failed' if attempts >= max).
#       The worker picks them up on its next poll cycle.
#   (b) observations in 'transcribing' or 'scoring' status older than
#       the threshold — mark 'failed' with a clear reason. Covers the
#       case where the worker crashed mid-pipeline BEFORE landing a
#       final job status; the observation was mid-flight when the
#       process died and neither status will progress on its own.
#
# Threshold is generous (30 min) because Whisper on a full-lesson video
# can legitimately take that long. Adjust if the pipeline gets faster.
STUCK_JOB_MINUTES = 30


def sweep_stuck_jobs(db_path: Path) -> int:
    """Reset stuck job_queue rows AND mark stuck observations failed.
    Returns the total number reclaimed (jobs + observations).
    Idempotent — safe to call at every startup.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STUCK_JOB_MINUTES)).isoformat()
    reclaimed = 0
    conn = db_connect(db_path)
    try:
        # (a) job_queue rows that were claimed but never finished.
        stuck_jobs = conn.execute(
            """SELECT id, attempts, max_attempts FROM job_queue
               WHERE status = 'running' AND started_at < ?""",
            (cutoff,),
        ).fetchall()
        for row in stuck_jobs:
            reclaimed += 1
            if row["attempts"] < row["max_attempts"]:
                # Retry: back to pending. The worker will re-attempt.
                conn.execute(
                    """UPDATE job_queue
                       SET status = 'pending', started_at = NULL,
                           last_error = ?
                       WHERE id = ?""",
                    (f"Reset by startup sweep — process was likely killed mid-run. "
                     f"Attempt {row['attempts']} of {row['max_attempts']}.",
                     row["id"]),
                )
            else:
                # Out of attempts. Mark failed permanently.
                conn.execute(
                    """UPDATE job_queue
                       SET status = 'failed', finished_at = ?,
                           last_error = ?
                       WHERE id = ?""",
                    (_now_iso(),
                     f"Exhausted {row['max_attempts']} attempts; last "
                     f"attempt didn't finish within {STUCK_JOB_MINUTES} min.",
                     row["id"]),
                )
        # (b) observations that reflect a mid-pipeline crash. `uploaded_at`
        # is the closest bound we have on "when did processing start" —
        # good enough because a newly-uploaded obs won't trip the cutoff.
        stuck_obs = conn.execute(
            """SELECT id FROM observations
               WHERE status IN ('transcribing', 'scoring')
                 AND uploaded_at < ?""",
            (cutoff,),
        ).fetchall()
        for row in stuck_obs:
            reclaimed += 1
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
        return reclaimed
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
) -> str:
    """Queue a scoring job. Returns the job_queue row id.

    Fire-and-forget from the caller's perspective. UI polls
    ``GET /observations/{id}/status`` for progress. If the app is
    restarted before the worker picks this row up, the row persists
    as 'pending' and the worker on the new boot runs it.
    """
    job_id = str(uuid.uuid4())
    payload = json.dumps({
        "db_path": str(db_path),
        "observation_id": observation_id,
        "video_path": str(video_path),
        "rubric_id": rubric_id,
        "whisper_model": whisper_model,
        "frame_interval": frame_interval,
    })
    conn = db_connect(db_path)
    try:
        conn.execute(
            """INSERT INTO job_queue
                 (id, kind, payload, status, max_attempts, created_at)
               VALUES (?, 'score_observation', ?, 'pending', ?, ?)""",
            (job_id, payload, MAX_JOB_ATTEMPTS, _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()
    # Wake the worker if it's sleeping — cheaper than making it poll faster.
    if _worker_singleton is not None:
        _worker_singleton.wake()
    return job_id


# ---------------------------------------------------------------------------
# Worker — one background thread that drains job_queue.
# ---------------------------------------------------------------------------

_worker_singleton: Optional["JobWorker"] = None


class JobWorker:
    """Owns one background thread that atomically claims and runs jobs.

    Not thread-safe against multiple workers sharing one DB in the naive
    sense — the atomic claim (``UPDATE ... WHERE status='pending'`` with
    rowcount check) is safe against concurrent claimers, so multiple
    JobWorker instances against the same DB would compete correctly.
    Today we run one per process; production can add more without
    schema changes.
    """

    def __init__(self, db_path: Path, poll_interval: float = WORKER_POLL_INTERVAL_SECONDS):
        self.db_path = db_path
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="job-worker", daemon=True,
        )
        self._thread.start()
        _log.info("Job worker started (db=%s, poll=%.1fs)", self.db_path, self.poll_interval)

    def stop(self, timeout: float = 10.0) -> None:
        """Signal the loop to exit. Waits up to ``timeout`` for the
        current job to reach a checkpoint. A long-running scoring call
        will run to completion (its own internal timeouts bound the
        wall-time); the loop exits after that.
        """
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        _log.info("Job worker stopped")

    def wake(self) -> None:
        """Nudge the loop to skip its poll wait and check for work now.
        Called from submit_job so a fresh job doesn't wait for the next
        poll tick.
        """
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                ran = self._claim_and_run_one()
            except Exception as e:
                # Never let the loop die — a broken claim would silently
                # leave the queue growing.
                _log.exception("Job worker loop error: %s", e)
                ran = False
            if not ran:
                # Idle wait — interrupted by wake() on new submissions.
                self._wake.wait(self.poll_interval)
                self._wake.clear()

    def _claim_and_run_one(self) -> bool:
        """Try to claim ONE pending job. Returns True if a job was
        claimed and run (regardless of outcome), False if the queue
        was empty. Atomic-claim pattern: UPDATE ... WHERE id=? AND
        status='pending', check rowcount.
        """
        conn = db_connect(self.db_path)
        job = None
        try:
            candidate = conn.execute(
                """SELECT id, kind, payload, attempts, max_attempts
                   FROM job_queue
                   WHERE status = 'pending'
                   ORDER BY created_at ASC
                   LIMIT 1"""
            ).fetchone()
            if not candidate:
                return False
            # Atomic claim. If another worker grabbed this row between
            # our SELECT and UPDATE, rowcount is 0 and we skip.
            cur = conn.execute(
                """UPDATE job_queue
                   SET status = 'running',
                       started_at = ?,
                       attempts = attempts + 1
                   WHERE id = ? AND status = 'pending'""",
                (_now_iso(), candidate["id"]),
            )
            conn.commit()
            if cur.rowcount != 1:
                # Lost the race.
                return True
            job = dict(candidate)
            job["attempts"] = job["attempts"] + 1
        finally:
            conn.close()

        if not job:
            return False

        # Run the job body. Every exception is caught + recorded — the
        # loop must survive.
        try:
            payload = json.loads(job["payload"])
            if job["kind"] == "score_observation":
                _run_job(
                    db_path=Path(payload["db_path"]),
                    observation_id=payload["observation_id"],
                    video_path=Path(payload["video_path"]),
                    rubric_id=payload["rubric_id"],
                    whisper_model=payload.get("whisper_model", "medium"),
                    frame_interval=float(payload.get("frame_interval", 60.0)),
                )
            else:
                raise RuntimeError(f"Unknown job kind: {job['kind']!r}")
            # Success.
            conn = db_connect(self.db_path)
            try:
                conn.execute(
                    """UPDATE job_queue
                       SET status = 'complete', finished_at = ?
                       WHERE id = ?""",
                    (_now_iso(), job["id"]),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            # Retry or fail. Note attempts already incremented above.
            _log.exception("Job %s failed on attempt %d: %s",
                           job["id"][:8], job["attempts"], e)
            err = f"{type(e).__name__}: {e}"[:2000]
            conn = db_connect(self.db_path)
            try:
                if job["attempts"] >= job["max_attempts"]:
                    conn.execute(
                        """UPDATE job_queue
                           SET status = 'failed', finished_at = ?, last_error = ?
                           WHERE id = ?""",
                        (_now_iso(), err, job["id"]),
                    )
                else:
                    # Back to pending for retry. The observation's own
                    # status was set to 'failed' by _run_job's except
                    # block; the next attempt will re-flip it through
                    # 'transcribing' → 'scoring' → 'complete'.
                    conn.execute(
                        """UPDATE job_queue
                           SET status = 'pending', started_at = NULL, last_error = ?
                           WHERE id = ?""",
                        (err, job["id"]),
                    )
                conn.commit()
            finally:
                conn.close()
        return True


def start_worker(db_path: Path) -> JobWorker:
    """Idempotent starter for app._bootstrap."""
    global _worker_singleton
    if _worker_singleton is None:
        _worker_singleton = JobWorker(db_path)
        _worker_singleton.start()
    return _worker_singleton


def stop_worker() -> None:
    global _worker_singleton
    if _worker_singleton is not None:
        _worker_singleton.stop()
        _worker_singleton = None


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
        # Sanitize the failure_reason before it lands in the DB → coach UI.
        # audio.py / frames.py already scrub the video path from their own
        # RuntimeError messages, but a raw exception from anywhere else in
        # the pipeline may still include an absolute host path. Strip the
        # uploads root prefix, keep enough of the message to be actionable.
        raw = f"{type(e).__name__}: {e}"
        try:
            _uploads_root = str(video_path.parent.parent)  # app/uploads
            raw = raw.replace(_uploads_root, "<uploads>")
            raw = raw.replace(str(video_path.parent), f"<uploads>/<obs>")
            raw = raw.replace(str(video_path), f"<uploads>/<obs>/{video_path.name}")
        except Exception:
            pass
        _set_status(db_path, observation_id, "failed", failure_reason=raw[:2000])


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
