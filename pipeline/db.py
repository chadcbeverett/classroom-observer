"""SQLite-backed adapter for the multi-tenant schema.

Purpose: validate the schema design in ``docs/data_model.md`` against real
observation data, and provide a stepping stone for the eventual Postgres
migration. Not intended for production use.

Usage:
    from pipeline.db import connect, init_db
    conn = connect("classroom_observer.sqlite")
    init_db(conn)  # idempotent

    # Import an existing reports/*/scores.json:
    from pipeline.db import import_scores_json
    obs_id = import_scores_json(conn, Path("reports/dulaney_2026-05-11"))
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return str(uuid.uuid4())


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with foreign-key enforcement enabled."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Apply the schema. Idempotent — CREATE TABLE IF NOT EXISTS + additive
    ALTER TABLE for columns added after the initial schema.
    """
    schema_sql = SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    _apply_additive_migrations(conn)
    conn.commit()


def _apply_additive_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that were introduced after the initial schema shipped.
    Each ALTER TABLE is a no-op if the column already exists.

    SQLite caveat: ``ALTER TABLE … ADD COLUMN`` cannot declare a FOREIGN KEY
    constraint on the added column — the parser accepts REFERENCES but the
    constraint is silently NOT enforced. So the columns below that are
    conceptually foreign keys — ``observations.lesson_plan_id``,
    ``bite_sized_action_tracking.goal_id``,
    ``professional_goals.carried_forward_from_goal_id``, and
    ``hlm_responses.published_coach_move_id`` — live as bare TEXT here even
    though a fresh schema would FK them. That's fine today (no hard-DELETE
    path targets those parent tables), but noted so a future schema rebuild
    can restore the constraints, and app-layer writes already validate the
    parent-row existence at the call site.
    """
    for table, column, decl in [
        # teacher_profiles: coach ratings + skill dev narrative
        ("teacher_profiles", "coach_ratings", "TEXT"),
        ("teacher_profiles", "coach_ratings_context", "TEXT"),
        ("teacher_profiles", "skill_development_narrative", "TEXT"),
        # coaching_cycles: expected_close_date
        ("coaching_cycles", "expected_close_date", "TEXT"),
        # observations: coach-named debrief focus
        ("observations", "debrief_focus", "TEXT"),
        ("observations", "debrief_focus_gbf_id", "TEXT"),
        # bite-sized actions: teacher's own account of trying the action
        ("bite_sized_action_tracking", "teacher_account", "TEXT"),
        ("bite_sized_action_tracking", "teacher_account_at", "TEXT"),
        # professional_goals: what's being set aside (Bridges' ending)
        ("professional_goals", "releasing", "TEXT"),
        # professional_goals: close-time semantic — which success indicators were
        # met (JSON array of exact indicator strings), why it didn't land, and
        # whether it was carried forward into a new goal.
        ("professional_goals", "closed_indicators_met", "TEXT"),
        ("professional_goals", "closed_outcome_reason", "TEXT"),
        ("professional_goals", "closed_outcome_notes", "TEXT"),
        ("professional_goals", "carried_forward_from_goal_id", "TEXT"),
        # bite-sized actions: relational tie to the goal being worked on. Was
        # historically a free-text link via related_domain/skill; now first-class.
        ("bite_sized_action_tracking", "goal_id", "TEXT"),
        # district_context: coaching expectations (observation cadence) so the
        # compliance report and teacher hubs reflect district policy instead of
        # ad-hoc URL params.
        ("district_context", "observations_per_year_target", "INTEGER"),
        ("district_context", "days_between_obs_target", "INTEGER"),
        ("district_context", "days_from_obs_to_debrief_target", "INTEGER"),
        # observations: link to the lesson plan being observed. Coach picks at
        # upload; auto-suggest by date-range match on plan_start/plan_end.
        ("observations", "lesson_plan_id", "TEXT"),
        # professional_goals: which district priority (if any) seeded this goal.
        # Set when coach uses a priority chip on the empty-goal CTA. Enables
        # district-level rollup: "of goals from priority X, how many met."
        ("professional_goals", "origin_priority_name", "TEXT"),
        # coaching_cycles: the "story of this cycle" — coach-authored narrative
        # of the arc. Rendered co-equal with the rubric-delta KPI on cycle close.
        # Distinct from `notes` (kick-off) and `closing_notes` (short close tags).
        ("coaching_cycles", "growth_story", "TEXT"),
    ]:
        cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    # New table: the coach's *published* move for the teacher.
    # Distinct from the AI's raw suggestion — coach reviews AI, edits, and
    # publishes. Teacher never sees raw AI output; only the coach's version.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS published_coach_moves (
            id                       TEXT PRIMARY KEY,
            observation_id           TEXT NOT NULL REFERENCES observations(id),
            move_text                TEXT NOT NULL,
            gbf_step_id              TEXT,
            related_core_teacher_skill TEXT,
            -- true when the coach clicked "start from AI's suggestion"; false when
            -- the coach wrote fresh. Never shown to the teacher; internal tracking
            -- for principal oversight of AI-coach interaction.
            derived_from_ai          INTEGER NOT NULL DEFAULT 0,
            published_by_user_id     TEXT REFERENCES users(id),
            published_at             TEXT NOT NULL,
            edited_at                TEXT,
            -- Set when a NEW version is published — the old version stays for the
            -- record but no longer surfaces as "current" to the teacher.
            superseded_at            TEXT,
            superseded_by_move_id    TEXT
        )"""
    )
    # One CURRENT (non-superseded) move per observation.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_pcm_current
           ON published_coach_moves(observation_id) WHERE superseded_at IS NULL"""
    )

    # New table: teacher's response to the coach's published move.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hlm_responses (
            id                  TEXT PRIMARY KEY,
            observation_id      TEXT NOT NULL REFERENCES observations(id),
            response_type       TEXT NOT NULL
                CHECK (response_type IN ('resonates', 'adjust', 'talk')),
            teacher_note        TEXT,
            teacher_user_id     TEXT REFERENCES users(id),
            created_at          TEXT NOT NULL,
            acknowledged_at     TEXT,
            acknowledged_by_user_id TEXT REFERENCES users(id)
        )"""
    )
    # One active response per observation — later responses replace earlier.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_hlm_response_obs ON hlm_responses(observation_id)"
    )
    # Tie response to a specific published move version (nullable for legacy rows).
    _hlm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(hlm_responses)").fetchall()}
    if "published_coach_move_id" not in _hlm_cols:
        conn.execute("ALTER TABLE hlm_responses ADD COLUMN published_coach_move_id TEXT")

    # One active (non-closed) cycle per teacher — hard invariant at the storage
    # layer. The app enforces this in create_cycle already, but two concurrent
    # POSTs could both pass a check-then-insert without a transaction gate.
    # A partial unique index makes the storage refuse to hold two open cycles
    # for one teacher regardless of write path.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_one_open_cycle_per_teacher
           ON coaching_cycles(teacher_id) WHERE closed_at IS NULL"""
    )

    # Partial-unique indexes on teachers matching what bulk_create_teacher's
    # dedupe checks. The app-level check-then-insert is safe from concurrent
    # bulk uploads collapsing into duplicate rows only if the storage catches
    # the race — a partial index on non-null values is what makes that hold.
    # NULL email / employee_id are allowed to repeat (they're legitimately
    # missing on some rows).
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_teachers_org_email
           ON teachers(org_id, email) WHERE email IS NOT NULL"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_teachers_org_employee_id
           ON teachers(org_id, employee_id) WHERE employee_id IS NOT NULL"""
    )

    # ---- Auth substrate: real sign-in replaces the cookie-switch personas ----
    #
    # Magic-link flow: user enters email, we mint a short-lived signed token
    # bound to their (org, email), email them the URL, they click, we consume
    # the token and mint a session. Sessions are the durable side; tokens are
    # single-use. Both live in the app DB so a session survives an app restart
    # and a token can be revoked by row.
    #
    # No passwords are stored — the token IS the auth factor, and it lives
    # long enough to click through an email but not to be leaked-and-replayed.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS auth_sessions (
            id                  TEXT PRIMARY KEY,   -- session token (goes in the cookie)
            user_id             TEXT NOT NULL REFERENCES users(id),
            created_at          TEXT NOT NULL,
            last_seen_at        TEXT NOT NULL,
            expires_at          TEXT NOT NULL,
            ip                  TEXT,               -- best-effort, may be a proxy hop
            user_agent          TEXT,
            revoked_at          TEXT                -- explicit sign-out or admin revoke
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id, expires_at)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS magic_link_tokens (
            id                  TEXT PRIMARY KEY,   -- the token itself (URL-safe)
            email               TEXT NOT NULL,      -- what was typed at /signin
            user_id             TEXT REFERENCES users(id),  -- resolved if the email matches
            purpose             TEXT NOT NULL DEFAULT 'signin'
                CHECK (purpose IN ('signin', 'invite')),
            next_url            TEXT,               -- where to land after sign-in (same-origin)
            created_at          TEXT NOT NULL,
            expires_at          TEXT NOT NULL,      -- default: created_at + 15 minutes
            consumed_at         TEXT,               -- set on successful click
            consumed_session_id TEXT REFERENCES auth_sessions(id),
            ip                  TEXT                -- from the /signin POST
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_magic_link_email ON magic_link_tokens(email, created_at DESC)"
    )
    # Outbound mail: everything we would email lands here first. In dev/pilot
    # this IS the delivery channel (a dev page reads unopened rows and shows
    # the link); in production an SMTP sender daemon pumps unsent rows out.
    # Separating "write intent to send" from "actually send" gives a clean
    # audit trail and lets us re-send by clearing sent_at.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbound_mail (
            id                  TEXT PRIMARY KEY,
            to_email            TEXT NOT NULL,
            subject             TEXT NOT NULL,
            body_text           TEXT NOT NULL,
            body_html           TEXT,
            created_at          TEXT NOT NULL,
            sent_at             TEXT,               -- set by the SMTP sender when it lands
            failed_at           TEXT,
            failure_reason      TEXT,
            related_token_id    TEXT REFERENCES magic_link_tokens(id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_outbound_mail_unsent ON outbound_mail(sent_at, created_at)"
    )

    # ---- Persistent job queue ---------------------------------------------
    # The AI scoring pipeline used to run inside an in-process
    # ThreadPoolExecutor with no persistence — a uvicorn crash / SIGKILL /
    # container restart lost every queued job, and a mid-flight job left
    # observation status pinned at 'transcribing' or 'scoring' forever.
    # (The startup sweep on observations reclaimed the latter as 'failed',
    # but the queued backlog was gone.)
    #
    # New table: submit_job INSERTs here; a worker thread claims one row
    # at a time and runs the pipeline. Restarts resume: 'pending' stays
    # pending (worker picks up on restart), 'running' rows older than
    # STUCK_JOB_MINUTES get reset by the startup sweep. Failed attempts
    # up to max_attempts re-queue as 'pending' with `last_error` set.
    #
    # Kept SQLite-backed so we don't drag Redis into the pilot deploy;
    # the interface is the same one RQ would offer, so swapping in RQ
    # later is a worker-class replacement, not a route rewrite.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS job_queue (
            id                  TEXT PRIMARY KEY,
            kind                TEXT NOT NULL,
                -- 'score_observation' is the only kind today; keep the
                -- column so future one-off ceremonies (re-render report,
                -- reprocess with a new rubric) share the queue.
            payload             TEXT NOT NULL,   -- JSON blob, kind-specific
            status              TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'complete', 'failed')),
            attempts            INTEGER NOT NULL DEFAULT 0,
            max_attempts        INTEGER NOT NULL DEFAULT 3,
            created_at          TEXT NOT NULL,
            started_at          TEXT,
            finished_at         TEXT,
            last_error          TEXT
        )"""
    )
    # Partial index — only "live" jobs. 'complete' rows accumulate but
    # never hit the worker's SELECT again, so no need to index them.
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_job_queue_live
           ON job_queue(status, created_at)
           WHERE status IN ('pending', 'running')"""
    )

    # ---- Consent: the schema table exists; add the audit column for who
    # electronically signed. Also ensure the "one active consent per teacher"
    # invariant via partial-unique index — matches the "org-level, revocable"
    # policy we agreed on. (A single_observation-scoped row would need its own
    # observation_id column; leave for later.)
    _cr_cols = {row["name"] for row in conn.execute("PRAGMA table_info(consent_records)").fetchall()}
    if "granted_by_user_id" not in _cr_cols:
        conn.execute("ALTER TABLE consent_records ADD COLUMN granted_by_user_id TEXT REFERENCES users(id)")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_consent_per_teacher
           ON consent_records(teacher_id) WHERE revoked_at IS NULL"""
    )

    # ---- GBF step-id migration (mana → mgmt) -------------------------------
    # The GBF slug builder previously used `trajectory[:4]`, which turned
    # "management" into "mana" instead of the "mgmt" the docs, the prompt,
    # and the schema-example all cite. Rewrites of _mk fix new ids; any
    # stored id from before this fix stays broken until we migrate. Do the
    # substitution once against the two columns that carry gbf_step_id.
    # No-op when nothing matches; idempotent.
    conn.execute(
        """UPDATE observations
           SET debrief_focus_gbf_id = REPLACE(debrief_focus_gbf_id, '_mana_', '_mgmt_')
           WHERE debrief_focus_gbf_id LIKE '%\\_mana\\_%' ESCAPE '\\'"""
    )
    conn.execute(
        """UPDATE published_coach_moves
           SET gbf_step_id = REPLACE(gbf_step_id, '_mana_', '_mgmt_')
           WHERE gbf_step_id LIKE '%\\_mana\\_%' ESCAPE '\\'"""
    )

    # ---- Missing indexes on hot aggregate paths ----------------------------
    # Every dashboard / roster / compliance aggregate filters `o.deleted_at
    # IS NULL` combined with a teacher_id or org_id predicate. The existing
    # idx_obs_teacher is single-column on teacher_id, so SQLite scans every
    # matching teacher's observations and filters at the row level. A
    # composite makes the deleted_at filter part of the index seek — matters
    # once a pilot's obs count crosses ~1,000. Similarly for cycles.closed_at
    # (every "active cycle" query filters IS NULL) and the newer goal_id FK
    # on bite_sized_action_tracking (list_actions_for_goal scans without it).
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_obs_teacher_active "
        "ON observations(teacher_id, deleted_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cycles_teacher_open "
        "ON coaching_cycles(teacher_id, closed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bsat_goal "
        "ON bite_sized_action_tracking(goal_id) WHERE goal_id IS NOT NULL"
    )

    # ---- CHECK-constraint widening (existing DBs) --------------------------
    # schema.sql was updated to allow 'teacher' as a legal role literal on
    # users.role and lesson_plan_comments.author_role. Because SQLite can't
    # ALTER a CHECK constraint in place, DBs that were created before this
    # widening still refuse `role='teacher'` at insert time — a real teacher
    # signing in via magic link would silently drop out of every viewer.role
    # gate. The one-time rebuild below copies the table through, applies the
    # new CHECK, and preserves data. Idempotent: only fires when the current
    # CHECK definition still lists 'teacher_self_serve' without 'teacher'.
    _widen_role_checks(conn)

    conn.commit()


def _widen_role_checks(conn: sqlite3.Connection) -> None:
    """One-time table-rebuild migrations for the two role/author CHECK
    constraints that predate the standardization on 'teacher' as the
    canonical role literal. Idempotent — skipped when the existing CHECK
    already permits 'teacher'.

    Uses the standard 12-step-lite SQLite pattern: turn FKs off (so the
    rename doesn't cascade), create shadow table, copy, drop, rename,
    recreate the index that lived on the old table, turn FKs back on.
    """
    for table, column, new_check, extra_indexes in [
        (
            "users", "role",
            "CHECK (role IN ('admin', 'coach', 'teacher', 'teacher_self_serve', 'viewer'))",
            ["CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id)"],
        ),
        (
            "lesson_plan_comments", "author_role",
            "CHECK (author_role IN ('teacher', 'teacher_self_serve', 'coach', 'admin'))",
            ["CREATE INDEX IF NOT EXISTS idx_lpc_plan ON lesson_plan_comments(lesson_plan_id, created_at ASC)"],
        ),
    ]:
        # Detect whether the existing table's CHECK already includes 'teacher'.
        # The SQL text is stored in sqlite_master.sql for the CREATE TABLE.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not row or not row["sql"]:
            continue
        existing_sql = row["sql"]
        # If it already lists 'teacher' as a bare-word literal in a CHECK on
        # this column, the migration is done.
        if f"CHECK ({column} IN " in existing_sql and "'teacher'" in existing_sql:
            continue
        if "'teacher_self_serve'" not in existing_sql:
            # Neither the old spelling nor the new one — some other schema is
            # in play (test harness? partial migration?). Skip conservatively.
            continue
        # Rebuild. Wrap in an explicit transaction; on any failure, roll back
        # so the old table is left intact.
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            # Compose the new CREATE TABLE by string-substituting the CHECK.
            new_create = existing_sql.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}__new")
            new_create = new_create.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}__new")
            # Swap the old CHECK for the new one. The old CHECK is the substring
            # between "CHECK (" + column + " IN " and the closing ")".
            import re as _re
            new_create = _re.sub(
                rf"CHECK \({column} IN \([^)]*\)\)",
                new_check,
                new_create,
                count=1,
            )
            conn.execute(new_create)
            # Copy every column across (SELECT * matches the shadow table's
            # column order because we didn't change the column list).
            conn.execute(f"INSERT INTO {table}__new SELECT * FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__new RENAME TO {table}")
            for idx_sql in extra_indexes:
                conn.execute(idx_sql)
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


# ---------------------------------------------------------------------------
# Seed helpers — create the minimum tenant/user/teacher/rubric context needed
# to attach an observation. In production these come from real user actions.
# ---------------------------------------------------------------------------


def get_or_create_org(conn: sqlite3.Connection, *, slug: str, name: str) -> str:
    row = conn.execute("SELECT id FROM organizations WHERE slug = ?", (slug,)).fetchone()
    if row:
        return row["id"]
    org_id = _new_id()
    conn.execute(
        """INSERT INTO organizations (id, name, slug, plan_tier, created_at)
           VALUES (?, ?, ?, 'pilot', ?)""",
        (org_id, name, slug, _now_iso()),
    )
    conn.commit()
    return org_id


def get_or_create_user(
    conn: sqlite3.Connection, *, org_id: str, email: str, name: str, role: str = "coach"
) -> str:
    row = conn.execute(
        "SELECT id FROM users WHERE org_id = ? AND email = ?", (org_id, email)
    ).fetchone()
    if row:
        return row["id"]
    user_id = _new_id()
    conn.execute(
        """INSERT INTO users (id, org_id, email, name, role, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, org_id, email, name, role, _now_iso()),
    )
    conn.commit()
    return user_id


class ArchivedTeacherError(ValueError):
    """Raised when the name lookup matches an archived teacher — refuses to
    silently create a divergent second row or resurrect the archived one.
    Caller must restore the teacher explicitly before observations can land.
    """


def get_or_create_teacher(
    conn: sqlite3.Connection, *, org_id: str, name: str, coach_user_id: Optional[str] = None
) -> str:
    # Prefer an unarchived match. If the only match is archived, refuse —
    # otherwise we'd silently reattach observations to a soft-deleted row,
    # invisible to every aggregate (roster / compliance / dashboards) but
    # still accumulating work. The archived-teacher signal is meant to be
    # a decision point ("restore or create fresh?"), not a no-op.
    active = conn.execute(
        "SELECT id FROM teachers WHERE org_id = ? AND name = ? AND archived_at IS NULL",
        (org_id, name),
    ).fetchone()
    if active:
        return active["id"]
    archived = conn.execute(
        "SELECT id FROM teachers WHERE org_id = ? AND name = ? AND archived_at IS NOT NULL",
        (org_id, name),
    ).fetchone()
    if archived:
        raise ArchivedTeacherError(
            f"A teacher named {name!r} exists but is archived. "
            f"Restore that record or use a different name."
        )
    teacher_id = _new_id()
    conn.execute(
        """INSERT INTO teachers (id, org_id, name, assigned_coach_user_id, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (teacher_id, org_id, name, coach_user_id, _now_iso()),
    )
    conn.commit()
    return teacher_id


def bulk_create_teacher(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    name: str,
    email: Optional[str] = None,
    employee_id: Optional[str] = None,
    assigned_coach_user_id: Optional[str] = None,
    grade_levels: Optional[list] = None,
    subjects: Optional[list] = None,
) -> tuple[str, bool]:
    """Bulk-import a teacher. Idempotent on ``(org_id, email)`` and
    ``(org_id, employee_id)``. Returns ``(teacher_id, was_created)`` — when
    a match is found the existing row is left in place (create-only; we don't
    silently overwrite coach-authored profile data on a re-upload).

    Raises :class:`ArchivedTeacherError` when the match is on an archived
    teacher; caller catches and reports it as a per-row error rather than
    surfacing it to end users.
    """
    # Run BOTH lookups (not short-circuit on email) so a cross-match — where
    # the email lands on one existing teacher but the employee_id lands on a
    # different one — surfaces as an error the caller can report per-row.
    # Silently returning either match would coalesce two distinct people
    # under the row's payload.
    by_email = None
    if email:
        by_email = conn.execute(
            "SELECT id, archived_at FROM teachers WHERE org_id = ? AND email = ?",
            (org_id, email),
        ).fetchone()
    by_empid = None
    if employee_id:
        by_empid = conn.execute(
            "SELECT id, archived_at FROM teachers WHERE org_id = ? AND employee_id = ?",
            (org_id, employee_id),
        ).fetchone()
    if by_email and by_empid and by_email["id"] != by_empid["id"]:
        raise ValueError(
            f"CSV row is ambiguous: email {email!r} matches an existing teacher, "
            f"but employee_id {employee_id!r} matches a different one. "
            f"Fix the row so both keys point at the same person, or leave one blank."
        )
    match = by_email or by_empid
    if match and match["archived_at"]:
        raise ArchivedTeacherError(
            f"A teacher matching this row exists but is archived. "
            f"Restore that record or use a different email / employee id."
        )
    if match:
        return match["id"], False

    teacher_id = _new_id()
    conn.execute(
        """INSERT INTO teachers
               (id, org_id, name, email, employee_id, assigned_coach_user_id,
                grade_levels, subjects, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            teacher_id, org_id, name, email, employee_id, assigned_coach_user_id,
            json.dumps(grade_levels) if grade_levels else None,
            json.dumps(subjects) if subjects else None,
            _now_iso(),
        ),
    )
    conn.commit()
    return teacher_id, True


def get_or_create_rubric_from_id(
    conn: sqlite3.Connection, *, org_id: Optional[str], rubric_id_kind: str
) -> str:
    """Register the built-in rubric (identified by its ``kind`` string, e.g.
    ``tntp_core_4pt_2014``) as a rubric row. Idempotent per (org_id, kind).
    """
    from .rubric import get_rubric

    rubric = get_rubric(rubric_id_kind)
    row = conn.execute(
        "SELECT id FROM rubrics WHERE (org_id IS ? OR org_id = ?) AND kind = ?",
        (org_id, org_id, rubric.id),
    ).fetchone()
    if row:
        return row["id"]

    db_rubric_id = _new_id()
    # Serialize the Rubric dataclass to JSON — only fields useful for round-trip.
    config = {
        "id": rubric.id,
        "name": rubric.name,
        "pdf_path": str(rubric.pdf_path),
        "domains": rubric.domains,
        "rating_levels": rubric.rating_levels,
        "essential_questions": rubric.essential_questions,
        "core_teacher_skill_note": rubric.core_teacher_skill_note,
        "vocabulary_examples": rubric.vocabulary_examples,
        "coaching_philosophy": rubric.coaching_philosophy,
        "scoring_notes": rubric.scoring_notes,
    }
    conn.execute(
        """INSERT INTO rubrics (id, org_id, kind, name, version, pdf_ref, config_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            db_rubric_id,
            org_id,
            rubric.id,
            rubric.name,
            "2014",
            str(rubric.pdf_path),
            json.dumps(config),
            _now_iso(),
        ),
    )
    conn.commit()
    return db_rubric_id


# ---------------------------------------------------------------------------
# Import an existing reports/*/scores.json into the DB
# ---------------------------------------------------------------------------


def import_scores_json(
    conn: sqlite3.Connection,
    report_dir: Path,
    *,
    org_slug: str = "default",
    org_name: str = "Default Org",
    observer_email: str = "coach@example.com",
    observer_name: str = "Import Coach",
    teacher_name_override: Optional[str] = None,
) -> str:
    """Import a single reports/<folder>/scores.json into the SQLite DB.

    Creates the minimum tenant scaffolding (org, coach user, teacher, rubric)
    on the fly. Returns the observation id. Idempotent per report folder — if
    an observation for the same folder already exists, its report is updated
    (new version_number).
    """
    scores_path = report_dir / "scores.json"
    if not scores_path.exists():
        raise FileNotFoundError(f"No scores.json in {report_dir}")

    data = json.loads(scores_path.read_text())
    meta = data.get("metadata", {})
    report = data["report"]

    # Derive teacher name from folder ("summey_test" -> "Summey", etc.)
    if teacher_name_override:
        teacher_name = teacher_name_override
    else:
        teacher_name = report_dir.name.split("_")[0].replace("-", " ").title()

    # Rubric id from metadata (new schema) or default to TNTP for legacy reports.
    rubric_kind = meta.get("rubric_id") or "tntp_core_4pt_2014"

    org_id = get_or_create_org(conn, slug=org_slug, name=org_name)
    coach_id = get_or_create_user(
        conn, org_id=org_id, email=observer_email, name=observer_name, role="coach"
    )
    # Cross-caseload guard: if a teacher of the given name exists in this
    # org and is already assigned to a DIFFERENT coach, the import would
    # silently attach the imported observation to another coach's caseload.
    # Refuse loudly so a stray baseline import doesn't quietly show up on a
    # real coach's teacher page. (Web upload path has the same guard —
    # matches app/main.py:_upload flow post-round-8.)
    existing_t = conn.execute(
        """SELECT id, assigned_coach_user_id FROM teachers
           WHERE org_id = ? AND name = ? AND archived_at IS NULL""",
        (org_id, teacher_name),
    ).fetchone()
    if (
        existing_t
        and existing_t["assigned_coach_user_id"]
        and existing_t["assigned_coach_user_id"] != coach_id
    ):
        raise ValueError(
            f"import: teacher {teacher_name!r} is already on another coach's caseload "
            f"in this org. Pass --observer-email matching the existing coach, or use "
            f"a distinguishing --teacher-name."
        )
    teacher_id = get_or_create_teacher(
        conn, org_id=org_id, name=teacher_name, coach_user_id=coach_id
    )
    rubric_id = get_or_create_rubric_from_id(conn, org_id=None, rubric_id_kind=rubric_kind)

    # Consent: the web upload path refuses observations for teachers without
    # an active consent record (post-round-8). The import path is used to
    # replay baselines from disk — often teachers who never signed in to
    # grant consent themselves. Auto-grant here on their behalf and mark
    # the record with the import context so it's distinguishable from a
    # real teacher-authored grant.
    if get_active_consent(conn, teacher_id=teacher_id) is None:
        grant_consent(
            conn, org_id=org_id, teacher_id=teacher_id,
            granted_by_user_id=coach_id,
            form_version="import-auto",
        )

    # Look for an existing observation with the same source folder marker.
    # We stash the folder name in the video_ref column as a stable-per-folder id.
    folder_marker = f"reports/{report_dir.name}"
    existing = conn.execute(
        "SELECT id FROM observations WHERE video_ref = ? AND org_id = ?",
        (folder_marker, org_id),
    ).fetchone()

    if existing:
        observation_id = existing["id"]
        conn.execute(
            """UPDATE observations SET
                   rubric_id = ?,
                   video_filename = ?,
                   video_duration_s = ?,
                   transcript_model = ?,
                   frame_count = ?,
                   frame_interval_s = ?,
                   status = 'complete',
                   scored_at = ?
               WHERE id = ?""",
            (
                rubric_id,
                meta.get("video_filename", ""),
                meta.get("duration_seconds", 0),
                meta.get("transcription_model", ""),
                meta.get("frame_count", 0),
                meta.get("frame_interval_seconds", 60.0),
                meta.get("scored_at", _now_iso()),
                observation_id,
            ),
        )
    else:
        observation_id = _new_id()
        conn.execute(
            """INSERT INTO observations
                   (id, org_id, teacher_id, observer_user_id, rubric_id,
                    video_ref, video_filename, video_duration_s,
                    transcript_model, frame_count, frame_interval_s,
                    status, uploaded_at, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', ?, ?)""",
            (
                observation_id, org_id, teacher_id, coach_id, rubric_id,
                folder_marker,
                meta.get("video_filename", ""),
                meta.get("duration_seconds", 0),
                meta.get("transcription_model", ""),
                meta.get("frame_count", 0),
                meta.get("frame_interval_seconds", 60.0),
                _now_iso(),
                meta.get("scored_at", _now_iso()),
            ),
        )

    # Report version — bump number if updating.
    max_ver = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS m FROM report_versions WHERE observation_id = ?",
        (observation_id,),
    ).fetchone()["m"]
    new_ver = max_ver + 1

    # Read a pre-rendered report.md if present; otherwise leave null.
    report_md_path = report_dir / "report.md"
    rendered_md = report_md_path.read_text() if report_md_path.exists() else None

    # Unpublish any prior version BEFORE inserting the new one. The partial
    # unique index ``uq_rv_one_published`` allows at most one row per
    # observation with ``published_at IS NOT NULL``; the second import
    # against the same folder would otherwise INSERT a v2 alongside a
    # still-published v1 and blow up with IntegrityError, marking the
    # import "failed" despite the docstring's idempotency promise. Match
    # the shape _persist_report uses for the web path.
    conn.execute(
        "UPDATE report_versions SET published_at = NULL WHERE observation_id = ?",
        (observation_id,),
    )

    # Auto-publish v1 in the single-user semantics of the local app. When a
    # multi-user coach-edit workflow lands, this becomes a decision point.
    ts = _now_iso()
    conn.execute(
        """INSERT INTO report_versions
               (id, observation_id, version_number, authored_by,
                opening_paragraph, overall_summary,
                domain_assessments, coaching_recommendations,
                rendered_markdown, published_at, created_at)
           VALUES (?, ?, ?, 'ai', ?, ?, ?, ?, ?, ?, ?)""",
        (
            _new_id(),
            observation_id,
            new_ver,
            report.get("opening_paragraph", ""),
            report.get("overall_summary", ""),
            json.dumps(report.get("domain_assessments", [])),
            json.dumps(report.get("coaching_recommendations", [])),
            rendered_md,
            ts,
            ts,
        ),
    )

    # Audit log the import action. Namespaced action so a future _is_sample_db
    # check (see tools/seed_sample_data.py) can distinguish an imported-
    # baseline DB from a real pilot one.
    conn.execute(
        """INSERT INTO audit_log
               (id, org_id, actor_user_id, action, target_type, target_id, metadata, occurred_at)
           VALUES (?, ?, ?, 'import:baseline:report', 'observation', ?, ?, ?)""",
        (
            _new_id(), org_id, coach_id, observation_id,
            json.dumps({"version_number": new_ver, "source_folder": report_dir.name}),
            _now_iso(),
        ),
    )

    conn.commit()
    return observation_id


# ---------------------------------------------------------------------------
# Query helpers — used to verify the schema round-trips correctly
# ---------------------------------------------------------------------------


def list_observations(conn: sqlite3.Connection, *, org_id: Optional[str] = None) -> list[sqlite3.Row]:
    """List observations, optionally scoped to one org (proves the multi-tenant contract)."""
    if org_id:
        rows = conn.execute(
            """SELECT o.id AS obs_id, t.name AS teacher, o.video_filename, o.video_duration_s,
                      o.status, o.scored_at,
                      (SELECT MAX(version_number) FROM report_versions rv WHERE rv.observation_id = o.id) AS latest_version
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               WHERE o.org_id = ? AND o.deleted_at IS NULL
               ORDER BY o.scored_at DESC""",
            (org_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT o.id AS obs_id, t.name AS teacher, o.video_filename, o.video_duration_s,
                      o.status, o.scored_at,
                      (SELECT MAX(version_number) FROM report_versions rv WHERE rv.observation_id = o.id) AS latest_version
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               WHERE o.deleted_at IS NULL
               ORDER BY o.scored_at DESC"""
        ).fetchall()
    return rows


def get_latest_report(conn: sqlite3.Connection, observation_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM report_versions
           WHERE observation_id = ?
           ORDER BY version_number DESC
           LIMIT 1""",
        (observation_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Phase A: relational-context helpers
# ---------------------------------------------------------------------------


# Recommended self-rating dimensions. Districts can extend the JSON blob with
# their own dimensions; these are the sensible defaults presented in the UI.
SELF_RATING_DIMENSIONS = (
    "classroom_management",
    "content_expertise",
    "student_engagement",
    "formative_assessment",
    "family_communication",
)


def upsert_teacher_profile_teacher_side(
    conn: sqlite3.Connection,
    *,
    teacher_id: str,
    years_teaching_total: Optional[int] = None,
    years_teaching_subject: Optional[int] = None,
    years_at_current_school: Optional[int] = None,
    highest_credential: Optional[str] = None,
    subjects_taught: Optional[list] = None,
    grade_levels_taught: Optional[list] = None,
    self_ratings: Optional[dict] = None,
    coaching_style_preference: Optional[str] = None,
    career_narrative_notes: Optional[str] = None,
    career_goals_notes: Optional[str] = None,
) -> None:
    """Insert or update the teacher-authored side of a teacher profile.

    Coach-side fields are untouched. Idempotent — call whenever the teacher
    saves the profile edit form.
    """
    existing = conn.execute(
        "SELECT teacher_id FROM teacher_profiles WHERE teacher_id = ?", (teacher_id,)
    ).fetchone()
    payload = {
        "years_teaching_total": years_teaching_total,
        "years_teaching_subject": years_teaching_subject,
        "years_at_current_school": years_at_current_school,
        "highest_credential": highest_credential,
        "subjects_taught": json.dumps(subjects_taught) if subjects_taught is not None else None,
        "grade_levels_taught": json.dumps(grade_levels_taught) if grade_levels_taught is not None else None,
        "self_ratings": json.dumps(self_ratings) if self_ratings is not None else None,
        "coaching_style_preference": coaching_style_preference,
        "career_narrative_notes": career_narrative_notes,
        "career_goals_notes": career_goals_notes,
        "teacher_updated_at": _now_iso(),
    }
    if existing:
        cols = ", ".join(f"{k} = ?" for k in payload)
        conn.execute(
            f"UPDATE teacher_profiles SET {cols} WHERE teacher_id = ?",
            (*payload.values(), teacher_id),
        )
    else:
        conn.execute(
            f"""INSERT INTO teacher_profiles
                (teacher_id, {', '.join(payload.keys())})
                VALUES ({', '.join(['?'] * (1 + len(payload)))})""",
            (teacher_id, *payload.values()),
        )
    conn.commit()


def upsert_teacher_profile_coach_side(
    conn: sqlite3.Connection,
    *,
    teacher_id: str,
    coach_notes_on_teacher: Optional[str] = None,
    observed_style_notes: Optional[str] = None,
    coach_ratings: Optional[dict] = None,
    coach_ratings_context: Optional[dict] = None,
    skill_development_narrative: Optional[str] = None,
) -> None:
    """Insert or update the coach-authored side of a teacher profile (public,
    visible to teacher). Coach's private observations belong in coach_private_notes.
    """
    existing = conn.execute(
        "SELECT teacher_id FROM teacher_profiles WHERE teacher_id = ?", (teacher_id,)
    ).fetchone()
    ts = _now_iso()
    coach_ratings_json = json.dumps(coach_ratings) if coach_ratings is not None else None
    coach_ratings_ctx_json = (
        json.dumps(coach_ratings_context) if coach_ratings_context is not None else None
    )
    if existing:
        conn.execute(
            """UPDATE teacher_profiles SET
                   coach_notes_on_teacher = ?,
                   observed_style_notes = ?,
                   coach_ratings = ?,
                   coach_ratings_context = ?,
                   skill_development_narrative = ?,
                   coach_updated_at = ?
               WHERE teacher_id = ?""",
            (coach_notes_on_teacher, observed_style_notes,
             coach_ratings_json, coach_ratings_ctx_json,
             skill_development_narrative, ts, teacher_id),
        )
    else:
        conn.execute(
            """INSERT INTO teacher_profiles
                   (teacher_id, coach_notes_on_teacher, observed_style_notes,
                    coach_ratings, coach_ratings_context, skill_development_narrative,
                    coach_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (teacher_id, coach_notes_on_teacher, observed_style_notes,
             coach_ratings_json, coach_ratings_ctx_json,
             skill_development_narrative, ts),
        )
    conn.commit()


def get_teacher_profile(conn: sqlite3.Connection, teacher_id: str) -> dict:
    """Return the merged profile as a dict. JSON fields are decoded.

    Missing profile returns an empty dict with just the teacher_id.
    """
    row = conn.execute(
        "SELECT * FROM teacher_profiles WHERE teacher_id = ?", (teacher_id,)
    ).fetchone()
    if not row:
        return {"teacher_id": teacher_id}
    d = dict(row)
    for f in ("subjects_taught", "grade_levels_taught", "self_ratings",
              "coach_ratings", "coach_ratings_context"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                pass
    return d


def create_goal(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    teacher_id: str,
    coaching_cycle_id: Optional[str],
    title: str,
    description: Optional[str],
    proposed_by: str,
    success_indicators: Optional[list] = None,
    teacher_notes: Optional[str] = None,
    coach_notes: Optional[str] = None,
    related_rubric_domain: Optional[str] = None,
    related_core_teacher_skill: Optional[str] = None,
) -> str:
    """Create a new professional goal in 'proposed' status."""
    if proposed_by not in ("teacher", "coach", "joint"):
        raise ValueError(f"Invalid proposed_by: {proposed_by!r}")
    goal_id = _new_id()
    conn.execute(
        """INSERT INTO professional_goals
               (id, org_id, teacher_id, coaching_cycle_id, title, description,
                success_indicators, proposed_by, teacher_notes, coach_notes,
                related_rubric_domain, related_core_teacher_skill,
                status, proposed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
        (
            goal_id, org_id, teacher_id, coaching_cycle_id, title, description,
            json.dumps(success_indicators) if success_indicators else None,
            proposed_by, teacher_notes, coach_notes,
            related_rubric_domain, related_core_teacher_skill,
            _now_iso(),
        ),
    )
    conn.commit()
    return goal_id


class GoalTransitionError(ValueError):
    """Raised when a goal-lifecycle transition is illegal (agree on closed,
    close on already-closed, etc.). Caller surfaces as 409.
    """


def agree_goal(conn: sqlite3.Connection, goal_id: str) -> None:
    """Mark a goal as agreed and active. One-way transition: only valid on
    a goal currently in ``proposed`` status. A double-submit on an already-
    active goal is a no-op; a submit on a closed/abandoned goal raises so
    the coach doesn't accidentally resurrect it (which would double-count
    it in the district's closed-outcomes rollup and reset lifecycle history).
    """
    row = conn.execute(
        "SELECT status FROM professional_goals WHERE id = ?", (goal_id,)
    ).fetchone()
    if not row:
        raise GoalTransitionError(f"Goal {goal_id!r} not found")
    if row["status"] == "active":
        return  # idempotent
    if row["status"] != "proposed":
        raise GoalTransitionError(
            f"Cannot agree to goal in status {row['status']!r} — only 'proposed' goals accept agreement."
        )
    conn.execute(
        "UPDATE professional_goals SET status = 'active', agreed_at = ?, updated_at = ? WHERE id = ?",
        (_now_iso(), _now_iso(), goal_id),
    )
    conn.commit()


def close_goal(
    conn: sqlite3.Connection,
    goal_id: str,
    outcome: str,
    *,
    indicators_met: Optional[list] = None,
    outcome_reason: Optional[str] = None,
    outcome_notes: Optional[str] = None,
) -> None:
    """Close a goal with an outcome. Valid outcomes: closed_met / closed_partial /
    closed_unmet / abandoned.

    Optional structured fields:
    - ``indicators_met``: list of exact success-indicator strings that were met.
      Used to compute a defensible partial-met assessment.
    - ``outcome_reason``: enum-ish tag for closed_unmet — 'not_actionable',
      'not_prioritized', 'attempted_didnt_land', 'external_blockers', 'other'.
    - ``outcome_notes``: free-text detail from the coach.
    """
    if outcome not in ("closed_met", "closed_partial", "closed_unmet", "abandoned"):
        raise ValueError(f"Invalid outcome: {outcome!r}")
    conn.execute(
        """UPDATE professional_goals
           SET status = ?, closed_at = ?, updated_at = ?,
               closed_indicators_met = ?, closed_outcome_reason = ?, closed_outcome_notes = ?
           WHERE id = ?""",
        (outcome, _now_iso(), _now_iso(),
         json.dumps(indicators_met) if indicators_met else None,
         outcome_reason, outcome_notes,
         goal_id),
    )
    conn.commit()


def carry_forward_goal(
    conn: sqlite3.Connection,
    *,
    from_goal_id: str,
    new_coaching_cycle_id: Optional[str] = None,
) -> str:
    """Seed a NEW proposed goal from an unmet-or-partial one — same skill focus,
    linked back to the source via ``carried_forward_from_goal_id``.
    Coach can then edit before agreeing.
    """
    src = conn.execute(
        "SELECT * FROM professional_goals WHERE id = ?", (from_goal_id,)
    ).fetchone()
    if not src:
        raise ValueError(f"Source goal not found: {from_goal_id!r}")
    new_id = _new_id()
    conn.execute(
        """INSERT INTO professional_goals
           (id, org_id, teacher_id, coaching_cycle_id, title, description,
            success_indicators, proposed_by, status, coach_notes, teacher_notes,
            related_rubric_domain, related_core_teacher_skill,
            releasing, carried_forward_from_goal_id, proposed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'coach', 'proposed', ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id, src["org_id"], src["teacher_id"],
            new_coaching_cycle_id,
            f"[Carried forward] {src['title']}",
            (src["description"] or "") + "\n\n(Carried forward from a prior unmet/partial cycle.)",
            src["success_indicators"],  # keep the same JSON indicators
            (src["coach_notes"] or ""),
            (src["teacher_notes"] or ""),
            src["related_rubric_domain"],
            src["related_core_teacher_skill"],
            src["releasing"],
            from_goal_id,
            _now_iso(),
        ),
    )
    conn.commit()
    return new_id


def list_actions_for_goal(conn: sqlite3.Connection, goal_id: str) -> list:
    rows = conn.execute(
        """SELECT * FROM bite_sized_action_tracking
           WHERE goal_id = ? ORDER BY created_at ASC""",
        (goal_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_action_goal(
    conn: sqlite3.Connection, *, action_id: str, goal_id: Optional[str]
) -> None:
    """Link (or unlink) an existing bite-sized action to a goal."""
    conn.execute(
        "UPDATE bite_sized_action_tracking SET goal_id = ? WHERE id = ?",
        (goal_id, action_id),
    )
    conn.commit()


def list_goals_for_teacher(
    conn: sqlite3.Connection, teacher_id: str, *, active_only: bool = False
) -> list:
    """List goals for a teacher, most recent first. Success indicators decoded."""
    if active_only:
        rows = conn.execute(
            """SELECT * FROM professional_goals
               WHERE teacher_id = ? AND status IN ('proposed', 'active')
               ORDER BY proposed_at DESC""",
            (teacher_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM professional_goals WHERE teacher_id = ? ORDER BY proposed_at DESC",
            (teacher_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("success_indicators"):
            try:
                d["success_indicators"] = json.loads(d["success_indicators"])
            except Exception:
                pass
        if d.get("closed_indicators_met"):
            try:
                d["closed_indicators_met"] = json.loads(d["closed_indicators_met"])
            except Exception:
                pass
        out.append(d)
    return out


def upsert_district_context(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    academic_year: str,
    year_arc: Optional[list] = None,
    district_priorities: Optional[list] = None,
    district_initiatives: Optional[list] = None,
    year_start_date: Optional[str] = None,
    year_end_date: Optional[str] = None,
    observations_per_year_target: Optional[int] = None,
    days_between_obs_target: Optional[int] = None,
    days_from_obs_to_debrief_target: Optional[int] = None,
) -> str:
    """Insert or update the district context for one org+academic_year."""
    existing = conn.execute(
        "SELECT id FROM district_context WHERE org_id = ? AND academic_year = ?",
        (org_id, academic_year),
    ).fetchone()
    payload = {
        "year_start_date": year_start_date,
        "year_end_date": year_end_date,
        "year_arc_json": json.dumps(year_arc) if year_arc is not None else None,
        "district_priorities_json": json.dumps(district_priorities) if district_priorities is not None else None,
        "district_initiatives_json": json.dumps(district_initiatives) if district_initiatives is not None else None,
        "observations_per_year_target": observations_per_year_target,
        "days_between_obs_target": days_between_obs_target,
        "days_from_obs_to_debrief_target": days_from_obs_to_debrief_target,
        "updated_at": _now_iso(),
    }
    if existing:
        cols = ", ".join(f"{k} = ?" for k in payload)
        conn.execute(
            f"UPDATE district_context SET {cols} WHERE id = ?",
            (*payload.values(), existing["id"]),
        )
        ctx_id = existing["id"]
    else:
        ctx_id = _new_id()
        conn.execute(
            f"""INSERT INTO district_context
                (id, org_id, academic_year, created_at, {', '.join(payload.keys())})
                VALUES (?, ?, ?, ?, {', '.join(['?'] * len(payload))})""",
            (ctx_id, org_id, academic_year, _now_iso(), *payload.values()),
        )
    conn.commit()
    return ctx_id


def get_district_context(
    conn: sqlite3.Connection, *, org_id: str, academic_year: str
) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM district_context WHERE org_id = ? AND academic_year = ?",
        (org_id, academic_year),
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    for f in ("year_arc_json", "district_priorities_json", "district_initiatives_json"):
        if d.get(f):
            try:
                d[f] = json.loads(d[f])
            except Exception:
                pass
    return d


def add_coach_private_note(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    author_user_id: str,
    teacher_id: str,
    body: str,
    observation_id: Optional[str] = None,
) -> str:
    """Add a private note (coach-only visibility)."""
    note_id = _new_id()
    conn.execute(
        """INSERT INTO coach_private_notes
               (id, org_id, observation_id, teacher_id, author_user_id, body,
                visibility, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'coach_only', ?)""",
        (note_id, org_id, observation_id, teacher_id, author_user_id, body, _now_iso()),
    )
    conn.commit()
    return note_id


def list_private_notes_for_teacher(
    conn: sqlite3.Connection,
    *,
    teacher_id: str,
    caller_role: str,
) -> list:
    """Return private notes for a teacher. Enforces visibility rules.

    Only callers with role 'coach' see notes. Admin and teacher get [].
    """
    if caller_role != "coach":
        return []
    rows = conn.execute(
        "SELECT * FROM coach_private_notes WHERE teacher_id = ? ORDER BY created_at DESC",
        (teacher_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_private_note(
    conn: sqlite3.Connection, *, note_id: str, author_user_id: str
) -> bool:
    """Hard-delete a coach's private note. Author-only — the row is removed
    ONLY if ``author_user_id`` matches the note's author. Returns True on
    delete, False on no-op (note missing or author mismatch).

    Coach private notes are the coach's own scratchpad — not a record — so
    when the coach wants one gone, gone. Author guard is defense-in-depth
    against a URL-forge in a future multi-coach world; today single-user auth
    trivially satisfies it.
    """
    row = conn.execute(
        "SELECT author_user_id FROM coach_private_notes WHERE id = ?", (note_id,)
    ).fetchone()
    if not row or row["author_user_id"] != author_user_id:
        return False
    conn.execute("DELETE FROM coach_private_notes WHERE id = ?", (note_id,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Archive / restore — soft-delete flows.
#
# Teachers archive with a cascade: any open cycle closes with a canonical note,
# any active/proposed goal transitions to `abandoned`, any in-flight lesson
# plan flips to `archived`. Observations stay in place (they're history; the
# aggregate queries filter `t.archived_at IS NULL` at the teacher level, so
# the observations naturally drop out of dashboards along with the teacher).
#
# Observations archive using the existing `deleted_at` column — mis-uploads
# happen (wrong teacher, bad video, uploaded twice), and every aggregate query
# filters `o.deleted_at IS NULL` so an archived observation stops contributing
# to rating rollups, cycle counts, and compliance stats. By-id lookups still
# work so the coach can restore.
#
# Restore for teachers un-archives the teacher record but does NOT un-cascade —
# a restored teacher starts fresh (coach opens a new cycle, proposes a new
# goal). That matches how archive-then-restore reads in coaching contexts.
# Restore for observations does un-do the delete: `deleted_at` clears and the
# observation re-enters aggregates.
# ---------------------------------------------------------------------------


def archive_teacher(
    conn: sqlite3.Connection, *, teacher_id: str
) -> dict:
    """Archive a teacher and cascade-close their in-flight work.

    Cascade actions (all inside one transaction):
      - `teachers.archived_at` = now
      - Any open coaching cycle → closed with note "Auto-closed on teacher archive"
      - Any `proposed` or `active` professional goal → status `abandoned`
      - Any lesson plan in status `submitted` / `coach_reviewed` /
        `revision_requested` → status `archived`

    Returns a summary dict of what changed:
      ``{"cycles_closed": int, "goals_abandoned": int, "plans_archived": int,
         "already_archived": bool}``

    Idempotent: archiving an already-archived teacher is a no-op (returns
    ``already_archived: True`` with all counts zero).
    """
    row = conn.execute(
        "SELECT archived_at FROM teachers WHERE id = ?", (teacher_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Teacher {teacher_id!r} not found")
    if row["archived_at"]:
        return {
            "cycles_closed": 0, "goals_abandoned": 0,
            "plans_archived": 0, "already_archived": True,
        }

    now = _now_iso()

    # Cycles — auto-close with a canonical note. Use close_cycle so the notes
    # marker matches the format elsewhere and so growth_story is left alone.
    open_cycles = conn.execute(
        "SELECT id FROM coaching_cycles WHERE teacher_id = ? AND closed_at IS NULL",
        (teacher_id,),
    ).fetchall()
    for c in open_cycles:
        try:
            close_cycle(
                conn, c["id"],
                closing_notes="Auto-closed on teacher archive",
            )
        except CycleAlreadyClosedError:
            # Race with a concurrent close — fine, already handled.
            pass

    # Goals — abandon any proposed/active. Skip the transition guard by using
    # close_goal with the abandoned outcome, which the guard accepts.
    open_goals = conn.execute(
        """SELECT id FROM professional_goals
           WHERE teacher_id = ? AND status IN ('proposed', 'active')""",
        (teacher_id,),
    ).fetchall()
    for g in open_goals:
        close_goal(
            conn, g["id"], "abandoned",
            outcome_notes="Auto-abandoned on teacher archive",
        )

    # Lesson plans — flip in-flight statuses to archived. Approved plans stay
    # approved (they're a record of what was taught); drafts stay drafts.
    in_flight_plans = conn.execute(
        """SELECT id FROM lesson_plans
           WHERE teacher_id = ?
             AND status IN ('submitted', 'coach_reviewed', 'revision_requested')""",
        (teacher_id,),
    ).fetchall()
    for p in in_flight_plans:
        update_lesson_plan_status(conn, p["id"], "archived")

    # Finally, stamp the teacher.
    conn.execute(
        "UPDATE teachers SET archived_at = ? WHERE id = ?",
        (now, teacher_id),
    )
    conn.commit()

    return {
        "cycles_closed": len(open_cycles),
        "goals_abandoned": len(open_goals),
        "plans_archived": len(in_flight_plans),
        "already_archived": False,
    }


def restore_teacher(conn: sqlite3.Connection, *, teacher_id: str) -> None:
    """Un-archive a teacher. Does NOT un-cascade: any cycles/goals/plans
    closed on archive stay closed. A restored teacher starts fresh — coach
    opens a new cycle, proposes a new goal.
    """
    conn.execute(
        "UPDATE teachers SET archived_at = NULL WHERE id = ? AND archived_at IS NOT NULL",
        (teacher_id,),
    )
    conn.commit()


def archive_observation(conn: sqlite3.Connection, *, observation_id: str) -> None:
    """Soft-delete an observation. Sets ``deleted_at`` so aggregate queries
    stop counting it (rating rollups, cycle counts, compliance stats). The
    row stays on disk; by-id lookups still work so the coach can restore.
    Idempotent: a re-archive updates ``deleted_at`` (harmless — value is
    only read for its truthiness).
    """
    row = conn.execute(
        "SELECT id FROM observations WHERE id = ?", (observation_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Observation {observation_id!r} not found")
    conn.execute(
        "UPDATE observations SET deleted_at = ? WHERE id = ?",
        (_now_iso(), observation_id),
    )
    conn.commit()


def restore_observation(conn: sqlite3.Connection, *, observation_id: str) -> None:
    """Un-archive an observation: clears ``deleted_at`` so it re-enters
    aggregates.
    """
    conn.execute(
        "UPDATE observations SET deleted_at = NULL WHERE id = ?",
        (observation_id,),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Auth: sessions, magic-link tokens, outbound mail
#
# Magic-link flow the routes use:
#   1. /signin POST → create_magic_link_token(email, next_url)
#      → queue_email(magic_link_email(...))
#   2. /auth/{token} GET → consume_magic_link_token(token) returns user_id
#      → create_session(user_id) returns session_id
#      → set HttpOnly cookie, redirect to next_url
#   3. Any request → get_session(cookie) returns user_id + role + org
#      → renews last_seen_at on read
#   4. /signout POST → revoke_session(cookie)
# ---------------------------------------------------------------------------


import secrets as _secrets
from datetime import timedelta as _tdelta


SESSION_TTL_DAYS = 30            # cookie survives a month idle
MAGIC_LINK_TTL_MINUTES = 15      # short — clicking-through-email doesn't need longer
SESSION_RENEW_AFTER_MIN = 5      # only rewrite last_seen_at every N min (write reduction)


def _new_token(nbytes: int = 32) -> str:
    """URL-safe random token. 32 bytes → 43-char base64url string. Used for
    both session ids and magic-link tokens; enough entropy that guessing is
    strictly worse than compromising the cookie transport.
    """
    return _secrets.token_urlsafe(nbytes)


def create_magic_link_token(
    conn: sqlite3.Connection, *, email: str, next_url: Optional[str] = None,
    ip: Optional[str] = None, purpose: str = "signin",
) -> tuple[str, Optional[str]]:
    """Mint a single-use magic-link token for the given email.

    Returns ``(token, user_id_or_None)``. ``user_id`` is resolved when the
    email matches an existing user, otherwise None — the token is still
    minted (so we don't leak "this email is / is not registered"), and the
    /auth/{token} route later refuses to sign in unknown emails.
    """
    row = conn.execute("SELECT id FROM users WHERE lower(email) = lower(?)", (email,)).fetchone()
    user_id = row["id"] if row else None
    token = _new_token()
    now = _now_iso()
    expires = (datetime.now(timezone.utc) + _tdelta(minutes=MAGIC_LINK_TTL_MINUTES)).isoformat()
    conn.execute(
        """INSERT INTO magic_link_tokens
             (id, email, user_id, purpose, next_url, created_at, expires_at, ip)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (token, email.strip().lower(), user_id, purpose, next_url, now, expires, ip),
    )
    conn.commit()
    return token, user_id


def consume_magic_link_token(
    conn: sqlite3.Connection, *, token: str
) -> Optional[dict]:
    """Verify a magic-link token and mark it consumed.

    Returns ``{"user_id": ..., "next_url": ..., "email": ...}`` on success;
    ``None`` when the token is unknown, expired, already consumed, or bound
    to no user (a magic link for an email that doesn't have an account).
    Consumption is atomic — a race between two clicks lands one on ``None``.
    """
    now_dt = datetime.now(timezone.utc)
    row = conn.execute(
        """SELECT id, email, user_id, next_url, expires_at, consumed_at
           FROM magic_link_tokens WHERE id = ?""",
        (token,),
    ).fetchone()
    if not row:
        return None
    if row["consumed_at"] is not None:
        return None
    try:
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if exp < now_dt:
        return None
    if not row["user_id"]:
        # Unknown email — mark consumed so a leaked token can't be reused;
        # returning None makes the route show a generic "check the link" error.
        conn.execute(
            "UPDATE magic_link_tokens SET consumed_at = ? WHERE id = ?",
            (_now_iso(), token),
        )
        conn.commit()
        return None
    # Atomic consume: UPDATE ... WHERE consumed_at IS NULL, check rowcount.
    cur = conn.execute(
        "UPDATE magic_link_tokens SET consumed_at = ? WHERE id = ? AND consumed_at IS NULL",
        (_now_iso(), token),
    )
    conn.commit()
    if cur.rowcount != 1:
        # Lost the race to a concurrent click.
        return None
    return {
        "user_id": row["user_id"],
        "next_url": row["next_url"],
        "email": row["email"],
    }


def create_session(
    conn: sqlite3.Connection, *, user_id: str, ip: Optional[str] = None,
    user_agent: Optional[str] = None, magic_link_token_id: Optional[str] = None,
) -> str:
    """Mint a session for the given user_id. Returns the session token (goes
    in the HttpOnly cookie). Expires SESSION_TTL_DAYS out.
    """
    sid = _new_token()
    now = _now_iso()
    expires = (datetime.now(timezone.utc) + _tdelta(days=SESSION_TTL_DAYS)).isoformat()
    conn.execute(
        """INSERT INTO auth_sessions
             (id, user_id, created_at, last_seen_at, expires_at, ip, user_agent)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (sid, user_id, now, now, expires, ip, user_agent),
    )
    if magic_link_token_id:
        conn.execute(
            "UPDATE magic_link_tokens SET consumed_session_id = ? WHERE id = ?",
            (sid, magic_link_token_id),
        )
    conn.commit()
    return sid


def get_session(conn: sqlite3.Connection, *, session_id: str) -> Optional[dict]:
    """Look up a session by token. Returns the user record joined with
    org info when the session is valid; None when unknown, revoked, or
    expired. Renews ``last_seen_at`` (throttled to avoid a write on every
    request).
    """
    if not session_id:
        return None
    row = conn.execute(
        """SELECT s.id AS session_id, s.user_id, s.expires_at, s.revoked_at, s.last_seen_at,
                  u.email, u.name, u.role, u.org_id,
                  o.name AS org_name, o.slug AS org_slug
           FROM auth_sessions s
           JOIN users u ON u.id = s.user_id
           LEFT JOIN organizations o ON o.id = u.org_id
           WHERE s.id = ?""",
        (session_id,),
    ).fetchone()
    if not row:
        return None
    if row["revoked_at"]:
        return None
    now_dt = datetime.now(timezone.utc)
    try:
        exp = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
    except Exception:
        return None
    if exp < now_dt:
        return None
    # Throttled renewal — only rewrite last_seen_at every SESSION_RENEW_AFTER_MIN.
    try:
        last = datetime.fromisoformat(row["last_seen_at"].replace("Z", "+00:00"))
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now_dt - last).total_seconds() > SESSION_RENEW_AFTER_MIN * 60:
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                (_now_iso(), session_id),
            )
            conn.commit()
    except Exception:
        pass
    return {
        "session_id": row["session_id"],
        "user_id": row["user_id"],
        "email": row["email"],
        "name": row["name"],
        "role": row["role"],
        "org_id": row["org_id"],
        "org_name": row["org_name"],
        "org_slug": row["org_slug"],
    }


def revoke_session(conn: sqlite3.Connection, *, session_id: str) -> None:
    """Explicit sign-out. Marks revoked; a later get_session returns None."""
    if not session_id:
        return
    conn.execute(
        "UPDATE auth_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (_now_iso(), session_id),
    )
    conn.commit()


def queue_email(
    conn: sqlite3.Connection, *, to_email: str, subject: str,
    body_text: str, body_html: Optional[str] = None,
    related_token_id: Optional[str] = None,
) -> str:
    """Write an email into outbound_mail. In pilot this is the delivery
    channel — the dev viewer or a coach's inbox reads from the row. In
    production a sender daemon pulls unsent rows and pumps them out.

    Returns the mail id.
    """
    mid = _new_id()
    conn.execute(
        """INSERT INTO outbound_mail
             (id, to_email, subject, body_text, body_html, created_at, related_token_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (mid, to_email.strip().lower(), subject, body_text, body_html, _now_iso(), related_token_id),
    )
    conn.commit()
    return mid


def list_recent_outbound_mail(conn: sqlite3.Connection, *, limit: int = 20) -> list:
    """Dev-view helper: recent outbound mail, latest first."""
    rows = conn.execute(
        "SELECT * FROM outbound_mail ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Consent — grant / revoke / lookup.
#
# Policy (agreed with coach): one-time org-level consent, revocation blocks
# future observations but keeps history in place. The schema also supports
# per-observation scoping; not used yet.
# ---------------------------------------------------------------------------


def get_active_consent(
    conn: sqlite3.Connection, *, teacher_id: str
) -> Optional[dict]:
    """Return the active (non-revoked) consent for this teacher, or None
    if none exists. The partial-unique index enforces at most one.
    """
    row = conn.execute(
        """SELECT * FROM consent_records
           WHERE teacher_id = ? AND revoked_at IS NULL
           ORDER BY consented_at DESC LIMIT 1""",
        (teacher_id,),
    ).fetchone()
    return dict(row) if row else None


def grant_consent(
    conn: sqlite3.Connection, *, org_id: str, teacher_id: str,
    granted_by_user_id: str, form_version: str = "2026-09-v1",
    scope: str = "perpetual_until_revoked",
) -> str:
    """Record consent. Idempotent: if there's already an active consent for
    this teacher, returns its id without inserting a duplicate.

    Race-safe: the check-then-insert on ``get_active_consent`` alone would
    let two concurrent grants both see "no active consent" and both INSERT,
    with the second hitting ``uq_one_active_consent_per_teacher`` and
    surfacing as a 500. On IntegrityError we re-read and return the winner's
    id — the caller gets the same idempotent behavior either way.
    """
    existing = get_active_consent(conn, teacher_id=teacher_id)
    if existing:
        return existing["id"]
    cid = _new_id()
    try:
        conn.execute(
            """INSERT INTO consent_records
                 (id, org_id, teacher_id, scope, form_version, method,
                  consented_at, granted_by_user_id)
               VALUES (?, ?, ?, ?, ?, 'electronic_signature', ?, ?)""",
            (cid, org_id, teacher_id, scope, form_version, _now_iso(), granted_by_user_id),
        )
        conn.commit()
        return cid
    except sqlite3.IntegrityError:
        # The only unique constraint an INSERT here can trip is the partial
        # index ``uq_one_active_consent_per_teacher``. (Other constraints on
        # this table are NOT NULL / CHECK on scope+method, all pre-validated
        # by the arguments.) SQLite doesn't include the index NAME in the
        # error string — it reports "UNIQUE constraint failed: teacher_id"
        # — so we can't match on name; but the semantics are unambiguous:
        # a concurrent grant slipped in between our check-then-insert. Re-
        # read and return the winner's id, matching the idempotent contract.
        winner = get_active_consent(conn, teacher_id=teacher_id)
        if winner:
            return winner["id"]
        raise


def revoke_consent(
    conn: sqlite3.Connection, *, teacher_id: str
) -> bool:
    """Revoke the active consent for this teacher. Returns True if a row was
    revoked, False if there was nothing active. Historical observations stay
    in place — retention policy governs purge, not consent.
    """
    cur = conn.execute(
        """UPDATE consent_records SET revoked_at = ?
           WHERE teacher_id = ? AND revoked_at IS NULL""",
        (_now_iso(), teacher_id),
    )
    conn.commit()
    return cur.rowcount > 0


def record_bite_sized_action(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    teacher_id: str,
    source_observation_id: str,
    core_teacher_skill: str,
    related_domain: str,
    bite_sized_action_text: str,
    goal_id: Optional[str] = None,
    commit: bool = True,
) -> str:
    """Snapshot a coaching recommendation into the tracking table so it can be
    assessed at the next observation. Called at the moment a scoring job completes.

    ``goal_id`` is the professional-goal this action is meant to advance. If not
    provided, we auto-match by ``related_domain`` against active goals for this
    teacher — coach can override in the UI.

    ``commit=False`` lets the caller compose atomic transactions across
    several writes — used by the persist path in app/jobs.py so a mid-persist
    crash doesn't leave the observation marked complete with a phantom
    tracking row but no report.
    """
    if goal_id is None:
        # Auto-link to an active goal on the same domain — but ONLY when the
        # match is unambiguous. Two open goals on the same domain is unusual
        # (usually you close one before proposing the next) but the tool
        # doesn't prevent it, and picking one silently by proposed_at buries
        # the disambiguation from the coach. Leave goal_id NULL when ambiguous
        # so the observation page's "unlinked action — pick a goal" chip
        # forces the coach to choose.
        candidates = conn.execute(
            """SELECT id FROM professional_goals
               WHERE teacher_id = ? AND status IN ('proposed', 'active')
                 AND related_rubric_domain = ?
               ORDER BY proposed_at DESC LIMIT 2""",
            (teacher_id, related_domain),
        ).fetchall()
        if len(candidates) == 1:
            goal_id = candidates[0]["id"]
        # len==0: no active goal on this domain — leave NULL, coach can attach
        # via set_action_goal_route later.
        # len>=2: ambiguous — leave NULL, coach must pick.

    tracking_id = _new_id()
    conn.execute(
        """INSERT INTO bite_sized_action_tracking
               (id, org_id, teacher_id, source_observation_id, core_teacher_skill,
                related_domain, bite_sized_action_text, goal_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            tracking_id, org_id, teacher_id, source_observation_id,
            core_teacher_skill, related_domain, bite_sized_action_text,
            goal_id,
            _now_iso(),
        ),
    )
    if commit:
        conn.commit()
    return tracking_id


def assess_bite_sized_action(
    conn: sqlite3.Connection,
    *,
    tracking_id: str,
    followup_observation_id: str,
    implementation: str,
    evidence_notes: Optional[str],
    assessed_by_user_id: str,
) -> None:
    """Coach (or AI) records whether the action was implemented."""
    if implementation not in ("not_observed", "partial", "full", "regressed"):
        raise ValueError(f"Invalid implementation: {implementation!r}")
    conn.execute(
        """UPDATE bite_sized_action_tracking SET
               followup_observation_id = ?,
               implementation = ?,
               evidence_notes = ?,
               assessed_by_user_id = ?,
               assessed_at = ?
           WHERE id = ?""",
        (followup_observation_id, implementation, evidence_notes,
         assessed_by_user_id, _now_iso(), tracking_id),
    )
    conn.commit()


def list_open_action_tracking_for_teacher(
    conn: sqlite3.Connection, teacher_id: str
) -> list:
    """Actions issued to this teacher that haven't been assessed yet.

    Filters out actions whose source observation was archived — those actions
    came from a mis-uploaded or invalidated observation and should not
    continue to appear as "awaiting assessment" in coach dashboards or, more
    importantly, in the AI's context on the next observation (where the
    prompt asks the model to score them and could produce contradictory
    ratings for phantom actions).
    """
    rows = conn.execute(
        """SELECT bsat.* FROM bite_sized_action_tracking bsat
           JOIN observations src ON src.id = bsat.source_observation_id
           WHERE bsat.teacher_id = ? AND bsat.implementation IS NULL
             AND src.deleted_at IS NULL
           ORDER BY bsat.created_at DESC""",
        (teacher_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_cycle(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    teacher_id: str,
    coach_user_id: str,
    title: Optional[str] = None,
    notes: Optional[str] = None,
    goal_focus_skills: Optional[list] = None,
    expected_close_date: Optional[str] = None,
) -> str:
    """Open a new coaching cycle. Title is optional but recommended.

    ``expected_close_date`` defaults to opened_at + 21 days (3-week cadence)
    if not supplied; overridable at creation and after.
    """
    cycle_id = _new_id()
    opened_at = _now_iso()
    if not expected_close_date:
        from datetime import datetime as _dt, timedelta as _td
        d = _dt.fromisoformat(opened_at) + _td(days=21)
        expected_close_date = d.date().isoformat()
    conn.execute(
        """INSERT INTO coaching_cycles
               (id, org_id, teacher_id, coach_user_id, opened_at, expected_close_date,
                goal_focus_skills, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            cycle_id, org_id, teacher_id, coach_user_id, opened_at, expected_close_date,
            json.dumps(goal_focus_skills) if goal_focus_skills else None,
            (title + "\n\n" + notes) if (title and notes)
                else (title or notes),
        ),
    )
    conn.commit()
    return cycle_id


import re as _re


def update_cycle_expected_close(
    conn: sqlite3.Connection, cycle_id: str, expected_close_date: str
) -> None:
    """Store a new expected-close date. Enforces YYYY-MM-DD format so
    downstream `_date.fromisoformat` calls (attention items, week-progress
    math) don't silently drop the value.
    """
    if not expected_close_date or not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected_close_date):
        raise ValueError(f"expected_close_date must be YYYY-MM-DD, got {expected_close_date!r}")
    conn.execute(
        "UPDATE coaching_cycles SET expected_close_date = ? WHERE id = ?",
        (expected_close_date, cycle_id),
    )
    conn.commit()


class CycleAlreadyClosedError(ValueError):
    """Raised on a close attempt against a cycle whose closed_at is already set.
    Caller surfaces as 409 — double-submit shouldn't overwrite the closed_at
    timestamp or silently append a second '[Closed …]' line to notes.
    """


def close_cycle(
    conn: sqlite3.Connection,
    cycle_id: str,
    *,
    closing_notes: Optional[str] = None,
    growth_story: Optional[str] = None,
) -> None:
    """Close a coaching cycle.

    - Appends any ``closing_notes`` to the existing notes field (short tags).
    - Saves ``growth_story`` on its own column — the coach-authored narrative
      of the arc, rendered co-equal with the rubric-delta KPI on cycle-detail.

    Refuses to close an already-closed cycle: overwriting closed_at distorts
    cycle-duration KPIs, and re-appending closing_notes would leave a second
    "[Closed …]" marker in the notes field. Edit growth_story via the
    /cycles/{id}/growth-story route instead.
    """
    row = conn.execute(
        "SELECT closed_at, notes FROM coaching_cycles WHERE id = ?", (cycle_id,)
    ).fetchone()
    if not row:
        raise ValueError(f"Cycle {cycle_id!r} not found")
    if row["closed_at"]:
        raise CycleAlreadyClosedError(
            f"Cycle already closed on {row['closed_at'][:10]}. "
            f"Edit growth story via /cycles/{cycle_id}/growth-story instead."
        )
    if closing_notes:
        prev = row["notes"] or ""
        combined = prev + f"\n\n---\n[Closed {_now_iso()[:10]}] " + closing_notes
        conn.execute(
            "UPDATE coaching_cycles SET closed_at = ?, notes = ?, growth_story = COALESCE(?, growth_story) WHERE id = ?",
            (_now_iso(), combined, growth_story, cycle_id),
        )
    else:
        conn.execute(
            "UPDATE coaching_cycles SET closed_at = ?, growth_story = COALESCE(?, growth_story) WHERE id = ?",
            (_now_iso(), growth_story, cycle_id),
        )
    conn.commit()


def list_cycles_for_teacher(
    conn: sqlite3.Connection, teacher_id: str, *, active_only: bool = False
) -> list:
    """List coaching cycles for a teacher, most recent first."""
    if active_only:
        rows = conn.execute(
            """SELECT * FROM coaching_cycles
               WHERE teacher_id = ? AND closed_at IS NULL
               ORDER BY opened_at DESC""",
            (teacher_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM coaching_cycles WHERE teacher_id = ? ORDER BY opened_at DESC",
            (teacher_id,),
        ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("goal_focus_skills"):
            try:
                d["goal_focus_skills"] = json.loads(d["goal_focus_skills"])
            except Exception:
                pass
        out.append(d)
    return out


def get_cycle(conn: sqlite3.Connection, cycle_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM coaching_cycles WHERE id = ?", (cycle_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("goal_focus_skills"):
        try:
            d["goal_focus_skills"] = json.loads(d["goal_focus_skills"])
        except Exception:
            pass
    return d


def list_goals_in_cycle(conn: sqlite3.Connection, cycle_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM professional_goals WHERE coaching_cycle_id = ? ORDER BY proposed_at ASC",
        (cycle_id,),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        if d.get("success_indicators"):
            try:
                d["success_indicators"] = json.loads(d["success_indicators"])
            except Exception:
                pass
        out.append(d)
    return out


def list_observations_in_cycle(conn: sqlite3.Connection, cycle_id: str) -> list:
    """List observations attached to a cycle. Archived observations are
    filtered out — the cycle rollup treats them as never having happened,
    matching every other aggregate.
    """
    rows = conn.execute(
        """SELECT id, video_filename, video_duration_s, status, scored_at, uploaded_at
           FROM observations
           WHERE coaching_cycle_id = ? AND deleted_at IS NULL
           ORDER BY uploaded_at ASC""",
        (cycle_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def attach_observation_to_cycle(
    conn: sqlite3.Connection, *, observation_id: str, cycle_id: Optional[str]
) -> None:
    """Attach (or detach when cycle_id is None) an observation to a cycle."""
    conn.execute(
        "UPDATE observations SET coaching_cycle_id = ? WHERE id = ?",
        (cycle_id, observation_id),
    )
    conn.commit()


def attach_goal_to_cycle(
    conn: sqlite3.Connection, *, goal_id: str, cycle_id: Optional[str]
) -> None:
    conn.execute(
        "UPDATE professional_goals SET coaching_cycle_id = ? WHERE id = ?",
        (cycle_id, goal_id),
    )
    conn.commit()


def list_actions_issued_at(
    conn: sqlite3.Connection, observation_id: str
) -> list:
    """Actions that were recommended in THIS observation (outbound to next cycle)."""
    rows = conn.execute(
        """SELECT * FROM bite_sized_action_tracking
           WHERE source_observation_id = ?
           ORDER BY created_at ASC""",
        (observation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_actions_available_for_assessment_at(
    conn: sqlite3.Connection, followup_observation_id: str
) -> list:
    """Actions from PRIOR observations of the same teacher that haven't been
    assessed yet. These are the ones eligible for assessment against THIS
    observation.
    """
    obs = conn.execute(
        "SELECT teacher_id, uploaded_at FROM observations WHERE id = ?",
        (followup_observation_id,),
    ).fetchone()
    if not obs:
        return []
    rows = conn.execute(
        """SELECT bsat.* FROM bite_sized_action_tracking bsat
           JOIN observations src ON src.id = bsat.source_observation_id
           WHERE bsat.teacher_id = ?
             AND bsat.implementation IS NULL
             AND src.uploaded_at < ?
             AND bsat.source_observation_id != ?
             AND src.deleted_at IS NULL
           ORDER BY bsat.created_at ASC""",
        (obs["teacher_id"], obs["uploaded_at"], followup_observation_id),
    ).fetchall()
    return [dict(r) for r in rows]


def list_actions_assessed_at(
    conn: sqlite3.Connection, followup_observation_id: str
) -> list:
    """Actions that were assessed against THIS observation, whether by the AI
    (assessed_by_user_id IS NULL) or by a coach.
    """
    rows = conn.execute(
        """SELECT * FROM bite_sized_action_tracking
           WHERE followup_observation_id = ?
           ORDER BY assessed_at ASC""",
        (followup_observation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_observation_debrief_focus(
    conn: sqlite3.Connection, *, observation_id: str,
    debrief_focus: Optional[str], debrief_focus_gbf_id: Optional[str] = None,
) -> None:
    """Coach-authored: name the specific move to be practiced in the debrief
    following this observation."""
    conn.execute(
        "UPDATE observations SET debrief_focus = ?, debrief_focus_gbf_id = ? WHERE id = ?",
        (debrief_focus, debrief_focus_gbf_id, observation_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Lesson plans
# ---------------------------------------------------------------------------


def create_lesson_plan(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    teacher_id: str,
    title: str,
    plan_start_date: Optional[str],
    plan_end_date: Optional[str],
    created_by_user_id: str,
    file_ref: str,
    original_filename: str,
    extracted_text: Optional[str] = None,
) -> str:
    """Create a lesson plan and its version 1 in one call."""
    plan_id = _new_id()
    ts = _now_iso()
    conn.execute(
        """INSERT INTO lesson_plans
               (id, org_id, teacher_id, title, plan_start_date, plan_end_date,
                current_version, status, created_by_user_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, 'submitted', ?, ?, ?)""",
        (plan_id, org_id, teacher_id, title, plan_start_date, plan_end_date,
         created_by_user_id, ts, ts),
    )
    conn.execute(
        """INSERT INTO lesson_plan_versions
               (id, lesson_plan_id, version_number, file_ref, original_filename,
                extracted_text, uploaded_by_user_id, created_at)
           VALUES (?, ?, 1, ?, ?, ?, ?, ?)""",
        (_new_id(), plan_id, file_ref, original_filename, extracted_text,
         created_by_user_id, ts),
    )
    conn.commit()
    return plan_id


def add_lesson_plan_version(
    conn: sqlite3.Connection,
    *,
    lesson_plan_id: str,
    file_ref: str,
    original_filename: str,
    uploaded_by_user_id: str,
    extracted_text: Optional[str] = None,
) -> int:
    """Add a new version to an existing lesson plan; bumps current_version.
    Returns the new version number.
    """
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS m FROM lesson_plan_versions WHERE lesson_plan_id = ?",
        (lesson_plan_id,),
    ).fetchone()
    new_ver = row["m"] + 1
    ts = _now_iso()
    conn.execute(
        """INSERT INTO lesson_plan_versions
               (id, lesson_plan_id, version_number, file_ref, original_filename,
                extracted_text, uploaded_by_user_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (_new_id(), lesson_plan_id, new_ver, file_ref, original_filename,
         extracted_text, uploaded_by_user_id, ts),
    )
    conn.execute(
        "UPDATE lesson_plans SET current_version = ?, updated_at = ?, status = 'submitted' WHERE id = ?",
        (new_ver, ts, lesson_plan_id),
    )
    conn.commit()
    return new_ver


def update_lesson_plan_status(
    conn: sqlite3.Connection, lesson_plan_id: str, status: str
) -> None:
    if status not in (
        "submitted", "coach_reviewed", "revision_requested", "approved", "archived"
    ):
        raise ValueError(f"Invalid status: {status!r}")
    conn.execute(
        "UPDATE lesson_plans SET status = ?, updated_at = ? WHERE id = ?",
        (status, _now_iso(), lesson_plan_id),
    )
    conn.commit()


def add_lesson_plan_comment(
    conn: sqlite3.Connection,
    *,
    lesson_plan_id: str,
    plan_version_number: int,
    author_user_id: str,
    author_role: str,
    body: str,
) -> str:
    cid = _new_id()
    conn.execute(
        """INSERT INTO lesson_plan_comments
               (id, lesson_plan_id, plan_version_number, author_user_id, author_role,
                body, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (cid, lesson_plan_id, plan_version_number, author_user_id, author_role,
         body, _now_iso()),
    )
    conn.commit()
    return cid


def list_lesson_plans_for_teacher(conn: sqlite3.Connection, teacher_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM lesson_plans WHERE teacher_id = ? ORDER BY created_at DESC",
        (teacher_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_lesson_plans_for_org(conn: sqlite3.Connection, org_id: str) -> list:
    rows = conn.execute(
        """SELECT lp.*, t.name AS teacher_name
           FROM lesson_plans lp
           JOIN teachers t ON t.id = lp.teacher_id
           WHERE lp.org_id = ?
           ORDER BY lp.created_at DESC""",
        (org_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_lesson_plan(conn: sqlite3.Connection, plan_id: str) -> Optional[dict]:
    row = conn.execute(
        """SELECT lp.*, t.name AS teacher_name FROM lesson_plans lp
           JOIN teachers t ON t.id = lp.teacher_id WHERE lp.id = ?""",
        (plan_id,),
    ).fetchone()
    return dict(row) if row else None


def list_lesson_plan_versions(conn: sqlite3.Connection, plan_id: str) -> list:
    rows = conn.execute(
        "SELECT * FROM lesson_plan_versions WHERE lesson_plan_id = ? ORDER BY version_number ASC",
        (plan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def list_lesson_plan_comments(conn: sqlite3.Connection, plan_id: str) -> list:
    rows = conn.execute(
        """SELECT c.*, u.name AS author_name
           FROM lesson_plan_comments c
           JOIN users u ON u.id = c.author_user_id
           WHERE c.lesson_plan_id = ? ORDER BY c.created_at ASC""",
        (plan_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# District documents
# ---------------------------------------------------------------------------


def create_district_document(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    title: str,
    doc_type: Optional[str],
    file_ref: str,
    original_filename: str,
    extracted_text: Optional[str],
    uploaded_by_user_id: str,
    academic_year: Optional[str] = None,
) -> str:
    did = _new_id()
    conn.execute(
        """INSERT INTO district_documents
               (id, org_id, academic_year, title, doc_type, file_ref,
                original_filename, extracted_text, uploaded_by_user_id, uploaded_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (did, org_id, academic_year, title, doc_type, file_ref,
         original_filename, extracted_text, uploaded_by_user_id, _now_iso()),
    )
    conn.commit()
    return did


def list_district_documents(
    conn: sqlite3.Connection, *, org_id: str, academic_year: Optional[str] = None
) -> list:
    if academic_year:
        rows = conn.execute(
            """SELECT * FROM district_documents
               WHERE org_id = ? AND archived_at IS NULL
                 AND (academic_year IS NULL OR academic_year = ?)
               ORDER BY uploaded_at DESC""",
            (org_id, academic_year),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM district_documents WHERE org_id = ? AND archived_at IS NULL ORDER BY uploaded_at DESC",
            (org_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def archive_district_document(conn: sqlite3.Connection, doc_id: str) -> None:
    conn.execute(
        "UPDATE district_documents SET archived_at = ? WHERE id = ?",
        (_now_iso(), doc_id),
    )
    conn.commit()


def rubric_score_movement_for_teacher(
    conn: sqlite3.Connection, teacher_id: str, *, domain: Optional[str] = None
) -> list:
    """Return per-observation rating history for a teacher, optionally filtered
    to one domain. Used for the impact dashboard (rubric-score-movement half).

    Returns list of dicts with observation_id, scored_at, and either a single
    rating (if domain given) or a dict of all-4-domain ratings.
    """
    rows = conn.execute(
        """SELECT o.id AS obs_id, o.scored_at,
                  rv.domain_assessments
           FROM observations o
           JOIN report_versions rv ON rv.observation_id = o.id
           WHERE o.teacher_id = ? AND o.status = 'complete'
             AND o.deleted_at IS NULL
             AND rv.published_at IS NOT NULL
           ORDER BY o.scored_at ASC""",
        (teacher_id,),
    ).fetchall()
    out = []
    for r in rows:
        try:
            das = json.loads(r["domain_assessments"]) if r["domain_assessments"] else []
        except Exception:
            das = []
        by_domain = {da["domain"]: da["overall_rating"] for da in das}
        entry = {"observation_id": r["obs_id"], "scored_at": r["scored_at"]}
        if domain:
            entry["rating"] = by_domain.get(domain)
        else:
            entry["ratings"] = by_domain
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Two-sided negotiation: teacher account on actions + HLM responses + releasing
# ---------------------------------------------------------------------------


def set_teacher_account_on_action(
    conn: sqlite3.Connection, *, action_id: str, teacher_account: str
) -> None:
    """Record the teacher's own narrative account of trying a bite-sized action.

    Distinct from ``evidence_notes`` (coach's outside-in read). The gap between
    the two is the coaching conversation.
    """
    conn.execute(
        """UPDATE bite_sized_action_tracking
           SET teacher_account = ?, teacher_account_at = ?
           WHERE id = ?""",
        (teacher_account, _now_iso(), action_id),
    )
    conn.commit()


def get_action(conn: sqlite3.Connection, action_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM bite_sized_action_tracking WHERE id = ?", (action_id,)
    ).fetchone()
    return dict(row) if row else None


def upsert_hlm_response(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    response_type: str,
    teacher_note: Optional[str] = None,
    teacher_user_id: Optional[str] = None,
) -> str:
    """Record (or replace) the teacher's response to the AI's highest-leverage move.

    One response per observation; a new save replaces the old (and resets the
    coach's acknowledgment).
    """
    if response_type not in ("resonates", "adjust", "talk"):
        raise ValueError(f"Unknown response_type: {response_type!r}")
    existing = conn.execute(
        "SELECT id FROM hlm_responses WHERE observation_id = ?", (observation_id,)
    ).fetchone()
    if existing:
        conn.execute(
            """UPDATE hlm_responses
               SET response_type = ?, teacher_note = ?, teacher_user_id = ?,
                   created_at = ?, acknowledged_at = NULL, acknowledged_by_user_id = NULL
               WHERE id = ?""",
            (response_type, teacher_note, teacher_user_id, _now_iso(), existing["id"]),
        )
        response_id = existing["id"]
    else:
        response_id = _new_id()
        conn.execute(
            """INSERT INTO hlm_responses
                   (id, observation_id, response_type, teacher_note, teacher_user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (response_id, observation_id, response_type, teacher_note, teacher_user_id, _now_iso()),
        )
    conn.commit()
    return response_id


def get_hlm_response(conn: sqlite3.Connection, observation_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM hlm_responses WHERE observation_id = ?", (observation_id,)
    ).fetchone()
    return dict(row) if row else None


def acknowledge_hlm_response(
    conn: sqlite3.Connection, *, observation_id: str, coach_user_id: str
) -> None:
    conn.execute(
        """UPDATE hlm_responses
           SET acknowledged_at = ?, acknowledged_by_user_id = ?
           WHERE observation_id = ?""",
        (_now_iso(), coach_user_id, observation_id),
    )
    conn.commit()


def list_unacknowledged_hlm_responses(conn: sqlite3.Connection) -> list:
    """Coach attention: HLM responses that need acknowledgment or a modified plan."""
    rows = conn.execute(
        """SELECT hr.id, hr.observation_id, hr.response_type, hr.teacher_note, hr.created_at,
                  t.id AS teacher_id, t.name AS teacher_name
           FROM hlm_responses hr
           JOIN observations o ON o.id = hr.observation_id
           JOIN teachers t ON t.id = o.teacher_id
           WHERE hr.acknowledged_at IS NULL
             AND hr.response_type IN ('adjust', 'talk')
             AND o.deleted_at IS NULL
             AND t.archived_at IS NULL
           ORDER BY hr.created_at ASC"""
    ).fetchall()
    return [dict(r) for r in rows]


def set_goal_releasing(
    conn: sqlite3.Connection, *, goal_id: str, releasing: Optional[str]
) -> None:
    """Bridges' 'ending' — name the practice the teacher is setting aside."""
    conn.execute(
        "UPDATE professional_goals SET releasing = ? WHERE id = ?",
        (releasing, goal_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Coach's published move (coach-mediated AI)
# ---------------------------------------------------------------------------


def publish_coach_move(
    conn: sqlite3.Connection,
    *,
    observation_id: str,
    move_text: str,
    gbf_step_id: Optional[str] = None,
    related_core_teacher_skill: Optional[str] = None,
    derived_from_ai: bool = False,
    published_by_user_id: Optional[str] = None,
) -> str:
    """Publish a new coach's move for this observation.

    If a current (non-superseded) move already exists for this observation,
    it is marked superseded and a new row inserted. The teacher's view will
    now show the new version.
    """
    now = _now_iso()
    existing = conn.execute(
        "SELECT id FROM published_coach_moves WHERE observation_id = ? AND superseded_at IS NULL",
        (observation_id,),
    ).fetchone()
    new_id = _new_id()
    if existing:
        conn.execute(
            "UPDATE published_coach_moves SET superseded_at = ?, superseded_by_move_id = ? WHERE id = ?",
            (now, new_id, existing["id"]),
        )
        # Any prior teacher response is now against a superseded move — the
        # coach's previous acknowledgment no longer stands for the current move.
        # Coach must re-acknowledge if the teacher reacts to this new version.
        conn.execute(
            """UPDATE hlm_responses
               SET acknowledged_at = NULL, acknowledged_by_user_id = NULL
               WHERE observation_id = ?""",
            (observation_id,),
        )
    conn.execute(
        """INSERT INTO published_coach_moves
               (id, observation_id, move_text, gbf_step_id, related_core_teacher_skill,
                derived_from_ai, published_by_user_id, published_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (new_id, observation_id, move_text.strip(), gbf_step_id, related_core_teacher_skill,
         1 if derived_from_ai else 0, published_by_user_id, now),
    )
    conn.commit()
    return new_id


def edit_coach_move(
    conn: sqlite3.Connection,
    *,
    move_id: str,
    move_text: str,
    gbf_step_id: Optional[str] = None,
    related_core_teacher_skill: Optional[str] = None,
) -> None:
    """Edit an existing published move in place — does NOT create a new version.

    Use this for typos or small tweaks. For a substantive revision (e.g. after
    the teacher pushed back and you met in person), call ``publish_coach_move``
    which supersedes.

    Ack invariant: if the edit changes ``move_text`` and the teacher has
    already responded (any ack), null the ack — the teacher's earlier response
    now points to different text than what they saw. The response row itself
    is preserved (so the coach can still see what the teacher said); only the
    coach's acknowledgment is cleared, so the stale-response badge re-surfaces
    on the observation page. Same idea as the supersede-clears-ack rule in
    ``publish_coach_move``, but scoped to edits that actually alter the text.
    """
    prior = conn.execute(
        "SELECT move_text, observation_id FROM published_coach_moves WHERE id = ?",
        (move_id,),
    ).fetchone()
    stripped_new = move_text.strip()
    text_changed = prior is not None and (prior["move_text"] or "") != stripped_new
    conn.execute(
        """UPDATE published_coach_moves
           SET move_text = ?, gbf_step_id = ?, related_core_teacher_skill = ?, edited_at = ?
           WHERE id = ?""",
        (stripped_new, gbf_step_id, related_core_teacher_skill, _now_iso(), move_id),
    )
    if text_changed:
        conn.execute(
            """UPDATE hlm_responses
               SET acknowledged_at = NULL, acknowledged_by_user_id = NULL
               WHERE observation_id = ? AND published_coach_move_id = ?""",
            (prior["observation_id"], move_id),
        )
    conn.commit()


def get_current_coach_move(
    conn: sqlite3.Connection, observation_id: str
) -> Optional[dict]:
    row = conn.execute(
        """SELECT * FROM published_coach_moves
           WHERE observation_id = ? AND superseded_at IS NULL""",
        (observation_id,),
    ).fetchone()
    return dict(row) if row else None


def list_coach_move_history(
    conn: sqlite3.Connection, observation_id: str
) -> list:
    """All published moves for an observation, current first, then superseded ones."""
    rows = conn.execute(
        """SELECT * FROM published_coach_moves
           WHERE observation_id = ?
           ORDER BY (superseded_at IS NULL) DESC, published_at DESC""",
        (observation_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def most_recent_published_move_for_teacher(
    conn: sqlite3.Connection, teacher_id: str
) -> Optional[dict]:
    """The coach's most recent published (non-superseded) move for this teacher,
    across all their observations. Used on the teacher-view when the latest
    observation hasn't yet had a move published — falls back to the previous.
    """
    row = conn.execute(
        """SELECT pcm.*, o.uploaded_at AS obs_uploaded_at, o.scored_at AS obs_scored_at
           FROM published_coach_moves pcm
           JOIN observations o ON o.id = pcm.observation_id
           WHERE o.teacher_id = ? AND pcm.superseded_at IS NULL
             AND o.deleted_at IS NULL
           ORDER BY pcm.published_at DESC LIMIT 1""",
        (teacher_id,),
    ).fetchone()
    return dict(row) if row else None


def bootstrap_coach_moves_from_ai(conn: sqlite3.Connection) -> int:
    """Migration helper: for each observation that has an AI HLM but no
    published coach move, create a coach move (derived_from_ai=True) with
    the AI's text. This gives existing dev data something to render while
    coaches catch up on the workflow.

    Returns the number of moves created.
    """
    created = 0
    rows = conn.execute(
        """SELECT o.id AS observation_id, rv.coaching_recommendations, o.debrief_focus_gbf_id
           FROM observations o
           JOIN report_versions rv ON rv.observation_id = o.id
           WHERE rv.published_at IS NOT NULL
             AND o.id NOT IN (SELECT observation_id FROM published_coach_moves)
           ORDER BY rv.created_at ASC"""
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["coaching_recommendations"] or "{}")
            if not isinstance(raw, dict):
                continue
            hlm = raw.get("highest_leverage_move") or {}
            move_text = (hlm.get("move") or "").strip()
            if not move_text:
                continue
            gbf = hlm.get("gbf_step_id") or row["debrief_focus_gbf_id"]
            skill = hlm.get("related_core_teacher_skill")
            publish_coach_move(
                conn,
                observation_id=row["observation_id"],
                move_text=move_text,
                gbf_step_id=gbf,
                related_core_teacher_skill=skill,
                derived_from_ai=True,
            )
            created += 1
        except Exception:
            continue
    return created


# ---------------------------------------------------------------------------
# Practice log — deliberate-practice micro-attempts between observations
# ---------------------------------------------------------------------------


def _ensure_practice_log_table(conn: sqlite3.Connection) -> None:
    """Create the practice_log_entries table if it doesn't exist.

    Kept out of the main additive-migration block so it's a standalone table.
    Called opportunistically before any practice-log helper runs.
    """
    conn.execute(
        """CREATE TABLE IF NOT EXISTS practice_log_entries (
            id                  TEXT PRIMARY KEY,
            org_id              TEXT NOT NULL REFERENCES organizations(id),
            teacher_id          TEXT NOT NULL REFERENCES teachers(id),
            action_id           TEXT REFERENCES bite_sized_action_tracking(id),
            cycle_id            TEXT REFERENCES coaching_cycles(id),
            entry_text          TEXT NOT NULL,
            created_by_role     TEXT NOT NULL
                CHECK (created_by_role IN ('teacher', 'coach')),
            created_at          TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plog_teacher ON practice_log_entries(teacher_id, created_at DESC)"
    )
    # Additive migration for the cycle_id column (older DB may not have it).
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(practice_log_entries)").fetchall()}
    if "cycle_id" not in cols:
        conn.execute("ALTER TABLE practice_log_entries ADD COLUMN cycle_id TEXT")
    conn.commit()


def add_practice_log_entry(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    teacher_id: str,
    entry_text: str,
    created_by_role: str,
    action_id: Optional[str] = None,
    cycle_id: Optional[str] = None,
) -> str:
    """Record a practice-log entry — micro-attempts between observations.

    Deliberate practice needs feedback loops smaller than the 3-week cycle.
    This is where a teacher (or coach on their behalf) records: 'tried the
    peer-response prompt today, worked in period 3, flopped in period 4.'

    ``cycle_id`` defaults to the teacher's currently-active cycle so entries
    are grouped by arc for display.
    """
    if created_by_role not in ("teacher", "coach"):
        raise ValueError(f"created_by_role must be teacher|coach, got {created_by_role!r}")
    _ensure_practice_log_table(conn)
    if cycle_id is None:
        row = conn.execute(
            "SELECT id FROM coaching_cycles WHERE teacher_id = ? AND closed_at IS NULL LIMIT 1",
            (teacher_id,),
        ).fetchone()
        if row:
            cycle_id = row["id"]
    eid = _new_id()
    conn.execute(
        """INSERT INTO practice_log_entries
               (id, org_id, teacher_id, action_id, cycle_id, entry_text, created_by_role, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (eid, org_id, teacher_id, action_id, cycle_id, entry_text.strip(), created_by_role, _now_iso()),
    )
    conn.commit()
    return eid


def list_practice_log_for_teacher(
    conn: sqlite3.Connection, teacher_id: str, *, limit: int = 20
) -> list:
    """Return recent practice-log entries.

    Each row is enriched with:
    - ``cycle_title``: first line of the parent cycle's notes (or None)
    - ``action_skill``: core_teacher_skill of the linked action (or None)
    """
    _ensure_practice_log_table(conn)
    rows = conn.execute(
        """SELECT ple.*,
                  c.notes AS cycle_notes, c.closed_at AS cycle_closed_at,
                  ba.core_teacher_skill AS action_skill
           FROM practice_log_entries ple
           LEFT JOIN coaching_cycles c ON c.id = ple.cycle_id
           LEFT JOIN bite_sized_action_tracking ba ON ba.id = ple.action_id
           WHERE ple.teacher_id = ?
           ORDER BY ple.created_at DESC LIMIT ?""",
        (teacher_id, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        notes = (d.pop("cycle_notes", None) or "")
        d["cycle_title"] = notes.splitlines()[0] if notes else None
        out.append(d)
    return out
