-- Classroom Observer schema (SQLite starter mirroring docs/data_model.md).
--
-- This is a fidelity check for the design doc, not the production DDL. When we
-- migrate to Postgres/Supabase we'll port these to proper migrations with RLS
-- policies. SQLite doesn't enforce column types or FK by default, so this is
-- intentionally strict — enable FK enforcement per connection with:
--     PRAGMA foreign_keys = ON;
--
-- UUIDs stored as TEXT (SQLite has no native uuid type).
-- Timestamps stored as TEXT in ISO 8601 (SQLite has no native timestamptz).
-- JSON columns stored as TEXT (SQLite has JSON1 extension for queries).

PRAGMA foreign_keys = ON;

-------------------------------------------------------------------------------
-- Tenancy
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS organizations (
    id                          TEXT PRIMARY KEY,
    name                        TEXT NOT NULL,
    slug                        TEXT NOT NULL UNIQUE,
    plan_tier                   TEXT NOT NULL DEFAULT 'free'
        CHECK (plan_tier IN ('free', 'pilot', 'district')),
    is_educational_institution  INTEGER NOT NULL DEFAULT 0,
    signed_dpa_at               TEXT,
    dpa_document_ref            TEXT,
    default_rubric_id           TEXT,
    retention_policy_id         TEXT,
    created_at                  TEXT NOT NULL,
    archived_at                 TEXT
);

CREATE TABLE IF NOT EXISTS retention_policies (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),
    video_retention_days        INTEGER NOT NULL,
    transcript_retention_days   INTEGER NOT NULL,
    report_retention_days       INTEGER NOT NULL,
    audit_log_retention_days    INTEGER NOT NULL,
    updated_at                  TEXT NOT NULL
);

-------------------------------------------------------------------------------
-- Identity
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS users (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    email               TEXT NOT NULL,
    name                TEXT NOT NULL,
    role                TEXT NOT NULL
        -- 'teacher_self_serve' was the historical spelling for a teacher user
        -- account (as opposed to a teacher record in the `teachers` table).
        -- The app-layer role literal is 'teacher' throughout — every
        -- ``viewer["role"] == "teacher"`` gate in app/main.py assumes that.
        -- Both are permitted here so old rows keep working and new writes
        -- can standardize on 'teacher'. Fresh DBs write 'teacher'; existing
        -- DBs' CHECK gets widened by the migration in _apply_additive_migrations.
        CHECK (role IN ('admin', 'coach', 'teacher', 'teacher_self_serve', 'viewer')),
    auth_provider       TEXT NOT NULL DEFAULT 'email'
        CHECK (auth_provider IN ('email', 'google', 'microsoft', 'district_sso')),
    auth_provider_id    TEXT,
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    last_login_at       TEXT,
    UNIQUE (org_id, email)
);

CREATE INDEX IF NOT EXISTS idx_users_org ON users(org_id);

-------------------------------------------------------------------------------
-- Domain
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS teachers (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),
    name                        TEXT NOT NULL,
    email                       TEXT,
    employee_id                 TEXT,
    user_id                     TEXT REFERENCES users(id),
    assigned_coach_user_id      TEXT REFERENCES users(id),
    grade_levels                TEXT,  -- JSON array
    subjects                    TEXT,  -- JSON array
    created_at                  TEXT NOT NULL,
    archived_at                 TEXT
);

CREATE INDEX IF NOT EXISTS idx_teachers_org ON teachers(org_id);
CREATE INDEX IF NOT EXISTS idx_teachers_coach ON teachers(assigned_coach_user_id);

CREATE TABLE IF NOT EXISTS rubrics (
    id              TEXT PRIMARY KEY,
    org_id          TEXT REFERENCES organizations(id),  -- null = built-in
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    version         TEXT NOT NULL,
    pdf_ref         TEXT,
    config_json     TEXT NOT NULL,  -- Rubric dataclass serialized
    created_at      TEXT NOT NULL,
    archived_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_rubrics_org ON rubrics(org_id);

-------------------------------------------------------------------------------
-- Observations & reports
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS consent_records (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL REFERENCES organizations(id),
    teacher_id              TEXT NOT NULL REFERENCES teachers(id),
    scope                   TEXT NOT NULL
        CHECK (scope IN ('single_observation', 'academic_year', 'perpetual_until_revoked')),
    form_version            TEXT NOT NULL,
    method                  TEXT NOT NULL
        CHECK (method IN ('electronic_signature', 'paper_scan', 'verbal_witnessed')),
    consented_at            TEXT NOT NULL,
    revoked_at              TEXT,
    consent_artifact_ref    TEXT
);

CREATE INDEX IF NOT EXISTS idx_consent_teacher ON consent_records(teacher_id);

CREATE TABLE IF NOT EXISTS coaching_cycles (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    teacher_id          TEXT NOT NULL REFERENCES teachers(id),
    coach_user_id       TEXT NOT NULL REFERENCES users(id),
    opened_at           TEXT NOT NULL,
    -- Coach's declared expected close date. Default new cycles to opened_at + 21 days
    -- (3-week cadence) — configurable at open time and after.
    expected_close_date TEXT,
    closed_at           TEXT,
    goal_focus_skills   TEXT,  -- JSON array
    notes               TEXT
);

CREATE INDEX IF NOT EXISTS idx_cycles_teacher ON coaching_cycles(teacher_id);

CREATE TABLE IF NOT EXISTS observations (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL REFERENCES organizations(id),
    teacher_id              TEXT NOT NULL REFERENCES teachers(id),
    observer_user_id        TEXT NOT NULL REFERENCES users(id),
    rubric_id               TEXT NOT NULL REFERENCES rubrics(id),
    coaching_cycle_id       TEXT REFERENCES coaching_cycles(id),
    consent_record_id       TEXT REFERENCES consent_records(id),
    video_ref               TEXT,
    video_filename          TEXT NOT NULL,
    video_duration_s        REAL NOT NULL,
    video_bytes             INTEGER,
    transcript_ref          TEXT,
    transcript_model        TEXT,
    frames_prefix           TEXT,
    frame_count             INTEGER,
    frame_interval_s        REAL,
    status                  TEXT NOT NULL DEFAULT 'pending'
        -- NOTE: 'deleted' is legacy — no code writes it. Soft-delete uses the
        -- `deleted_at` timestamp column below. The enum value is preserved
        -- here (removing it would require a table rebuild) but it is orphaned.
        -- Any aggregate that cares about soft-delete filters on
        -- `deleted_at IS NULL`, not on `status <> 'deleted'`.
        CHECK (status IN ('pending', 'transcribing', 'scoring', 'complete', 'failed', 'deleted')),
    failure_reason          TEXT,
    observed_at             TEXT,
    uploaded_at             TEXT NOT NULL,
    scored_at               TEXT,
    deleted_at              TEXT,
    -- Coach-authored: the specific coaching move to be practiced during the
    -- debrief that follows this observation. Named by the coach (not the AI).
    -- Distinct from the AI's highest_leverage_move.
    debrief_focus           TEXT,
    -- Optional GBF action step this debrief focus maps to
    -- (e.g., "phase2_mgmt_teacher_radar"). See pipeline/gbf.py for the ids.
    debrief_focus_gbf_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_org ON observations(org_id);
CREATE INDEX IF NOT EXISTS idx_obs_teacher ON observations(teacher_id);
CREATE INDEX IF NOT EXISTS idx_obs_cycle ON observations(coaching_cycle_id);
CREATE INDEX IF NOT EXISTS idx_obs_status ON observations(status);

CREATE TABLE IF NOT EXISTS report_versions (
    id                          TEXT PRIMARY KEY,
    observation_id              TEXT NOT NULL REFERENCES observations(id),
    version_number              INTEGER NOT NULL,
    authored_by                 TEXT NOT NULL
        CHECK (authored_by IN ('ai', 'human')),
    authored_by_user_id         TEXT REFERENCES users(id),
    opening_paragraph           TEXT NOT NULL,
    overall_summary             TEXT NOT NULL,
    domain_assessments          TEXT NOT NULL,  -- JSON
    coaching_recommendations    TEXT NOT NULL,  -- JSON
    rendered_markdown           TEXT,
    published_at                TEXT,
    published_by_user_id        TEXT REFERENCES users(id),
    notes                       TEXT,
    created_at                  TEXT NOT NULL,
    UNIQUE (observation_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_rv_obs ON report_versions(observation_id);

-- At most one published version per observation. SQLite partial index expresses this.
CREATE UNIQUE INDEX IF NOT EXISTS uq_rv_one_published
    ON report_versions(observation_id)
    WHERE published_at IS NOT NULL;

-------------------------------------------------------------------------------
-- Compliance & usage
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES organizations(id),
    actor_user_id   TEXT REFERENCES users(id),
    actor_ip        TEXT,
    action          TEXT NOT NULL,
    target_type     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    metadata        TEXT,  -- JSON
    occurred_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_org_time
    ON audit_log(org_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target
    ON audit_log(target_type, target_id, occurred_at DESC);

CREATE TABLE IF NOT EXISTS usage_events (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    observation_id      TEXT REFERENCES observations(id),
    kind                TEXT NOT NULL,
    quantity            REAL NOT NULL,
    unit_cost_cents     REAL,
    occurred_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_org ON usage_events(org_id, occurred_at DESC);

-------------------------------------------------------------------------------
-- Relational context (Phase A extension)
--
-- These tables underpin the "relational context" product wedge: the AI's job
-- shifts from scoring a lesson in isolation to scoring a lesson in the context
-- of a specific teacher's stage, prior goals, prior coaching moves, and the
-- district's arc-of-year. Each table below serves one of those dimensions.
-------------------------------------------------------------------------------

-- Teacher profile — two-sided authorship (teacher fields + coach fields).
-- Quantitative wherever possible so the AI has structured signal instead of
-- prose to interpret. Career narrative and career goals are free-text escape
-- hatches; the AI is told to treat them as context, not authoritative facts.
CREATE TABLE IF NOT EXISTS teacher_profiles (
    teacher_id                  TEXT PRIMARY KEY REFERENCES teachers(id),

    -- Teacher-provided quantitative facts
    years_teaching_total        INTEGER,
    years_teaching_subject      INTEGER,
    years_at_current_school     INTEGER,
    highest_credential          TEXT,   -- 'bachelors' / 'masters' / 'doctorate' / 'national_board_certified' / 'other'
    subjects_taught             TEXT,   -- JSON array
    grade_levels_taught         TEXT,   -- JSON array

    -- Teacher-provided self-ratings (JSON dict: dimension -> integer 1-5)
    -- Recommended dimensions: classroom_management, content_expertise,
    -- student_engagement, formative_assessment, family_communication.
    -- Schema is intentionally open so districts can add their own dimensions.
    self_ratings                TEXT,   -- JSON: {"classroom_management": 4, ...}

    -- Teacher-provided preferences and context
    coaching_style_preference   TEXT,   -- free-form; e.g. 'direct', 'warm', 'socratic'
    career_narrative_notes      TEXT,   -- teacher's own framing of their trajectory
    career_goals_notes          TEXT,   -- teacher's own framing of where they want to grow

    teacher_updated_at          TEXT,

    -- Coach-provided public fields (visible to teacher — the "shared" side)
    coach_notes_on_teacher      TEXT,
    observed_style_notes        TEXT,

    -- Coach's ratings mirroring the teacher's self_ratings dimensions.
    -- {dimension: integer 1-5}
    coach_ratings               TEXT,   -- JSON
    -- Per-dimension context for the coach's rating (why this number).
    -- {dimension: "explanation..."}
    coach_ratings_context       TEXT,   -- JSON

    -- Coach-authored running narrative on the teacher's skill trajectory.
    -- Distinct from `coach_notes_on_teacher` — this is specifically about
    -- what they're working to master and where they've been in development.
    skill_development_narrative TEXT,

    coach_updated_at            TEXT
);

-- Professional goals — dual-authored, cycle-scoped, versioned by proposer.
CREATE TABLE IF NOT EXISTS professional_goals (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),
    teacher_id                  TEXT NOT NULL REFERENCES teachers(id),
    coaching_cycle_id           TEXT REFERENCES coaching_cycles(id),

    title                       TEXT NOT NULL,
    description                 TEXT,
    success_indicators          TEXT,   -- JSON array of strings

    proposed_by                 TEXT NOT NULL
        CHECK (proposed_by IN ('teacher', 'coach', 'joint')),
    teacher_notes               TEXT,   -- teacher's take on the goal
    coach_notes                 TEXT,   -- coach's take (visible to teacher — this is the public side)

    related_rubric_domain       TEXT,
    related_core_teacher_skill  TEXT,

    status                      TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'active', 'closed_met',
                          'closed_partial', 'closed_unmet', 'abandoned')),

    proposed_at                 TEXT NOT NULL,
    agreed_at                   TEXT,
    closed_at                   TEXT,
    updated_at                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_goals_teacher ON professional_goals(teacher_id, status);
CREATE INDEX IF NOT EXISTS idx_goals_cycle   ON professional_goals(coaching_cycle_id);

-- District context — per-org, per-academic-year. Fed to the AI to inform
-- observation framing (time of year, district focus, initiatives in flight).
-- Platform default (a generic year arc + no district priorities) applies when
-- no row exists for the current org+year.
CREATE TABLE IF NOT EXISTS district_context (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),
    academic_year               TEXT NOT NULL,   -- e.g., '2026-2027'
    year_start_date             TEXT,
    year_end_date               TEXT,

    -- Structured arc of the year — list of {phase, start_month, end_month, description}
    year_arc_json               TEXT,

    -- List of strings (this year's headline focus areas)
    district_priorities_json    TEXT,

    -- List of objects (curriculum roll-outs, PD initiatives, etc.)
    district_initiatives_json   TEXT,

    created_at                  TEXT NOT NULL,
    updated_at                  TEXT,

    UNIQUE (org_id, academic_year)
);

-- Coach's private notes — coach-only visibility. NOT visible to admins,
-- NOT visible to teachers. This is the coach's scratchpad; access control is
-- enforced by the application layer against the visibility column.
CREATE TABLE IF NOT EXISTS coach_private_notes (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),

    -- A note may be scoped to a specific observation, but is always also
    -- attached to the teacher (so a coach can browse standalone notes for a
    -- teacher across sessions).
    observation_id              TEXT REFERENCES observations(id),
    teacher_id                  TEXT NOT NULL REFERENCES teachers(id),

    author_user_id              TEXT NOT NULL REFERENCES users(id),
    body                        TEXT NOT NULL,

    -- Enum kept explicit so future visibility levels can be added without a
    -- migration; currently only 'coach_only' is valid.
    visibility                  TEXT NOT NULL DEFAULT 'coach_only'
        CHECK (visibility IN ('coach_only')),

    created_at                  TEXT NOT NULL,
    updated_at                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_priv_notes_teacher     ON coach_private_notes(teacher_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_priv_notes_observation ON coach_private_notes(observation_id);

-- Bite-sized action tracking — the mechanism for measuring coaching impact
-- (implementation half). When a coaching recommendation is issued in
-- observation N, a row is created here with source_observation_id = N. When
-- observation N+1 lands, the coach (or the AI, in Phase B) assesses whether
-- the action was implemented and populates followup_observation_id + implementation.
--
-- We snapshot core_teacher_skill and bite_sized_action_text so the tracking
-- survives even if the underlying report_versions row is edited or superseded.
CREATE TABLE IF NOT EXISTS bite_sized_action_tracking (
    id                          TEXT PRIMARY KEY,
    org_id                      TEXT NOT NULL REFERENCES organizations(id),
    teacher_id                  TEXT NOT NULL REFERENCES teachers(id),

    source_observation_id       TEXT NOT NULL REFERENCES observations(id),
    followup_observation_id     TEXT REFERENCES observations(id),

    -- Snapshot from the report_versions coaching_recommendations JSON at time
    -- of issue. Stable even if the report is re-versioned.
    core_teacher_skill          TEXT NOT NULL,
    related_domain              TEXT NOT NULL,
    bite_sized_action_text      TEXT NOT NULL,

    implementation              TEXT
        CHECK (implementation IN ('not_observed', 'partial', 'full', 'regressed')),
    evidence_notes              TEXT,

    assessed_by_user_id         TEXT REFERENCES users(id),
    assessed_at                 TEXT,

    created_at                  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_action_track_teacher ON bite_sized_action_tracking(teacher_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_action_track_source  ON bite_sized_action_tracking(source_observation_id);

-------------------------------------------------------------------------------
-- Lesson plans (teacher uploads, coach reviews recursively)
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS lesson_plans (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    teacher_id          TEXT NOT NULL REFERENCES teachers(id),
    -- Title / topic label; often the lesson name or unit.
    title               TEXT NOT NULL,
    -- Dates the plan covers (either single date or a span).
    plan_start_date     TEXT,
    plan_end_date       TEXT,
    -- Number of the current (latest) uploaded version. Version rows in
    -- lesson_plan_versions carry the actual file references and text.
    current_version     INTEGER NOT NULL DEFAULT 1,
    -- Coach's overall status assessment on the plan (informational).
    status              TEXT NOT NULL DEFAULT 'submitted'
        CHECK (status IN ('submitted', 'coach_reviewed', 'revision_requested', 'approved', 'archived')),
    created_by_user_id  TEXT NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);

CREATE INDEX IF NOT EXISTS idx_lesson_plans_teacher ON lesson_plans(teacher_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_lesson_plans_org     ON lesson_plans(org_id, status);

CREATE TABLE IF NOT EXISTS lesson_plan_versions (
    id                  TEXT PRIMARY KEY,
    lesson_plan_id      TEXT NOT NULL REFERENCES lesson_plans(id),
    version_number      INTEGER NOT NULL,
    file_ref            TEXT NOT NULL,     -- blob storage path
    original_filename   TEXT NOT NULL,
    extracted_text      TEXT,              -- pdftotext/pandoc output for search + inline preview
    uploaded_by_user_id TEXT NOT NULL REFERENCES users(id),
    created_at          TEXT NOT NULL,
    UNIQUE (lesson_plan_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_lpv_plan ON lesson_plan_versions(lesson_plan_id, version_number DESC);

CREATE TABLE IF NOT EXISTS lesson_plan_comments (
    id                  TEXT PRIMARY KEY,
    lesson_plan_id      TEXT NOT NULL REFERENCES lesson_plans(id),
    -- Version this comment is against (so a comment survives newer versions).
    plan_version_number INTEGER NOT NULL,
    author_user_id      TEXT NOT NULL REFERENCES users(id),
    author_role         TEXT NOT NULL
        -- See users.role note above: app writes 'teacher', historical schema
        -- used 'teacher_self_serve'. Both permitted so the write at
        -- app/main.py post_lesson_plan_comment doesn't hit a CHECK 500.
        CHECK (author_role IN ('teacher', 'teacher_self_serve', 'coach', 'admin')),
    body                TEXT NOT NULL,
    created_at          TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lpc_plan ON lesson_plan_comments(lesson_plan_id, created_at ASC);

-------------------------------------------------------------------------------
-- District documents (uploaded arc-of-year, coaching context, etc.)
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS district_documents (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL REFERENCES organizations(id),
    academic_year       TEXT,
    -- Human-readable label (e.g. "Arc of the Year 26-27", "Coaching Framework").
    title               TEXT NOT NULL,
    -- Free-form category so admins can group these (e.g., 'arc_of_year',
    -- 'coaching_framework', 'curriculum_pacing').
    doc_type            TEXT,
    file_ref            TEXT NOT NULL,     -- blob storage path
    original_filename   TEXT NOT NULL,
    -- pdftotext/pandoc-extracted text, injected into the AI context.
    extracted_text      TEXT,
    uploaded_by_user_id TEXT NOT NULL REFERENCES users(id),
    uploaded_at         TEXT NOT NULL,
    archived_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_ddoc_org  ON district_documents(org_id, uploaded_at DESC);
CREATE INDEX IF NOT EXISTS idx_ddoc_year ON district_documents(org_id, academic_year);

-------------------------------------------------------------------------------
-- Teacher's response to the AI's highest-leverage move (two-sided negotiation)
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS hlm_responses (
    id                      TEXT PRIMARY KEY,
    observation_id          TEXT NOT NULL REFERENCES observations(id),
    -- 'resonates' = accept as-is; 'adjust' = accept with modification;
    -- 'talk' = discuss at next debrief. Teacher_note carries the modification
    -- or the topic to discuss.
    response_type           TEXT NOT NULL
        CHECK (response_type IN ('resonates', 'adjust', 'talk')),
    teacher_note            TEXT,
    teacher_user_id         TEXT REFERENCES users(id),
    created_at              TEXT NOT NULL,
    acknowledged_at         TEXT,
    acknowledged_by_user_id TEXT REFERENCES users(id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_hlm_response_obs ON hlm_responses(observation_id);

-------------------------------------------------------------------------------
-- Coach's published move (what the teacher actually sees).
--
-- Coach mediates AI: reads the AI's raw highest-leverage move (kept on
-- report_versions.coaching_recommendations JSON, coach-only), edits into their
-- own message, publishes to the teacher. Teacher never sees AI-attributed
-- content — only what the coach chose to publish under their own voice.
-------------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS published_coach_moves (
    id                          TEXT PRIMARY KEY,
    observation_id              TEXT NOT NULL REFERENCES observations(id),
    move_text                   TEXT NOT NULL,
    -- Optional GBF alignment coach chose to tag.
    gbf_step_id                 TEXT,
    related_core_teacher_skill  TEXT,
    -- Internal only: true when coach clicked "start from AI's suggestion"
    -- (may or may not have edited it before publishing). False when coach
    -- wrote fresh. Feeds principal oversight of AI-coach interaction; NEVER
    -- surfaced to teachers.
    derived_from_ai             INTEGER NOT NULL DEFAULT 0,
    published_by_user_id        TEXT REFERENCES users(id),
    published_at                TEXT NOT NULL,
    edited_at                   TEXT,
    -- When set, this row is a prior version. Coach republished a new v.
    superseded_at               TEXT,
    superseded_by_move_id       TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_pcm_current
    ON published_coach_moves(observation_id) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_pcm_obs ON published_coach_moves(observation_id, published_at DESC);
