"""FastAPI local web UI for the classroom observer.

Run:
    uvicorn app.main:app --reload --port 8080

Then open http://localhost:8080 in your browser.

Environment:
    ANTHROPIC_API_KEY   required for scoring calls
    OBSERVER_DB         path to SQLite DB (default: reports/observations.sqlite)
    OBSERVER_UPLOADS    path to uploads dir (default: app/uploads)
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import markdown as md_lib
import bleach as _bleach

# Whitelist for the AI-produced report HTML. python-markdown passes raw HTML
# tags through untouched, so a lesson-plan PDF whose extracted text contains
# `<script>` (or a teacher whose CSV-imported name contains `<img src=x
# onerror=...>`) would otherwise land script content inside `report_html`,
# which the template pipes to the browser via `|safe`. Every tag markdown
# legitimately produces is on this list; every tag it doesn't is stripped.
# Attribute whitelist is deliberately narrow — href/title on links only,
# nothing that carries JS or CSS payload capability.
_REPORT_ALLOWED_TAGS = frozenset({
    "p", "br", "hr", "strong", "em", "u", "s", "code", "pre", "blockquote",
    "ul", "ol", "li",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "a",
    "table", "thead", "tbody", "tr", "th", "td",
    "span", "div",  # markdown-extra wraps some blocks in these
})
_REPORT_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "th": ["align"],
    "td": ["align"],
}
_REPORT_ALLOWED_PROTOCOLS = frozenset({"http", "https", "mailto"})


def _sanitize_report_html(raw_html: str) -> str:
    """Strip any tag or attribute a legitimate markdown render wouldn't
    produce, then re-close any tags the sanitizer left dangling.

    Called on the AI-rendered report before it lands in the template context
    for `|safe` rendering. Defense against two vectors:
      (a) AI prompt-injection: a document uploaded to the coach's caseload
          instructs the AI to embed `<script>` / `<img onerror>` in
          overall_summary; without this the payload persists in the report
          row and fires on every open by coach / principal / district.
      (b) Roster-name injection: a teacher CSV-imported with an HTML tag
          in `name` gets stitched into the markdown H1 by
          `_compose_report_markdown` — the tag would render otherwise.
    """
    return _bleach.clean(
        raw_html,
        tags=_REPORT_ALLOWED_TAGS,
        attributes=_REPORT_ALLOWED_ATTRS,
        protocols=_REPORT_ALLOWED_PROTOCOLS,
        strip=True,           # remove disallowed tags instead of escaping them
        strip_comments=True,  # HTML comments could carry conditional-comment IE-era payloads
    )
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.jobs import submit_job
from pipeline.db import (
    connect as db_connect, init_db,
    get_or_create_org, get_or_create_user, get_or_create_teacher, get_or_create_rubric_from_id,
    SELF_RATING_DIMENSIONS,
    upsert_teacher_profile_teacher_side, upsert_teacher_profile_coach_side, get_teacher_profile,
    create_goal, agree_goal, close_goal, list_goals_for_teacher,
    add_coach_private_note, list_private_notes_for_teacher,
    list_open_action_tracking_for_teacher, rubric_score_movement_for_teacher,
    assess_bite_sized_action,
    list_actions_issued_at, list_actions_available_for_assessment_at, list_actions_assessed_at,
    create_cycle, close_cycle, list_cycles_for_teacher, get_cycle,
    list_goals_in_cycle, list_observations_in_cycle,
    attach_observation_to_cycle, attach_goal_to_cycle,
    upsert_district_context, get_district_context,
    set_observation_debrief_focus,
    create_lesson_plan, add_lesson_plan_version, update_lesson_plan_status,
    add_lesson_plan_comment, list_lesson_plans_for_teacher, list_lesson_plans_for_org,
    get_lesson_plan, list_lesson_plan_versions, list_lesson_plan_comments,
    create_district_document, list_district_documents, archive_district_document,
    set_teacher_account_on_action, get_action,
    upsert_hlm_response, get_hlm_response, acknowledge_hlm_response,
    list_unacknowledged_hlm_responses, set_goal_releasing,
    publish_coach_move, edit_coach_move, get_current_coach_move,
    list_coach_move_history, most_recent_published_move_for_teacher,
    carry_forward_goal, set_action_goal, list_actions_for_goal,
    add_practice_log_entry, list_practice_log_for_teacher,
    bulk_create_teacher, ArchivedTeacherError,
    archive_teacher, restore_teacher,
    archive_observation, restore_observation,
    delete_private_note,
    create_magic_link_token, consume_magic_link_token,
    create_session, get_session, revoke_session,
    queue_email, list_recent_outbound_mail,
    get_active_consent, grant_consent, revoke_consent,
    SESSION_TTL_DAYS, MAGIC_LINK_TTL_MINUTES,
)
from pipeline.gbf import STEPS as GBF_STEPS, STEPS_BY_ID as GBF_STEPS_BY_ID, all_steps_by_phase
from pipeline.rubric import DEFAULT_RUBRIC_ID, RUBRICS, get_rubric


APP_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = APP_ROOT.parent
DB_PATH = Path(os.environ.get("OBSERVER_DB", PROJECT_ROOT / "reports" / "observations.sqlite"))
UPLOADS_DIR = Path(os.environ.get("OBSERVER_UPLOADS", APP_ROOT / "uploads"))

TEMPLATES = Jinja2Templates(directory=str(APP_ROOT / "templates"))

app = FastAPI(title="Classroom Observer")
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")
# Video files uploaded through the UI live under app/uploads/<obs_id>/<filename>
# and are served back so the observation detail can play them inline. The
# static mount used to be exposed unauthenticated at /uploads/... — a raw
# obs_id UUID leaking through email, logs, or shared-browser history would
# then hand anyone the classroom video. That mount is intentionally NOT
# present; the file is served by the gated /uploads/{observation_id}/{filename}
# route below, which enforces the same teacher-vs-obs owner rule as the
# observation detail page.
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Startup: init DB, seed single-user org
# ---------------------------------------------------------------------------

_SEEDED_IDS: dict[str, str] = {}


@app.on_event("startup")
def _bootstrap() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    conn = db_connect(DB_PATH)
    init_db(conn)
    # Single-user local install: prefer whatever org/user already exists so we
    # don't create a phantom "Local User" alongside a real "Default Org".
    existing_org = conn.execute(
        "SELECT id FROM organizations ORDER BY created_at ASC LIMIT 1"
    ).fetchone()
    if existing_org:
        org_id = existing_org["id"]
    else:
        org_id = get_or_create_org(conn, slug="local", name="Local User")

    existing_coach = conn.execute(
        "SELECT id FROM users WHERE role = 'coach' AND org_id = ? ORDER BY created_at ASC LIMIT 1",
        (org_id,),
    ).fetchone()
    if existing_coach:
        user_id = existing_coach["id"]
    else:
        user_id = get_or_create_user(
            conn, org_id=org_id, email="you@localhost", name="Local Coach", role="coach"
        )
    rubric_id = get_or_create_rubric_from_id(conn, org_id=None, rubric_id_kind=DEFAULT_RUBRIC_ID)
    _SEEDED_IDS.update({"org_id": org_id, "user_id": user_id, "rubric_id": rubric_id})
    conn.close()

    # Reclaim observations left in an in-flight status by a crashed worker
    # or a killed process. Without this, an interrupted job leaves the row
    # in 'transcribing'/'scoring' forever and blocks the coach from re-
    # uploading. See jobs.sweep_stuck_jobs for the threshold rationale.
    from app.jobs import sweep_stuck_jobs
    reclaimed = sweep_stuck_jobs(DB_PATH)
    if reclaimed:
        import logging
        logging.getLogger("uvicorn.error").info(
            "Startup watchdog: reclaimed %d stuck observation(s).", reclaimed,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: Optional[float]) -> str:
    if not seconds:
        return "—"
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _fmt_ts(ts: Optional[str]) -> str:
    if not ts:
        return "—"
    try:
        # Strip fractional seconds and timezone for compact display
        return ts.replace("T", " ")[:16]
    except Exception:
        return ts


TEMPLATES.env.filters["duration"] = _fmt_duration
TEMPLATES.env.filters["ts"] = _fmt_ts


def _fmt_warm_date(value) -> str:
    """Format a date as coach-copy would say it aloud.

    - Same-year dates → "Sep 12"
    - Other years    → "Sep 12, 2024"
    - Empty / unparseable → ""

    Reporting tables (compliance rows, timestamps for the record) stay on
    the raw string. This filter is for prose — dashboard cards, empty
    states, teacher hubs — where "2026-09-12" reads like a receipt.
    """
    if not value:
        return ""
    from datetime import datetime as _dt, date as _date
    try:
        s = str(value)
        if "T" in s:
            s = s.split("T", 1)[0]
        d = _date.fromisoformat(s[:10])
    except Exception:
        return str(value)
    now = _dt.now(timezone.utc).date()
    if d.year == now.year:
        return d.strftime("%b %-d")
    return d.strftime("%b %-d, %Y")


def _fmt_warm_datetime(value) -> str:
    """Warm form for a moment — "Sep 12 at 5:42 pm". Falls back to date-only
    ("Sep 12") when the input is date-only, so callers don't get a misleading
    "Sep 12 at 12:00 am" from a date that never had a time on it. Tolerates
    several input shapes because some callers pre-format via ``_fmt_ts``
    (space-separated) and some pass raw ISO.
    """
    if not value:
        return ""
    from datetime import datetime as _dt
    s = str(value).strip()
    if s in ("", "—"):
        return s
    # Date-only shortcut: if the string doesn't carry a time component,
    # render the warm-date form rather than fabricating a midnight timestamp.
    if len(s) <= 10 and "T" not in s and " " not in s:
        return _fmt_warm_date(value)
    # Try to parse a range of shapes: ISO with T/Z, space-separated compact,
    # date-only. Fall through to warm-date if nothing sticks.
    dt = None
    for candidate in (s, s.replace("Z", "+00:00"), s.replace(" ", "T", 1)):
        try:
            dt = _dt.fromisoformat(candidate)
            break
        except Exception:
            continue
    if dt is None:
        # Last chance: try common compact form "YYYY-MM-DD HH:MM"
        try:
            dt = _dt.strptime(s[:16], "%Y-%m-%d %H:%M")
        except Exception:
            return _fmt_warm_date(value)
    now = _dt.now(timezone.utc)
    when = dt.strftime("%b %-d")
    if dt.year != now.year:
        when = dt.strftime("%b %-d, %Y")
    time_part = dt.strftime("%-I:%M %p").lower()
    return f"{when} at {time_part}"


TEMPLATES.env.filters["warmdate"] = _fmt_warm_date
TEMPLATES.env.filters["warmdatetime"] = _fmt_warm_datetime


def _cycle_progress(cycle: dict) -> Optional[dict]:
    """Compute 'week X of Y' + days remaining for a cycle.
    Returns None when required dates are missing.
    """
    opened = cycle.get("opened_at")
    expected_close = cycle.get("expected_close_date")
    if not opened or not expected_close:
        return None
    from datetime import datetime as _dt, date as _date
    try:
        opened_date = _dt.fromisoformat(opened).date()
    except Exception:
        return None
    try:
        end_date = _date.fromisoformat(expected_close)
    except Exception:
        return None
    today = _dt.now().date()
    total_days = max(1, (end_date - opened_date).days)
    days_in = max(0, (today - opened_date).days)
    week_current = min(days_in // 7 + 1, (total_days + 6) // 7)
    week_total = max(1, (total_days + 6) // 7)
    days_remaining = (end_date - today).days
    return {
        "week_current": week_current,
        "week_total": week_total,
        "days_remaining": days_remaining,
        "total_days": total_days,
        "opened_date": opened_date.isoformat(),
        "end_date": end_date.isoformat(),
    }


TEMPLATES.env.globals["cycle_progress"] = _cycle_progress


# ---------------------------------------------------------------------------
# Viewer / role boundary (lightweight — pre-auth)
# ---------------------------------------------------------------------------
# Cookie name: "viewer". Values:
#   "coach"           → full access (default)
#   "teacher:<uuid>"  → teacher self-service — can only view/write their own
#
# Not a real auth system. There are no passwords. This gates the two-sided
# workflow so a teacher can't post to another teacher's routes, and so the
# UI can render the appropriate view. Real auth would replace this cookie
# with a signed session and back it with a users table lookup.

VIEWER_COOKIE = "viewer"
SESSION_COOKIE = "cobs_session"

# Dev-only bypass: when OBSERVER_DEV_LOGIN=1, the legacy persona-switcher
# cookie still works (coach / principal / district / teacher:<id>). This
# lets me smoke-test the four surfaces without going through email. In a
# real deploy the env var is unset and the persona cookie is ignored.
DEV_LOGIN_ENABLED = os.environ.get("OBSERVER_DEV_LOGIN", "").strip() == "1"


def _base_url_for_email(request: Request) -> str:
    """Origin (scheme://host) to use in magic-link URLs sent by email.
    Prefers OBSERVER_PUBLIC_URL when set (production behind a proxy), falls
    back to the request's own scheme/host (dev + local pilot).
    """
    override = os.environ.get("OBSERVER_PUBLIC_URL", "").strip()
    if override:
        return override.rstrip("/")
    return f"{request.url.scheme}://{request.url.netloc}"


def _next_obs_due(conn, teacher_id: str, days_between_target: Optional[int]) -> Optional[dict]:
    """Compute when the next observation is 'due' per district cadence.

    Returns {'due_date', 'days_until', 'overdue', 'last_obs_at'} or None
    when either no target is set or the teacher has never been observed.
    """
    if not days_between_target:
        return None
    from datetime import datetime as _d, timedelta as _td, date as _date
    row = conn.execute(
        """SELECT MAX(COALESCE(observed_at, scored_at, uploaded_at)) AS last_at
           FROM observations
           WHERE teacher_id = ? AND status = 'complete' AND deleted_at IS NULL""",
        (teacher_id,),
    ).fetchone()
    if not row or not row["last_at"]:
        return None
    try:
        last_dt = _d.fromisoformat(row["last_at"].replace("Z", "+00:00")).date()
    except Exception:
        return None
    due_date = last_dt + _td(days=days_between_target)
    today = _d.now(timezone.utc).date()
    days_until = (due_date - today).days
    return {
        "due_date": due_date.isoformat(),
        "days_until": days_until,
        "overdue": days_until < 0,
        "last_obs_at": last_dt.isoformat(),
    }


def _current_viewer(request: Request) -> dict:
    """Return the viewer record for this request, or an anonymous viewer.

    Priority:
      1. Real session cookie (SESSION_COOKIE) — the primary auth path. When
         valid, returns a full viewer with user_id / org_id / email plus the
         legacy 'role' key that every downstream check reads.
      2. Legacy persona cookie (VIEWER_COOKIE) — ONLY honored when the env
         var OBSERVER_DEV_LOGIN=1 is set. Lets me keep smoke-testing the four
         surfaces without going through email; off in production.
      3. Anonymous — {'role': 'anon'}. Every gated route sends this to /signin.

    Viewer keys:
      role: 'coach' | 'principal' | 'district' | 'teacher' | 'anon'
      user_id / email / name / org_id: present when signed in via session
      teacher_id: present for role='teacher' (the teacher's own record)
    """
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        conn = db_connect(DB_PATH)
        try:
            sess = get_session(conn, session_id=sid)
        finally:
            conn.close()
        if sess:
            v = {
                "role": sess["role"],
                "user_id": sess["user_id"],
                "email": sess["email"],
                "name": sess["name"],
                "org_id": sess["org_id"],
                "session_id": sess["session_id"],
                "authed_via": "session",
            }
            # A teacher-role user's teacher_id is looked up by user_id → teacher
            # (the user IS a teacher; the teacher row's own id is what routes
            # gate on). Cached inside the request-scoped viewer to avoid re-hits.
            #
            # Email match is case-insensitive on both sides: users are stored
            # verbatim as typed at sign-up, teachers imported by CSV keep the
            # roster's spelling, and neither is guaranteed to match. A
            # case-mismatch here would silently drop teacher_id off the viewer
            # and make every teacher-scoped route refuse the teacher's own
            # requests as 403.
            if sess["role"] == "teacher":
                conn = db_connect(DB_PATH)
                try:
                    tr = conn.execute(
                        "SELECT id FROM teachers WHERE assigned_coach_user_id IS NOT NULL "
                        "AND lower(email) = lower(?)",
                        (sess["email"],),
                    ).fetchone() or conn.execute(
                        "SELECT id FROM teachers WHERE lower(email) = lower(?)",
                        (sess["email"],),
                    ).fetchone()
                    if tr:
                        v["teacher_id"] = tr["id"]
                finally:
                    conn.close()
            return v

    if DEV_LOGIN_ENABLED:
        raw = request.cookies.get(VIEWER_COOKIE)
        if raw == "principal":
            return {"role": "principal", "authed_via": "dev", "org_id": _SEEDED_IDS.get("org_id"), "user_id": _SEEDED_IDS.get("user_id")}
        if raw == "district":
            return {"role": "district", "authed_via": "dev", "org_id": _SEEDED_IDS.get("org_id"), "user_id": _SEEDED_IDS.get("user_id")}
        if raw and raw.startswith("teacher:"):
            return {"role": "teacher", "teacher_id": raw.split(":", 1)[1], "authed_via": "dev", "org_id": _SEEDED_IDS.get("org_id")}
        if raw == "coach" or raw is None:
            return {"role": "coach", "authed_via": "dev", "org_id": _SEEDED_IDS.get("org_id"), "user_id": _SEEDED_IDS.get("user_id")}

    return {"role": "anon"}


# Paths that must remain reachable to an anon viewer — the auth surface
# itself and static assets. Exact matches AND prefix matches are stored
# separately so a future route like /signup or /favicon-hex-of-doom can't
# slip past the anon gate by a startswith accident (the middleware treats
# every allowlist entry as an exact-or-with-trailing-slash prefix).
_ANON_ALLOWED_EXACT = frozenset({
    "/signin", "/signout", "/favicon.ico", "/dev/mail",
})
_ANON_ALLOWED_PREFIXES = (
    "/auth/", "/static/",
)


def _require_signed_in(request: Request, viewer: dict) -> Optional[RedirectResponse]:
    """Called at the top of any gated route. Returns a RedirectResponse to
    /signin?next=<current path> when the viewer is anon; None when signed in.
    """
    if viewer.get("role") != "anon":
        return None
    from urllib.parse import quote
    next_path = request.url.path
    if request.url.query:
        next_path += "?" + request.url.query
    return RedirectResponse(url=f"/signin?next={quote(next_path, safe='/?=&')}", status_code=303)


def _viewer_can_edit_teacher(viewer: dict, teacher_id: str) -> bool:
    """Legacy gate — coach may edit any teacher on their caseload, teacher
    may only edit their own. Kept for compatibility; new code should call
    :func:`_require_teacher_access`.
    """
    if viewer["role"] in ("coach", "principal", "district"):
        return True
    return viewer.get("teacher_id") == teacher_id


def _teacher_on_coach_caseload(conn, *, teacher_id: str, coach_user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM teachers WHERE id = ? AND assigned_coach_user_id = ?",
        (teacher_id, coach_user_id),
    ).fetchone()
    return row is not None


def _teacher_in_org(conn, *, teacher_id: str, org_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM teachers WHERE id = ? AND org_id = ?", (teacher_id, org_id),
    ).fetchone()
    return row is not None


def _require_teacher_access(viewer: dict, teacher_id: str, action: str = "This action") -> None:
    """Refuse the request when the viewer has no legitimate relationship to
    this teacher. Rules:
      coach     — teacher must be on their caseload
      principal — teacher must be in their org
      district  — teacher must be in an org they administer (today: any org)
      teacher   — teacher_id must be their own

    Raises HTTPException(403) on mismatch. Anonymous viewers should have
    been redirected upstream by ``_require_signed_in``.
    """
    role = viewer.get("role")
    if role == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    if role == "teacher":
        if viewer.get("teacher_id") != teacher_id:
            raise HTTPException(403, "Not authorized to access this teacher")
        return
    if role == "district":
        return  # district can reach any teacher (revisit when districts are separately administered)
    conn = db_connect(DB_PATH)
    try:
        if role == "coach":
            uid = viewer.get("user_id")
            if not uid or not _teacher_on_coach_caseload(conn, teacher_id=teacher_id, coach_user_id=uid):
                raise HTTPException(403, "Not authorized to access this teacher")
            return
        if role == "principal":
            oid = viewer.get("org_id")
            if not oid or not _teacher_in_org(conn, teacher_id=teacher_id, org_id=oid):
                raise HTTPException(403, "Not authorized to access this teacher")
            return
    finally:
        conn.close()
    raise HTTPException(403, f"{action} refused for role {role!r}")


def _guard_teacher_write(request: Request, teacher_id: str, action: str = "Write") -> dict:
    """One-liner used at the top of every teacher-scoped write route.

    - Refuses anon (should already be redirected upstream by middleware, but
      belt-and-suspenders for direct POSTs).
    - Enforces cross-teacher URL-forge protection: coach must own this
      teacher, principal must be in the same org, teacher-role viewers may
      only write their own.

    Returns the viewer dict so the caller can read role/user_id without
    calling ``_current_viewer`` again.
    """
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    _require_teacher_access(viewer, teacher_id, action)
    return viewer


# ---------------------------------------------------------------------------
# Entity-scoped guards — resolve the entity's teacher_id and defer to
# _require_teacher_access. Cross-caseload URL-forges (a second coach
# hitting the first coach's obs/cycle/goal/action/plan via the id in the
# URL) get 403; unknown ids get 404 so the id space stays opaque.
# ---------------------------------------------------------------------------


def _resolve_obs_teacher(conn, observation_id: str) -> str:
    row = conn.execute(
        "SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Observation not found")
    return row["teacher_id"]


def _resolve_cycle_teacher(conn, cycle_id: str) -> str:
    row = conn.execute(
        "SELECT teacher_id FROM coaching_cycles WHERE id = ?", (cycle_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Cycle not found")
    return row["teacher_id"]


def _resolve_goal_teacher(conn, goal_id: str) -> str:
    row = conn.execute(
        "SELECT teacher_id FROM professional_goals WHERE id = ?", (goal_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Goal not found")
    return row["teacher_id"]


def _resolve_action_teacher(conn, action_id: str) -> str:
    row = conn.execute(
        "SELECT teacher_id FROM bite_sized_action_tracking WHERE id = ?", (action_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Action not found")
    return row["teacher_id"]


def _resolve_plan_teacher(conn, plan_id: str) -> str:
    row = conn.execute(
        "SELECT teacher_id FROM lesson_plans WHERE id = ?", (plan_id,)
    ).fetchone()
    if not row:
        raise HTTPException(404, "Lesson plan not found")
    return row["teacher_id"]


def _guard_obs_write(request: Request, observation_id: str, action: str = "Write") -> dict:
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    conn = db_connect(DB_PATH)
    try:
        tid = _resolve_obs_teacher(conn, observation_id)
    finally:
        conn.close()
    _require_teacher_access(viewer, tid, action)
    return viewer


def _guard_cycle_write(request: Request, cycle_id: str, action: str = "Write") -> dict:
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    conn = db_connect(DB_PATH)
    try:
        tid = _resolve_cycle_teacher(conn, cycle_id)
    finally:
        conn.close()
    _require_teacher_access(viewer, tid, action)
    return viewer


def _guard_goal_write(request: Request, goal_id: str, action: str = "Write") -> dict:
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    conn = db_connect(DB_PATH)
    try:
        tid = _resolve_goal_teacher(conn, goal_id)
    finally:
        conn.close()
    _require_teacher_access(viewer, tid, action)
    return viewer


def _guard_action_write(request: Request, action_id: str, action: str = "Write") -> dict:
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    conn = db_connect(DB_PATH)
    try:
        tid = _resolve_action_teacher(conn, action_id)
    finally:
        conn.close()
    _require_teacher_access(viewer, tid, action)
    return viewer


def _guard_plan_write(request: Request, plan_id: str, action: str = "Write") -> dict:
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, f"{action} needs a sign-in")
    conn = db_connect(DB_PATH)
    try:
        tid = _resolve_plan_teacher(conn, plan_id)
    finally:
        conn.close()
    _require_teacher_access(viewer, tid, action)
    return viewer


def _viewer_uid(viewer: dict) -> str:
    """Author-of-record for a write path. Prefers the current viewer's user_id
    (real session), falls back to the seeded id (script paths, tests). In
    multi-coach production every route reaches this via a signed session, so
    the viewer wins.
    """
    return viewer.get("user_id") or _SEEDED_IDS.get("user_id")


def _scope_filter(viewer: dict, *, teachers_alias: str = "t") -> tuple[str, list]:
    """Return a SQL fragment + params that scopes a query to teachers the
    viewer may see. Meant to be spliced into aggregate queries whose FROM
    already joins ``teachers <teachers_alias>``.

    Coach     → WHERE {t}.assigned_coach_user_id = ?
    Principal → WHERE {t}.org_id = ?
    District  → no filter (returns empty fragment)
    Teacher   → WHERE {t}.id = ?
    Anon      → WHERE 1=0  (should never render; anon is redirected upstream)

    Callers combine with existing WHERE clauses via " AND " or start a WHERE
    clause depending on their query shape. Use ``_where(prefix)`` below to
    stitch cleanly.
    """
    role = viewer.get("role")
    if role == "coach":
        uid = viewer.get("user_id") or ""
        return f"{teachers_alias}.assigned_coach_user_id = ?", [uid]
    if role == "principal":
        return f"{teachers_alias}.org_id = ?", [viewer.get("org_id") or ""]
    if role == "teacher":
        return f"{teachers_alias}.id = ?", [viewer.get("teacher_id") or ""]
    if role == "district":
        return "", []
    return "1 = 0", []


def _teacher_is_archived(conn, teacher_id: str) -> bool:
    """True if the teacher exists AND is archived. Missing teachers return
    False — the caller should already 404 on those; this helper is for the
    archived vs. live distinction, not for existence.
    """
    row = conn.execute(
        "SELECT archived_at FROM teachers WHERE id = ?", (teacher_id,)
    ).fetchone()
    return bool(row and row["archived_at"])


def _is_safe_same_origin_path(candidate: str) -> bool:
    """True iff `candidate` is safe to use as an HTTP redirect target
    without leaving this origin. Guards against:
      - Protocol-relative URLs: `//evil.com/x` (starts with `//`).
      - Backslash tricks: `/\evil.com/x` — Chrome / Firefox / Safari
        normalize `\\` to `/` during URL parsing, so this lands cross-origin.
      - Percent-encoded backslash: `/%5cevil.com`.
      - Any char outside a conservative same-origin path alphabet.
    """
    if not candidate or not candidate.startswith("/"):
        return False
    if candidate.startswith("//"):
        return False
    # Reject any backslash (raw or percent-encoded, either case).
    lowered = candidate.lower()
    if "\\" in candidate or "%5c" in lowered:
        return False
    # Whitelist path/query/fragment characters. Restrictive on purpose; if
    # future code needs weirder characters, extend deliberately.
    _safe = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                "0123456789-._~/?#[]@!$&'()*+,;=%:")
    return all(c in _safe for c in candidate)


def _refuse_if_archived(conn, teacher_id: str, what: str = "Write") -> None:
    """Raise 409 if the teacher is archived. Used on write paths so an
    archived teacher can't accumulate new work (which would otherwise be
    invisible to every roster / dashboard / compliance query — F4 in the
    round-6 audit). Reads are unaffected; historical records stay viewable.
    """
    if _teacher_is_archived(conn, teacher_id):
        raise HTTPException(
            409, f"{what} refused: teacher record is archived. Restore first."
        )


def _deny_teacher(viewer: dict, what: str = "This page") -> None:
    """Raise 403 if viewer is the teacher role. Used on aggregate pages
    (roster, all-observations, principal/district hubs, compliance) that
    show cross-teacher data — the teacher role must never reach them.
    """
    if viewer["role"] == "teacher":
        raise HTTPException(403, f"{what} is not available to the teacher role")


def _require_coach(viewer: dict, what: str = "This action") -> None:
    """Raise 403 for anyone except coach. Used on coach-only writes: goal
    close, cycle open/close, coach-move publish, lesson-plan status, private
    notes, admin routes. Principal / district viewers are read-only in this
    build; if that changes, widen this helper.
    """
    if viewer.get("role") != "coach":
        raise HTTPException(403, f"{what} is coach-only")


def _obs_teacher_id(conn, observation_id: str) -> Optional[str]:
    """teacher_id of an observation, or None if no such observation."""
    row = conn.execute(
        "SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)
    ).fetchone()
    return row["teacher_id"] if row else None


def _viewer_from_obs(viewer: dict, conn, observation_id: str) -> bool:
    if viewer["role"] == "coach":
        return True
    row = conn.execute("SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)).fetchone()
    return bool(row) and viewer.get("teacher_id") == row["teacher_id"]


def _viewer_from_action(viewer: dict, conn, action_id: str) -> bool:
    if viewer["role"] == "coach":
        return True
    row = conn.execute(
        "SELECT teacher_id FROM bite_sized_action_tracking WHERE id = ?", (action_id,)
    ).fetchone()
    return bool(row) and viewer.get("teacher_id") == row["teacher_id"]


# Make viewer available in every template automatically.
@app.middleware("http")
async def _inject_viewer(request: Request, call_next):
    """Attach the viewer to every request, and gate anon requests to a
    minimal allowlist. The allowlist keeps the auth surface itself, static
    assets, and the dev mail viewer reachable without a session — everything
    else redirects to /signin?next=<current path>.
    """
    request.state.viewer = _current_viewer(request)
    if request.state.viewer.get("role") == "anon":
        path = request.url.path
        # Prefixes are declared with a trailing slash so /signup can't
        # match "/signin"'s startswith or /faviconics's match "/favicon".
        # Exact matches handle the fixed paths.
        is_anon_ok = path in _ANON_ALLOWED_EXACT or any(
            path.startswith(p) for p in _ANON_ALLOWED_PREFIXES
        )
        if not is_anon_ok:
            from urllib.parse import quote
            nxt = path
            if request.url.query:
                nxt += "?" + request.url.query
            return RedirectResponse(
                url=f"/signin?next={quote(nxt, safe='/?=&')}", status_code=303,
            )
    return await call_next(request)


def _template_ctx(request: Request, **extra) -> dict:
    """Convenience for routes that want the viewer in the template context."""
    return {"request": request, "viewer": request.state.viewer, **extra}


TEMPLATES.env.globals["current_role"] = lambda request: getattr(
    request.state, "viewer", {"role": "coach"}
)["role"]


def _avatar_idx(name: str) -> int:
    """Deterministic 0-5 palette index for coloring an avatar by name."""
    return sum(ord(c) for c in (name or "")) % 6


TEMPLATES.env.globals["avatar_idx"] = _avatar_idx
TEMPLATES.env.globals["avatar_palette"] = ["blue", "teal", "amber", "plum", "green", "rose"]
TEMPLATES.env.globals["DEV_LOGIN_ENABLED"] = DEV_LOGIN_ENABLED


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard_home(request: Request) -> HTMLResponse:
    """Homepage: org-level dashboard.

    Aggregates:
      - Observation count (total, in flight, complete)
      - Bite-sized action tracking: implementation rate (full+0.5*partial / assessed)
      - Action success rate (full / assessed)
      - Rubric score trend per domain across all teachers (median rating over time)
      - Teacher count, active cycles, active goals

    Role gate: teacher role gets redirected to their own teacher-view. The
    dashboard is cross-teacher aggregate data and coach-oriented.
    """
    _viewer_home = _current_viewer(request)
    if _viewer_home["role"] == "teacher":
        tid = _viewer_home.get("teacher_id")
        if not tid:
            raise HTTPException(403, "Teacher role missing teacher_id")
        return RedirectResponse(url=f"/teachers/{tid}/teacher-view", status_code=303)
    from datetime import datetime as _dt
    from collections import Counter

    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    _dash_scope_sql, _dash_scope_params = _scope_filter(_viewer_home, teachers_alias="t")
    _dash_scope_and = (" AND " + _dash_scope_sql) if _dash_scope_sql else ""
    conn = db_connect(DB_PATH)
    try:
        # Observation counts — scoped through a JOIN on teachers so the
        # coach's dashboard reflects only their caseload's observations.
        obs_counts = {"total": 0, "in_flight": 0, "complete": 0, "failed": 0}
        for row in conn.execute(
            f"""SELECT o.status, COUNT(*) AS c
               FROM observations o JOIN teachers t ON t.id = o.teacher_id
               WHERE o.deleted_at IS NULL AND t.archived_at IS NULL{_dash_scope_and}
               GROUP BY o.status""",
            _dash_scope_params,
        ):
            obs_counts["total"] += row["c"]
            if row["status"] == "complete":
                obs_counts["complete"] += row["c"]
            elif row["status"] == "failed":
                obs_counts["failed"] += row["c"]
            else:
                obs_counts["in_flight"] += row["c"]

        # Teacher / cycle / goal counts — all scoped to the viewer.
        teacher_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM teachers t WHERE t.archived_at IS NULL{_dash_scope_and}",
            _dash_scope_params,
        ).fetchone()["c"]
        active_cycle_count = conn.execute(
            f"""SELECT COUNT(*) AS c FROM coaching_cycles c
               JOIN teachers t ON t.id = c.teacher_id
               WHERE c.closed_at IS NULL AND t.archived_at IS NULL{_dash_scope_and}""",
            _dash_scope_params,
        ).fetchone()["c"]
        active_goal_count = conn.execute(
            f"""SELECT COUNT(*) AS c FROM professional_goals g
               JOIN teachers t ON t.id = g.teacher_id
               WHERE g.status IN ('proposed', 'active')
                 AND t.archived_at IS NULL{_dash_scope_and}""",
            _dash_scope_params,
        ).fetchone()["c"]

        # Bite-sized action implementation stats (across all)
        action_rows = conn.execute(
            "SELECT implementation FROM bite_sized_action_tracking"
        ).fetchall()
        total_actions = len(action_rows)
        assessed = [r for r in action_rows if r["implementation"]]
        by_status = Counter(r["implementation"] for r in assessed)
        implementation_rate = None
        success_rate = None
        if assessed:
            implementation_rate = round(
                (by_status.get("full", 0) + 0.5 * by_status.get("partial", 0)) / len(assessed) * 100, 1
            )
            success_rate = round(by_status.get("full", 0) / len(assessed) * 100, 1)

        # Rubric-rating distribution across all published reports (latest per obs).
        rating_counts = {d: Counter() for d in rubric.domains}
        for row in conn.execute(
            """SELECT rv.domain_assessments FROM report_versions rv
               JOIN observations o ON o.id = rv.observation_id
               WHERE o.status = 'complete' AND o.deleted_at IS NULL
                 AND rv.published_at IS NOT NULL"""
        ):
            try:
                das = json.loads(row["domain_assessments"] or "[]")
            except Exception:
                continue
            for da in das:
                d = da.get("domain")
                r = da.get("overall_rating")
                if d in rating_counts and r:
                    rating_counts[d][r] += 1

        # Recent observations (top 8) — coach's caseload only.
        recent_rows = conn.execute(
            f"""SELECT o.id, o.video_filename, o.status, o.uploaded_at, o.scored_at,
                      t.name AS teacher_name
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               WHERE o.deleted_at IS NULL AND t.archived_at IS NULL{_dash_scope_and}
               ORDER BY o.uploaded_at DESC LIMIT 8""",
            _dash_scope_params,
        ).fetchall()

        # Lesson plan status counts
        lp_status_counts = Counter()
        for row in conn.execute("SELECT status, COUNT(*) AS c FROM lesson_plans GROUP BY status"):
            lp_status_counts[row["status"]] = row["c"]

        # --- "Needs your attention" surfacing (coach-facing) ---
        from datetime import datetime, timezone, timedelta
        _now = datetime.now(timezone.utc)
        _2d = (_now - timedelta(days=2)).isoformat()
        _3d = (_now - timedelta(days=3)).isoformat()
        _7d = (_now - timedelta(days=7)).isoformat()
        _plus7 = (_now + timedelta(days=7)).date().isoformat()

        # District's debrief cadence target (days from obs → debrief). Falls back
        # to the hardcoded 2-day threshold when not configured.
        _year_dc_early = f"{_now.year - 1}-{_now.year}" if _now.month < 8 else f"{_now.year}-{_now.year + 1}"
        _dc_early = get_district_context(conn, org_id=_SEEDED_IDS["org_id"], academic_year=_year_dc_early)
        _debrief_target_days = (_dc_early or {}).get("days_from_obs_to_debrief_target") or 2
        _debrief_threshold = (_now - timedelta(days=_debrief_target_days)).isoformat()

        # Fire when: complete obs, past cadence, AND coach hasn't finished the
        # debrief work yet (either no debrief_focus OR no published coach move).
        attention_debriefs = [dict(r) for r in conn.execute(
            f"""SELECT o.id AS obs_id, o.scored_at, t.id AS teacher_id, t.name AS teacher_name,
                      (o.debrief_focus IS NULL OR o.debrief_focus = '') AS no_focus,
                      NOT EXISTS (SELECT 1 FROM published_coach_moves pcm
                                  WHERE pcm.observation_id = o.id AND pcm.superseded_at IS NULL) AS no_move
               FROM observations o JOIN teachers t ON t.id = o.teacher_id
               WHERE o.status = 'complete'
                 AND o.deleted_at IS NULL AND t.archived_at IS NULL{_dash_scope_and}
                 AND o.scored_at IS NOT NULL AND o.scored_at < ?
                 AND ((o.debrief_focus IS NULL OR o.debrief_focus = '')
                      OR NOT EXISTS (SELECT 1 FROM published_coach_moves pcm
                                     WHERE pcm.observation_id = o.id AND pcm.superseded_at IS NULL))
               ORDER BY o.scored_at DESC LIMIT 5""",
            (*_dash_scope_params, _debrief_threshold),
        ).fetchall()]

        attention_actions = [dict(r) for r in conn.execute(
            f"""SELECT ba.id AS action_id, ba.bite_sized_action_text, ba.created_at,
                      t.id AS teacher_id, t.name AS teacher_name,
                      ba.source_observation_id
               FROM bite_sized_action_tracking ba
               JOIN teachers t ON t.id = ba.teacher_id
               JOIN observations src ON src.id = ba.source_observation_id
               WHERE ba.implementation IS NULL AND ba.created_at < ?
                 AND src.deleted_at IS NULL AND t.archived_at IS NULL{_dash_scope_and}
               ORDER BY ba.created_at ASC LIMIT 5""",
            (_7d, *_dash_scope_params),
        ).fetchall()]

        attention_cycles = [dict(r) for r in conn.execute(
            f"""SELECT c.id AS cycle_id, c.expected_close_date,
                      t.id AS teacher_id, t.name AS teacher_name
               FROM coaching_cycles c
               JOIN teachers t ON t.id = c.teacher_id
               WHERE c.closed_at IS NULL
                 AND c.expected_close_date IS NOT NULL
                 AND c.expected_close_date <= ?
                 AND t.archived_at IS NULL{_dash_scope_and}
               ORDER BY c.expected_close_date ASC LIMIT 5""",
            (_plus7, *_dash_scope_params),
        ).fetchall()]

        attention_lps = [dict(r) for r in conn.execute(
            f"""SELECT lp.id AS lp_id, lp.title, lp.status,
                      COALESCE(lp.updated_at, lp.created_at) AS last_touched,
                      t.id AS teacher_id, t.name AS teacher_name
               FROM lesson_plans lp
               JOIN teachers t ON t.id = lp.teacher_id
               WHERE lp.status IN ('submitted', 'revision_requested')
                 AND COALESCE(lp.updated_at, lp.created_at) < ?
                 AND t.archived_at IS NULL{_dash_scope_and}
               ORDER BY last_touched ASC LIMIT 5""",
            (_3d, *_dash_scope_params),
        ).fetchall()]

        # "Teacher responded to your move" — unacknowledged adjust/talk responses.
        # (Not yet coach-scoped; list_unacknowledged_hlm_responses filters by
        # archived teacher, which is enough for pilot — every teacher on file
        # is on this coach's caseload in single-coach mode.)
        attention_responses = list_unacknowledged_hlm_responses(conn)

        # "Proposed goals waiting to be agreed" — carry-forward / district-priority
        # goals sit in 'proposed' until the coach + teacher agree. Older than 7 days
        # signals they've been forgotten.
        attention_proposed_goals = [dict(r) for r in conn.execute(
            f"""SELECT pg.id AS goal_id, pg.title, pg.proposed_at,
                      t.id AS teacher_id, t.name AS teacher_name
               FROM professional_goals pg
               JOIN teachers t ON t.id = pg.teacher_id
               WHERE pg.status = 'proposed' AND pg.proposed_at < ?
                 AND t.archived_at IS NULL{_dash_scope_and}
               ORDER BY pg.proposed_at ASC LIMIT 5""",
            (_7d, *_dash_scope_params),
        ).fetchall()]
        # `_days_since` is defined later; enrich these rows down where it exists.

        # "Teacher profile empty" — teacher exists but has no profile row / no fields.
        # AI recommendations are thinner without profile context.
        attention_no_profile = [dict(r) for r in conn.execute(
            f"""SELECT t.id AS teacher_id, t.name AS teacher_name
               FROM teachers t
               LEFT JOIN teacher_profiles tp ON tp.teacher_id = t.id
               WHERE t.archived_at IS NULL
                 AND (tp.teacher_id IS NULL
                      OR (tp.years_teaching_total IS NULL
                          AND tp.coaching_style_preference IS NULL)){_dash_scope_and}
               ORDER BY t.name""",
            _dash_scope_params,
        ).fetchall()]

        # "Teachers overdue for observation" — per district cadence expectation.
        attention_obs_due = []
        _year_dc = f"{_now.year - 1}-{_now.year}" if _now.month < 8 else f"{_now.year}-{_now.year + 1}"
        _dash_dc = get_district_context(conn, org_id=_SEEDED_IDS["org_id"], academic_year=_year_dc)
        _days_between = _dash_dc.get("days_between_obs_target") if _dash_dc else None
        if _days_between:
            for t in conn.execute(
                f"SELECT t.id, t.name FROM teachers t WHERE t.archived_at IS NULL{_dash_scope_and} ORDER BY t.name",
                _dash_scope_params,
            ).fetchall():
                due = _next_obs_due(conn, t["id"], _days_between)
                if due and due["overdue"]:
                    attention_obs_due.append({
                        "teacher_id": t["id"], "teacher_name": t["name"],
                        "days_late": -due["days_until"], "last_obs_at": due["last_obs_at"],
                    })

        # Days-late helper for each list (computed for display).
        def _days_since(iso_str: str) -> int:
            try:
                dt = _dt.fromisoformat(iso_str.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, (_now - dt).days)
            except Exception:
                return 0

        for row in attention_debriefs:
            row["days"] = _days_since(row["scored_at"])
        for row in attention_actions:
            row["days"] = _days_since(row["created_at"])
        for row in attention_lps:
            row["days"] = _days_since(row["last_touched"])
        for row in attention_responses:
            row["days"] = _days_since(row["created_at"])
        for row in attention_proposed_goals:
            row["days"] = _days_since(row["proposed_at"])
        for row in attention_cycles:
            try:
                from datetime import date as _date
                d = _date.fromisoformat(row["expected_close_date"])
                row["days_until"] = (d - _now.date()).days
            except Exception:
                row["days_until"] = 0

        attention_total = (
            len(attention_debriefs) + len(attention_actions)
            + len(attention_cycles) + len(attention_lps)
            + len(attention_responses) + len(attention_obs_due)
            + len(attention_no_profile) + len(attention_proposed_goals)
        )

        # Recent teachers for the tile row (top 6 by most recent activity),
        # scoped to the coach's caseload.
        recent_teachers = [dict(r) for r in conn.execute(
            f"""SELECT t.id, t.name,
                      MAX(COALESCE(o.uploaded_at, o.scored_at, t.created_at)) AS last_activity,
                      (SELECT COUNT(*) FROM observations o2
                       WHERE o2.teacher_id = t.id AND o2.deleted_at IS NULL) AS obs_count
               FROM teachers t
               LEFT JOIN observations o
                      ON o.teacher_id = t.id AND o.deleted_at IS NULL
               WHERE t.archived_at IS NULL{_dash_scope_and}
               GROUP BY t.id
               ORDER BY last_activity DESC LIMIT 6""",
            _dash_scope_params,
        ).fetchall()]

    finally:
        conn.close()

    # Time-of-day greeting for priming (server local time).
    _hour = _dt.now().hour
    if _hour < 12:
        _greeting = "Good morning"
    elif _hour < 17:
        _greeting = "Good afternoon"
    else:
        _greeting = "Good evening"

    return TEMPLATES.TemplateResponse("dashboard.html", {
        "request": request,
        "greeting": _greeting,
        "attention_debriefs": attention_debriefs,
        "attention_actions": attention_actions,
        "attention_cycles": attention_cycles,
        "attention_lps": attention_lps,
        "attention_responses": attention_responses,
        "attention_obs_due": attention_obs_due,
        "attention_no_profile": attention_no_profile,
        "attention_proposed_goals": attention_proposed_goals,
        "attention_total": attention_total,
        "recent_teachers": recent_teachers,
        "obs_counts": obs_counts,
        "teacher_count": teacher_count,
        "active_cycle_count": active_cycle_count,
        "active_goal_count": active_goal_count,
        "total_actions": total_actions,
        "assessed_actions": len(assessed),
        "action_status_counts": dict(by_status),
        "implementation_rate": implementation_rate,
        "success_rate": success_rate,
        "rating_counts": {d: dict(c) for d, c in rating_counts.items()},
        "rating_levels": rubric.rating_levels,
        "domains": rubric.domains,
        "recent_observations": [dict(r) for r in recent_rows],
        "lesson_plan_status_counts": dict(lp_status_counts),
    })


@app.get("/observations", response_class=HTMLResponse)
def list_observations(request: Request) -> HTMLResponse:
    """Observations list (moved from / to /observations when the dashboard moved into /)."""
    viewer = _current_viewer(request)
    _deny_teacher(viewer, "All-observations list")
    scope_sql, scope_params = _scope_filter(viewer, teachers_alias="t")
    _scope_and = (" AND " + scope_sql) if scope_sql else ""
    conn = db_connect(DB_PATH)
    try:
        rows = conn.execute(
            f"""SELECT o.id, o.video_filename, o.video_duration_s, o.status,
                      o.scored_at, o.uploaded_at, o.failure_reason,
                      t.name AS teacher_name,
                      r.name AS rubric_name,
                      (SELECT rv.domain_assessments
                       FROM report_versions rv
                       WHERE rv.observation_id = o.id
                       ORDER BY rv.version_number DESC LIMIT 1) AS latest_das
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               JOIN rubrics  r ON r.id = o.rubric_id
               WHERE o.deleted_at IS NULL{_scope_and}
               ORDER BY o.uploaded_at DESC""",
            scope_params,
        ).fetchall()

        observations = []
        for row in rows:
            das = json.loads(row["latest_das"]) if row["latest_das"] else []
            ratings = {da["domain"]: da["overall_rating"] for da in das}
            observations.append({
                "id": row["id"],
                "teacher": row["teacher_name"],
                "video_filename": row["video_filename"],
                "duration": _fmt_duration(row["video_duration_s"]),
                "status": row["status"],
                "failure_reason": row["failure_reason"],
                "rubric_name": row["rubric_name"],
                "scored_at": _fmt_ts(row["scored_at"]),
                "uploaded_at": _fmt_ts(row["uploaded_at"]),
                "ratings": ratings,
            })
    finally:
        conn.close()

    return TEMPLATES.TemplateResponse("list.html", {
        "request": request,
        "observations": observations,
        "rubrics": list(RUBRICS.values()),
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "today": datetime.now(timezone.utc).date().isoformat(),
    })


@app.post("/upload")
async def upload_observation(
    request: Request,
    video: UploadFile = File(...),
    teacher_name: str = Form(...),
    rubric_id: str = Form(DEFAULT_RUBRIC_ID),
    whisper_model: str = Form("medium"),
    lesson_plan_id: Optional[str] = Form(None),
    observed_at: Optional[str] = Form(None),
) -> RedirectResponse:
    """Accept a video upload, create the observation row, kick off the background job."""
    # Coach-only: uploading a video kicks off a paid AI scoring job and
    # creates an observation record. Teacher-role must never trigger this
    # (even if URL-known); besides cost, an attacker-controlled teacher_name
    # would create/attach observations to arbitrary teachers via the
    # get_or_create_teacher name match.
    _require_coach(_current_viewer(request), "Video upload")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(400, "ANTHROPIC_API_KEY is not set. Export it and restart the server.")

    if rubric_id not in RUBRICS:
        raise HTTPException(400, f"Unknown rubric: {rubric_id!r}")

    # Validate observed_at: must parse as YYYY-MM-DD and cannot be in the
    # future. Blocks the class of typo where the coach types next year's date
    # (e.g. 2027 instead of 2026) — a silent date-of-record error later hides
    # observations behind future cadence windows and skews compliance banding.
    if observed_at:
        try:
            _obs_dt = datetime.strptime(observed_at, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise HTTPException(400, f"observed_at must be YYYY-MM-DD, got: {observed_at!r}")
        _today = datetime.now(timezone.utc).date()
        if _obs_dt > _today:
            raise HTTPException(
                400,
                f"observed_at ({observed_at}) is in the future. "
                f"Observations must be dated on or before today ({_today.isoformat()}).",
            )

    # Resolve teacher + run every gate BEFORE writing the video to disk.
    # A 300 MB upload that gets refused for consent or cross-coach conflict
    # would otherwise leave orphaned bytes under app/uploads/ that no
    # observation row references — the consent policy would be violated on
    # disk even if the DB row was blocked.
    viewer = _current_viewer(request)
    _coach_uid = _viewer_uid(viewer)
    _org_id = viewer.get("org_id") or _SEEDED_IDS["org_id"]
    conn = db_connect(DB_PATH)
    try:
        from pipeline.db import ArchivedTeacherError
        # Cross-coach conflict: if the teacher-name already resolves to a
        # teacher assigned to a DIFFERENT coach in this org, refuse rather
        # than silently attaching an observation to another coach's teacher.
        # (The coach can rename their upload's teacher_name to disambiguate.)
        existing = conn.execute(
            """SELECT id, assigned_coach_user_id FROM teachers
               WHERE org_id = ? AND name = ? AND archived_at IS NULL""",
            (_org_id, teacher_name.strip()),
        ).fetchone()
        if existing and existing["assigned_coach_user_id"] and existing["assigned_coach_user_id"] != _coach_uid:
            raise HTTPException(
                409,
                f"A teacher named {teacher_name.strip()!r} is already on another coach's caseload. "
                f"Ask them to hand the record off, or upload under a distinguishable name."
            )
        try:
            teacher_id = get_or_create_teacher(
                conn, org_id=_org_id, name=teacher_name.strip(),
                coach_user_id=_coach_uid,
            )
        except ArchivedTeacherError as e:
            raise HTTPException(400, str(e))
        # Consent gate: refuse the upload if the teacher hasn't consented.
        # We don't purge data on revoke, but no NEW recording lands without
        # active consent — matches the org-level, revocable policy we
        # agreed on. The teacher's own /consent page grants or revokes.
        _consent = get_active_consent(conn, teacher_id=teacher_id)
        if not _consent:
            raise HTTPException(
                403,
                f"{teacher_name.strip()} hasn't consented to being recorded yet. "
                f"Send them the sign-in link so they can decide before you upload.",
            )
    except HTTPException:
        conn.close()
        raise
    # Only now that every guard has cleared do we spend the disk / bandwidth
    # to persist the video. If anything downstream fails we clean up the file
    # to keep app/uploads/ consistent with the observations table.
    obs_id = str(uuid.uuid4())
    safe_name = video.filename or "upload.mov"
    dest_dir = UPLOADS_DIR / obs_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / safe_name
    try:
        with dest_path.open("wb") as f:
            while chunk := await video.read(1024 * 1024):
                f.write(chunk)
    except Exception:
        # Whatever failed mid-write, don't leave a partial file behind.
        try:
            if dest_path.exists():
                dest_path.unlink()
            dest_dir.rmdir()
        except OSError:
            pass
        conn.close()
        raise
    try:
        rubric_db_id = get_or_create_rubric_from_id(conn, org_id=None, rubric_id_kind=rubric_id)
        # Auto-attach to the cycle that was live when the observation actually
        # happened. Falls back to the currently-active cycle if no date-match
        # exists (e.g., pre-cycle observations or missing observed_at).
        obs_date_for_cycle = observed_at or datetime.now(timezone.utc).date().isoformat()
        date_matched = conn.execute(
            """SELECT id FROM coaching_cycles
               WHERE teacher_id = ?
                 AND opened_at <= ?
                 AND (closed_at IS NULL OR closed_at >= ?)
               ORDER BY opened_at DESC LIMIT 1""",
            (teacher_id, obs_date_for_cycle + "T23:59:59+00:00", obs_date_for_cycle + "T00:00:00+00:00"),
        ).fetchone()
        if date_matched:
            auto_cycle_id = date_matched["id"]
        else:
            active_cycle = conn.execute(
                "SELECT id FROM coaching_cycles WHERE teacher_id = ? AND closed_at IS NULL LIMIT 1",
                (teacher_id,),
            ).fetchone()
            auto_cycle_id = active_cycle["id"] if active_cycle else None
        _now_iso_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # observed_at: coach may enter the actual observation date (default today)
        # so compliance and cadence math reflect when the lesson happened, not when
        # the video was uploaded.
        _obs_at = None
        _obs_date_str = None
        if observed_at:
            try:
                _obs_at = f"{observed_at}T12:00:00+00:00"
                _obs_date_str = observed_at  # YYYY-MM-DD
            except Exception:
                _obs_at = None
        if _obs_date_str is None:
            _obs_date_str = datetime.now(timezone.utc).date().isoformat()

        # Auto-attach a lesson plan by date match if none explicitly provided.
        # Match rule: exactly one non-archived plan whose date range covers the
        # observation date. Coach can change/detach on the observation page.
        if not lesson_plan_id:
            matches = conn.execute(
                """SELECT id FROM lesson_plans
                   WHERE teacher_id = ? AND status != 'archived'
                     AND plan_start_date IS NOT NULL
                     AND plan_start_date <= ?
                     AND (plan_end_date IS NULL OR plan_end_date >= ?)""",
                (teacher_id, _obs_date_str, _obs_date_str),
            ).fetchall()
            if len(matches) == 1:
                lesson_plan_id = matches[0]["id"]
        conn.execute(
            """INSERT INTO observations
                   (id, org_id, teacher_id, observer_user_id, rubric_id, coaching_cycle_id,
                    lesson_plan_id,
                    video_ref, video_filename, video_duration_s,
                    status, uploaded_at, observed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending', ?, ?)""",
            (obs_id, _org_id, teacher_id, _coach_uid, rubric_db_id,
             auto_cycle_id, (lesson_plan_id or None),
             str(dest_path), safe_name, _now_iso_ts, _obs_at),
        )
        conn.commit()
    except Exception:
        # DB write failed after the video landed on disk. Clean up so the
        # file store stays consistent with the observations table.
        try:
            if dest_path.exists():
                dest_path.unlink()
            dest_dir.rmdir()
        except OSError:
            pass
        raise
    finally:
        conn.close()

    submit_job(
        db_path=DB_PATH,
        observation_id=obs_id,
        video_path=dest_path,
        rubric_id=rubric_id,
        whisper_model=whisper_model,
    )

    return RedirectResponse(url=f"/observations/{obs_id}", status_code=303)


@app.get("/observations/{observation_id}", response_class=HTMLResponse)
def observation_detail(request: Request, observation_id: str) -> HTMLResponse:
    conn = db_connect(DB_PATH)
    try:
        obs = conn.execute(
            """SELECT o.*, t.name AS teacher_name, r.name AS rubric_name, r.kind AS rubric_kind
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               JOIN rubrics  r ON r.id = o.rubric_id
               WHERE o.id = ?""",
            (observation_id,),
        ).fetchone()
        if not obs:
            raise HTTPException(404, "Observation not found")
        # Teacher-role viewers may only see their own observations. Denied
        # AFTER the existence check so a random obs id gets 404, another
        # teacher's obs gets 403 — same information-leak posture as the
        # cycle gate. Coach / principal / district see all.
        viewer = _current_viewer(request)
        if viewer["role"] == "teacher" and viewer.get("teacher_id") != obs["teacher_id"]:
            raise HTTPException(403, "Not authorized to view this observation")

        report_row = conn.execute(
            """SELECT * FROM report_versions
               WHERE observation_id = ?
               ORDER BY version_number DESC
               LIMIT 1""",
            (observation_id,),
        ).fetchone()
    finally:
        conn.close()

    report_html = None
    ratings = {}
    coaching_recommendations = []
    domain_assessments = []
    highest_leverage_move = None
    prior_action_assessments = []
    if report_row:
        rubric = get_rubric(obs["rubric_kind"])
        domain_assessments = json.loads(report_row["domain_assessments"])
        # coaching_recommendations JSON can be one of two shapes:
        #   legacy: list of recommendations
        #   Phase B: {"recommendations": [...], "highest_leverage_move": {...}, "prior_action_assessments": [...]}
        raw = json.loads(report_row["coaching_recommendations"])
        if isinstance(raw, list):
            coaching_recommendations = raw
        else:
            coaching_recommendations = raw.get("recommendations", [])
            highest_leverage_move = raw.get("highest_leverage_move")
            prior_action_assessments = raw.get("prior_action_assessments", []) or []
        ratings = {da["domain"]: da["overall_rating"] for da in domain_assessments}
        # Render the report as markdown-derived HTML.
        markdown_source = _compose_report_markdown(
            report_row, domain_assessments, coaching_recommendations, obs, rubric,
            highest_leverage_move=highest_leverage_move,
            prior_action_assessments=prior_action_assessments,
        )
        # Render markdown → HTML, then sanitize through bleach before the
        # template's `|safe` unlocks it. python-markdown passes raw HTML
        # tags through untouched; without _sanitize_report_html a lesson-
        # plan PDF whose extracted text prompt-injects a `<script>` payload
        # into the AI's overall_summary would fire on every open of this
        # observation. (Same story for a teacher whose CSV `name` contained
        # an HTML tag — it lands in the report's H1.)
        report_html = _sanitize_report_html(
            md_lib.markdown(
                markdown_source,
                extensions=["extra", "sane_lists"],
            )
        )

    # Bite-sized action tracking — three buckets:
    #   issued: actions recommended in THIS obs (outbound to next cycle)
    #   available: prior actions awaiting assessment (assessable against THIS obs)
    #   assessed: actions that were already assessed (AI or coach) against THIS obs
    conn = db_connect(DB_PATH)
    try:
        actions_issued = list_actions_issued_at(conn, observation_id)
        actions_available = list_actions_available_for_assessment_at(conn, observation_id)
        actions_assessed = list_actions_assessed_at(conn, observation_id)
        # Coaching cycles this observation could attach to (all cycles for this teacher).
        teacher_cycles = list_cycles_for_teacher(conn, obs["teacher_id"])
        current_cycle = None
        if obs["coaching_cycle_id"]:
            current_cycle = next((c for c in teacher_cycles if c["id"] == obs["coaching_cycle_id"]), None)
        # Active goals for the goal-picker chip on unlinked actions. When the
        # auto-link at record time was ambiguous (2+ goals on the same domain)
        # or absent (no matching active goal), the action lands with goal_id
        # NULL and the coach must pick. Ordered by proposed_at DESC (newest
        # first) since the newest active goal is the most likely intended tie.
        teacher_active_goals = list_goals_for_teacher(conn, obs["teacher_id"], active_only=True)
    finally:
        conn.close()

    _av_idx = sum(ord(c) for c in (obs["teacher_name"] or "")) % 6
    _mirror_sketch = request.query_params.get("mirror") == "on"
    # Real teacher response to the coach's move (if any) + coach's current published move.
    conn2 = db_connect(DB_PATH)
    try:
        _hlm_resp = get_hlm_response(conn2, observation_id)
        _coach_move = get_current_coach_move(conn2, observation_id)
        _coach_move_history = list_coach_move_history(conn2, observation_id)
    finally:
        conn2.close()

    # Lesson plan attachment: the plan this observation covers (if any) +
    # a picklist of the teacher's recent plans so coach can pick/change.
    conn_lp = db_connect(DB_PATH)
    try:
        _attached_lp = None
        if obs.get("lesson_plan_id") if hasattr(obs, "get") else obs["lesson_plan_id"]:
            _lpid = obs["lesson_plan_id"]
            _attached_lp = conn_lp.execute(
                "SELECT id, title, current_version, plan_start_date, plan_end_date, status FROM lesson_plans WHERE id = ?",
                (_lpid,),
            ).fetchone()
            _attached_lp = dict(_attached_lp) if _attached_lp else None
        _teacher_plans = [dict(r) for r in conn_lp.execute(
            """SELECT id, title, current_version, plan_start_date, plan_end_date, status
               FROM lesson_plans
               WHERE teacher_id = ? AND status != 'archived'
               ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 8""",
            (obs["teacher_id"],),
        ).fetchall()]
    finally:
        conn_lp.close()

    # A teacher's response is "stale" when the coach has republished a newer
    # version after the response was given — the teacher hasn't reacted to
    # the coach's update yet.
    _response_stale = bool(
        _hlm_resp and _coach_move
        and _hlm_resp.get("published_coach_move_id")
        and _hlm_resp["published_coach_move_id"] != _coach_move["id"]
    )

    # Mode = review (coach reads AI, drafts move) OR debrief (coach with teacher,
    # narrative: observed -> bridge -> next). Auto-detect from state; override
    # via ?mode= if the coach wants the other layout.
    _mode = request.query_params.get("mode")
    if _mode not in ("review", "debrief"):
        if obs["status"] == "complete" and _coach_move:
            _mode = "debrief"
        else:
            _mode = "review"

    return TEMPLATES.TemplateResponse("detail.html", {
        "request": request,
        "avatar_idx": _av_idx,
        "mode": _mode,
        "mirror_sketch": _mirror_sketch,
        "hlm_response": _hlm_resp,
        "response_is_stale": _response_stale,
        "coach_move": _coach_move,
        "coach_move_history": _coach_move_history,
        "attached_lp": _attached_lp,
        "teacher_plans": _teacher_plans,
        "attached_cycle_closed": bool(current_cycle and current_cycle.get("closed_at") if hasattr(current_cycle, "get") else (current_cycle and current_cycle["closed_at"])),
        "obs": {
            "id": obs["id"],
            "teacher": obs["teacher_name"],
            "teacher_id": obs["teacher_id"],
            "rubric_name": obs["rubric_name"],
            "video_filename": obs["video_filename"],
            "duration": _fmt_duration(obs["video_duration_s"]),
            "status": obs["status"],
            "failure_reason": obs["failure_reason"],
            "uploaded_at": _fmt_ts(obs["uploaded_at"]),
            "scored_at": _fmt_ts(obs["scored_at"]),
            "debrief_focus": obs["debrief_focus"],
            "debrief_focus_gbf_id": obs["debrief_focus_gbf_id"],
            # Archive state — templates render a restore banner + hide the
            # archive control when the observation is already archived.
            "deleted_at": _fmt_ts(obs["deleted_at"]) if obs["deleted_at"] else None,
        },
        "ratings": ratings,
        "domain_assessments": domain_assessments,
        "coaching_recommendations": coaching_recommendations,
        "highest_leverage_move": highest_leverage_move,
        "prior_action_assessments": prior_action_assessments,
        "report_html": report_html,
        "actions_issued": actions_issued,
        "actions_available": actions_available,
        "actions_assessed": actions_assessed,
        "teacher_active_goals": teacher_active_goals,
        "teacher_cycles": teacher_cycles,
        "current_cycle": current_cycle,
        "video_url": _video_url_for(obs["id"], obs["video_ref"]),
        "gbf_steps": GBF_STEPS,
        "gbf_by_id": GBF_STEPS_BY_ID,
    })


# ---------------------------------------------------------------------------
# Lesson plans (teacher uploads, coach reviews recursively)
# ---------------------------------------------------------------------------


LESSON_PLANS_DIR = APP_ROOT / "lesson_plans"


@app.get("/teachers/{teacher_id}/lesson-plans", response_class=HTMLResponse)
def teacher_lesson_plans_list(request: Request, teacher_id: str) -> HTMLResponse:
    # Teacher-role: only their own plans.
    viewer = _current_viewer(request)
    if viewer["role"] == "teacher" and viewer.get("teacher_id") != teacher_id:
        raise HTTPException(403, "Not authorized to view this teacher's plans")
    conn = db_connect(DB_PATH)
    try:
        teacher = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not teacher:
            raise HTTPException(404)
        plans = list_lesson_plans_for_teacher(conn, teacher_id)
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("lesson_plans_list.html", {
        "request": request,
        "teacher": {"id": teacher["id"], "name": teacher["name"]},
        "plans": plans,
    })


@app.post("/teachers/{teacher_id}/lesson-plans")
async def create_lesson_plan_route(
    request: Request,
    teacher_id: str,
    title: str = Form(...),
    plan_start_date: Optional[str] = Form(None),
    plan_end_date: Optional[str] = Form(None),
    document: UploadFile = File(...),
) -> RedirectResponse:
    # Teacher submits their own plans; coach can upload on their behalf.
    viewer = _guard_teacher_write(request, teacher_id, "Lesson plan upload")
    import uuid as _uuid
    plan_id = str(_uuid.uuid4())
    dest_dir = LESSON_PLANS_DIR / plan_id / "v1"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (document.filename or "plan.pdf")
    with dest_path.open("wb") as f:
        while chunk := await document.read(1024 * 1024):
            f.write(chunk)
    from pipeline.text_extract import extract_text
    extracted = extract_text(dest_path)

    conn = db_connect(DB_PATH)
    try:
        real_id = create_lesson_plan(
            conn,
            org_id=viewer.get("org_id") or _SEEDED_IDS["org_id"],
            teacher_id=teacher_id,
            title=title.strip(),
            plan_start_date=plan_start_date or None,
            plan_end_date=plan_end_date or None,
            created_by_user_id=_viewer_uid(viewer),
            file_ref=str(dest_path),
            original_filename=document.filename or "plan.pdf",
            extracted_text=extracted,
        )
    finally:
        conn.close()
    # We generated our own plan_id but create_lesson_plan generates its own.
    return RedirectResponse(url=f"/lesson-plans/{real_id}", status_code=303)


@app.get("/lesson-plans/{plan_id}", response_class=HTMLResponse)
def lesson_plan_detail(request: Request, plan_id: str) -> HTMLResponse:
    conn = db_connect(DB_PATH)
    try:
        plan = get_lesson_plan(conn, plan_id)
        if not plan:
            raise HTTPException(404)
        # Teacher-role: only their own plans.
        viewer = _current_viewer(request)
        if viewer["role"] == "teacher" and viewer.get("teacher_id") != plan.get("teacher_id"):
            raise HTTPException(403, "Not authorized to view this plan")
        versions = list_lesson_plan_versions(conn, plan_id)
        comments = list_lesson_plan_comments(conn, plan_id)
    finally:
        conn.close()
    _av_idx = sum(ord(c) for c in (plan.get("teacher_name") or "")) % 6
    return TEMPLATES.TemplateResponse("lesson_plan_detail.html", {
        "request": request,
        "plan": plan,
        "avatar_idx": _av_idx,
        "versions": versions,
        "comments": comments,
    })


@app.post("/lesson-plans/{plan_id}/versions")
async def upload_lesson_plan_version(
    request: Request,
    plan_id: str,
    document: UploadFile = File(...),
) -> RedirectResponse:
    """Upload a revised version of an existing plan. Owner or coach only."""
    viewer = _guard_plan_write(request, plan_id, "Lesson plan revision")
    conn = db_connect(DB_PATH)
    try:
        plan = get_lesson_plan(conn, plan_id)
        if not plan:
            raise HTTPException(404)
        next_ver = plan["current_version"] + 1
    finally:
        conn.close()
    dest_dir = LESSON_PLANS_DIR / plan_id / f"v{next_ver}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (document.filename or "plan.pdf")
    with dest_path.open("wb") as f:
        while chunk := await document.read(1024 * 1024):
            f.write(chunk)
    from pipeline.text_extract import extract_text
    extracted = extract_text(dest_path)

    conn = db_connect(DB_PATH)
    try:
        add_lesson_plan_version(
            conn,
            lesson_plan_id=plan_id,
            file_ref=str(dest_path),
            original_filename=document.filename or "plan.pdf",
            uploaded_by_user_id=_viewer_uid(viewer),
            extracted_text=extracted,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/lesson-plans/{plan_id}", status_code=303)


@app.post("/lesson-plans/{plan_id}/comments")
def post_lesson_plan_comment(
    request: Request,
    plan_id: str,
    body: str = Form(...),
    plan_version_number: int = Form(...),
) -> RedirectResponse:
    """Post a comment on a lesson plan. Owner (teacher) or coach only.
    ``author_role`` is derived from the viewer — never trusted from the form —
    so a teacher-role writer can't plant a comment labelled 'coach' or vice
    versa.
    """
    if not body.strip():
        raise HTTPException(400, "Comment body required")
    viewer = _guard_plan_write(request, plan_id, "Lesson plan comment")
    conn = db_connect(DB_PATH)
    try:
        plan = get_lesson_plan(conn, plan_id)
        if not plan:
            raise HTTPException(404)
        # Derive author_role from the viewer; ignore any form-supplied value.
        author_role = "teacher" if viewer["role"] == "teacher" else "coach"
        add_lesson_plan_comment(
            conn,
            lesson_plan_id=plan_id,
            plan_version_number=plan_version_number,
            author_user_id=_viewer_uid(viewer),
            author_role=author_role,
            body=body.strip(),
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/lesson-plans/{plan_id}#comments", status_code=303)


# Lesson-plan lifecycle statuses the coach can set. Anything outside this
# set is refused — belt-and-suspenders against a form submitting a nonsense
# status that would corrupt filter queries.
_LESSON_PLAN_STATUSES = {"submitted", "coach_reviewed", "approved", "archived"}


@app.post("/lesson-plans/{plan_id}/status")
def set_lesson_plan_status_route(
    request: Request,
    plan_id: str,
    status: str = Form(...),
) -> RedirectResponse:
    """Coach-only: status transitions are the coach's read on the plan."""
    _require_coach(_current_viewer(request), "Lesson plan status")
    _guard_plan_write(request, plan_id, "Lesson plan status")
    if status not in _LESSON_PLAN_STATUSES:
        raise HTTPException(400, f"Unknown lesson plan status: {status!r}")
    conn = db_connect(DB_PATH)
    try:
        update_lesson_plan_status(conn, plan_id, status)
    finally:
        conn.close()
    return RedirectResponse(url=f"/lesson-plans/{plan_id}", status_code=303)


@app.get("/lesson-plans/{plan_id}/file/v{version}")
def lesson_plan_file(request: Request, plan_id: str, version: int):
    """Serve the plan's uploaded file for a given version."""
    from fastapi.responses import FileResponse
    conn = db_connect(DB_PATH)
    try:
        # Teacher-role: only their own plans' files.
        viewer = _current_viewer(request)
        if viewer["role"] == "teacher":
            plan = get_lesson_plan(conn, plan_id)
            if not plan or viewer.get("teacher_id") != plan.get("teacher_id"):
                raise HTTPException(403, "Not authorized to download this plan")
        row = conn.execute(
            "SELECT file_ref, original_filename FROM lesson_plan_versions "
            "WHERE lesson_plan_id = ? AND version_number = ?",
            (plan_id, version),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)
    return FileResponse(row["file_ref"], filename=row["original_filename"])


@app.get("/lesson-plans", response_class=HTMLResponse)
def all_lesson_plans(request: Request) -> HTMLResponse:
    """Admin view — all plans across all teachers."""
    _deny_teacher(_current_viewer(request), "All-plans admin view")
    conn = db_connect(DB_PATH)
    try:
        plans = list_lesson_plans_for_org(conn, _SEEDED_IDS["org_id"])
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("lesson_plans_all.html", {
        "request": request,
        "plans": plans,
    })


@app.post("/observations/{observation_id}/debrief-focus")
def set_debrief_focus_route(
    request: Request,
    observation_id: str,
    debrief_focus: Optional[str] = Form(None),
    debrief_focus_gbf_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach names the specific move to be practiced during the debrief.
    Coach-only.
    """
    _require_coach(_current_viewer(request), "Debrief focus edit")
    _guard_obs_write(request, observation_id, "Debrief focus edit")
    if debrief_focus_gbf_id and debrief_focus_gbf_id not in GBF_STEPS_BY_ID:
        debrief_focus_gbf_id = None
    conn = db_connect(DB_PATH)
    try:
        set_observation_debrief_focus(
            conn,
            observation_id=observation_id,
            debrief_focus=(debrief_focus or None),
            debrief_focus_gbf_id=(debrief_focus_gbf_id or None),
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}#debrief-focus", status_code=303)


@app.get("/uploads/{observation_id}/{filename}")
def serve_video_file(request: Request, observation_id: str, filename: str):
    """Gated video-file serving. Teacher-role may only fetch their own
    observation's video; coach/principal/district see all. Same rule as the
    observation detail page. Replaces the earlier unauthenticated static
    mount at /uploads. Path traversal is guarded by resolving the file
    against UPLOADS_DIR and refusing anything outside it.
    """
    from fastapi.responses import FileResponse
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT teacher_id, video_ref FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)
    viewer = _current_viewer(request)
    if viewer["role"] == "teacher" and viewer.get("teacher_id") != row["teacher_id"]:
        raise HTTPException(403, "Not authorized to fetch this video")
    if not row["video_ref"]:
        raise HTTPException(404)
    file_path = Path(row["video_ref"]).resolve()
    try:
        file_path.relative_to(UPLOADS_DIR.resolve())
    except ValueError:
        # Baseline observations point to reports/... outside UPLOADS_DIR.
        # These have no browser-facing URL and should never be reached here.
        raise HTTPException(404)
    if file_path.name != filename:
        # URL claims a different filename than the DB record — reject.
        raise HTTPException(404)
    if not file_path.exists():
        raise HTTPException(404)
    return FileResponse(str(file_path), filename=filename)


def _video_url_for(observation_id: str, video_ref: Optional[str]) -> Optional[str]:
    """Return a URL under /uploads/... if the video file lives in the uploads
    directory. Baseline observations (imported from reports/) return None,
    which the template renders as an 'not available' note.
    """
    if not video_ref:
        return None
    ref_path = Path(video_ref)
    try:
        rel = ref_path.resolve().relative_to(UPLOADS_DIR.resolve())
    except (ValueError, FileNotFoundError):
        return None
    return f"/uploads/{rel.as_posix()}"


@app.post("/observations/{observation_id}/assess-action")
def assess_action_route(
    request: Request,
    observation_id: str,
    tracking_id: str = Form(...),
    implementation: str = Form(...),
    evidence_notes: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach records (or overrides) the implementation assessment for a prior
    bite-sized action, against this observation. Coach-only — implementation
    ratings feed impact rollups and the AI's next-context prompt; letting a
    teacher self-assess would break the coach-mediates model.
    """
    _require_coach(_current_viewer(request), "Action assessment")
    viewer = _guard_obs_write(request, observation_id, "Action assessment")
    if implementation not in ("not_observed", "partial", "full", "regressed"):
        raise HTTPException(400, f"Invalid implementation value: {implementation!r}")
    conn = db_connect(DB_PATH)
    try:
        # Cross-teacher guard: the tracking row must belong to the same
        # teacher as the followup observation. Otherwise a stale form (or a
        # coach who copied the wrong id) would silently wire one teacher's
        # action to another's obs, breaking the impact rollup.
        obs_tid = _obs_teacher_id(conn, observation_id)
        action_row = conn.execute(
            "SELECT teacher_id FROM bite_sized_action_tracking WHERE id = ?",
            (tracking_id,),
        ).fetchone()
        if not action_row or not obs_tid or action_row["teacher_id"] != obs_tid:
            raise HTTPException(400, "Action does not belong to this observation's teacher.")
        assess_bite_sized_action(
            conn,
            tracking_id=tracking_id,
            followup_observation_id=observation_id,
            implementation=implementation,
            evidence_notes=(evidence_notes or "").strip() or None,
            assessed_by_user_id=_viewer_uid(viewer),
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}#actions", status_code=303)


@app.get("/observations/{observation_id}/status")
def observation_status(request: Request, observation_id: str) -> JSONResponse:
    """JSON poll endpoint the detail page uses to auto-refresh while processing.
    Same teacher-role gating as the detail page: only see your own obs status.
    failure_reason can contain filesystem paths / model names, so a leaking
    poll endpoint would tell a teacher more about the pipeline than intended.
    """
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT teacher_id, status, failure_reason FROM observations WHERE id = ?",
            (observation_id,),
        ).fetchone()
        if row:
            viewer = _current_viewer(request)
            if viewer["role"] == "teacher" and viewer.get("teacher_id") != row["teacher_id"]:
                raise HTTPException(403, "Not authorized to poll this observation")
    finally:
        conn.close()
    if not row:
        raise HTTPException(404)
    return JSONResponse({"status": row["status"], "failure_reason": row["failure_reason"]})


# ---------------------------------------------------------------------------
# Report markdown composition (subset of observe._compose_report_markdown,
# adapted for reading from dicts instead of Pydantic objects)
# ---------------------------------------------------------------------------


def _compose_report_markdown(
    report_row, domain_assessments, coaching_recommendations, obs, rubric,
    highest_leverage_move=None, prior_action_assessments=None,
) -> str:
    prior_action_assessments = prior_action_assessments or []
    out = []
    out.append(f"# {obs['teacher_name']} — Observation Report")
    out.append(f"*Scored against: {rubric.name}*")
    out.append("")
    out.append(report_row["opening_paragraph"].strip())
    out.append("")

    # Highest-leverage move — featured at the top when available (Phase B).
    if highest_leverage_move:
        m = highest_leverage_move
        out.append("## ⚡ Highest-leverage coaching move this cycle")
        out.append(f"**Do this:** {m.get('move', '').strip()}")
        out.append("")
        out.append(f"**Related Core Teacher Skill:** {m.get('related_core_teacher_skill', '')}  ")
        out.append(f"**Domain:** {m.get('related_domain', '')}")
        out.append("")
        if m.get("rationale"):
            out.append(f"**Why this move now:** {m['rationale'].strip()}")
            out.append("")
        cf = m.get("contextual_factors") or []
        if cf:
            out.append("**Context factors that shaped this recommendation:**")
            out.append("")
            for c in cf:
                out.append(f"- {c}")
            out.append("")
        signals = m.get("success_signals_next_visit") or []
        if signals:
            out.append("**Success signals next visit:**")
            out.append("")
            for s in signals:
                out.append(f"- {s}")
            out.append("")
        out.append("---")
        out.append("")

    # Prior-action assessment — surface what the AI concluded about last cycle's follow-through.
    if prior_action_assessments:
        out.append("## Prior action follow-through")
        out.append("Assessment of bite-sized actions from prior observations.")
        out.append("")
        for pa in prior_action_assessments:
            status = pa.get("implementation", "?")
            skill = pa.get("core_teacher_skill", "")
            out.append(f"- **{skill}** — *{status}*")
            if pa.get("evidence_notes"):
                out.append(f"  - {pa['evidence_notes'].strip()}")
        out.append("")
        out.append("---")
        out.append("")

    out.append("## Overall")
    out.append(report_row["overall_summary"].strip())
    out.append("")

    for domain in rubric.domains:
        da = next((x for x in domain_assessments if x["domain"] == domain), None)
        if not da:
            continue
        score = rubric.score_for(da["overall_rating"])
        out.append("---")
        out.append("")
        out.append(f"## {domain} — {da['overall_rating']} ({score}/{rubric.num_levels})")
        out.append(f"**Essential Question:** {da['essential_question'].strip()}")
        out.append("")
        out.append(f"**Rubric descriptor at {da['overall_rating']}:**")
        out.append(f"> {da['rubric_descriptor_text'].strip()}")
        out.append("")
        out.append("**What was observed:**")
        out.append(da["what_was_observed"].strip())
        out.append("")
        out.append(f"**Distance from target:** {da['distance_from_target'].strip()}")
        out.append("")
        skills = da.get("relevant_core_teacher_skills") or []
        if skills:
            out.append("**Coaching-construct entries implicated:**")
            out.append("")  # blank line so the following bullets parse as a list
            for skill in skills:
                out.append(f"- {skill}")
            out.append("")
        out.append(f"*Preponderance breakdown:* {da['preponderance_summary'].strip()}")
        out.append("")

    if coaching_recommendations:
        out.append("---")
        out.append("")
        out.append("## Coaching Priorities")
        out.append("")
        for i, rec in enumerate(coaching_recommendations, 1):
            out.append(f"### Priority {i}: {rec['core_teacher_skill']}")
            out.append(f"**Domain:** {rec['related_domain']}")
            out.append("")
            out.append(f"**Why:** {rec['rationale'].strip()}")
            out.append("")
            out.append(f"**Bite-sized action this week:** {rec['bite_sized_action'].strip()}")
            out.append("")
            indicators = rec.get("success_indicators") or []
            if indicators:
                out.append("**Success indicators next visit:**")
                out.append("")
                for ind in indicators:
                    out.append(f"- {ind}")
                out.append("")

    return "\n".join(out)


# ---------------------------------------------------------------------------
# Teacher-centric routes (Phase A relational-context UI)
#
# For the local single-user app, the seeded user has role='coach', so all
# private-notes calls pass caller_role='coach'. When real auth lands this
# comes from the session; the DB helpers already enforce the visibility gate.
# ---------------------------------------------------------------------------


@app.get("/teachers", response_class=HTMLResponse)
def teachers_list(request: Request, show_archived: int = 0) -> HTMLResponse:
    """Roster view — one row per teacher with a compact trend indicator per domain.

    ``show_archived=1`` swaps the roster from "active teachers" to "archived
    teachers", so the coach can find and restore one. The two modes render
    the same template with different rows.
    """
    viewer = _current_viewer(request)
    _deny_teacher(viewer, "Roster")
    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    scope_sql, scope_params = _scope_filter(viewer, teachers_alias="t")
    _scope_and = (" AND " + scope_sql) if scope_sql else ""
    conn = db_connect(DB_PATH)
    try:
        if show_archived:
            teachers = conn.execute(
                f"""SELECT t.id, t.name, t.archived_at,
                          (SELECT COUNT(*) FROM observations o
                           WHERE o.teacher_id = t.id AND o.status = 'complete'
                                 AND o.deleted_at IS NULL) AS obs_count,
                          (SELECT MAX(o.scored_at) FROM observations o
                           WHERE o.teacher_id = t.id AND o.status = 'complete'
                                 AND o.deleted_at IS NULL) AS last_scored
                   FROM teachers t
                   WHERE t.archived_at IS NOT NULL{_scope_and}
                   ORDER BY t.archived_at DESC""",
                scope_params,
            ).fetchall()
        else:
            teachers = conn.execute(
                f"""SELECT t.id, t.name, t.archived_at,
                          (SELECT COUNT(*) FROM observations o
                           WHERE o.teacher_id = t.id AND o.status = 'complete'
                                 AND o.deleted_at IS NULL) AS obs_count,
                          (SELECT MAX(o.scored_at) FROM observations o
                           WHERE o.teacher_id = t.id AND o.status = 'complete'
                                 AND o.deleted_at IS NULL) AS last_scored
                   FROM teachers t
                   WHERE t.archived_at IS NULL{_scope_and}
                   ORDER BY t.name""",
                scope_params,
            ).fetchall()

        rows = []
        for t in teachers:
            movement = rubric_score_movement_for_teacher(conn, t["id"])
            # Per-domain latest-vs-first trend arrow.
            latest_ratings = movement[-1]["ratings"] if movement else {}
            first_ratings = movement[0]["ratings"] if movement else {}
            trend_by_domain = {}
            for domain in rubric.domains:
                latest = latest_ratings.get(domain)
                first = first_ratings.get(domain)
                if not latest:
                    trend_by_domain[domain] = {"rating": None, "trend": "none"}
                    continue
                if len(movement) < 2 or not first or first == latest:
                    trend = "flat"
                else:
                    trend = "up" if rubric.score_for(latest) > rubric.score_for(first) else "down"
                trend_by_domain[domain] = {"rating": latest, "trend": trend}

            open_goals = list_goals_for_teacher(conn, t["id"], active_only=True)
            open_actions = list_open_action_tracking_for_teacher(conn, t["id"])
            rows.append({
                "id": t["id"],
                "name": t["name"],
                "archived_at": t["archived_at"],
                "obs_count": t["obs_count"],
                "last_scored": _fmt_ts(t["last_scored"]),
                "trend_by_domain": trend_by_domain,
                "active_goal_count": len(open_goals),
                "open_action_count": len(open_actions),
            })

        archived_count = conn.execute(
            f"SELECT COUNT(*) AS c FROM teachers t WHERE t.archived_at IS NOT NULL{_scope_and}",
            scope_params,
        ).fetchone()["c"]
    finally:
        conn.close()

    return TEMPLATES.TemplateResponse("teachers_list.html", {
        "request": request,
        "teachers": rows,
        "domains": rubric.domains,
        "rubric": rubric,
        "show_archived": bool(show_archived),
        "archived_count": archived_count,
    })


@app.get("/teachers/{teacher_id}", response_class=HTMLResponse)
def teacher_detail(request: Request, teacher_id: str) -> HTMLResponse:
    """Teacher hub: profile (both sides), active goals, observation history with
    trend, private notes, open action tracking. This is the primary product surface.
    """
    # Role gate: this is the coach's page. Teacher-role viewers get redirected
    # to their own teacher-view (never to another teacher's URL — the redirect
    # target is *their* teacher_id, not the URL's). This prevents URL-tampering
    # from one teacher to another AND from surfacing coach-only content
    # (private notes, coach ratings context) on the teacher role.
    viewer = _current_viewer(request)
    if viewer["role"] == "teacher":
        own_id = viewer.get("teacher_id")
        if not own_id:
            raise HTTPException(403, "Teacher role missing teacher_id")
        return RedirectResponse(url=f"/teachers/{own_id}/teacher-view", status_code=303)
    # Coach must own this teacher; principal must be in the same org.
    # Deferred until AFTER we know the teacher exists so unknown ids get 404,
    # cross-caseload ids get 403 (matches the observation gate's info leak posture).
    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    conn = db_connect(DB_PATH)
    try:
        teacher = conn.execute(
            "SELECT id, name, archived_at FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not teacher:
            raise HTTPException(404, "Teacher not found")
        _require_teacher_access(viewer, teacher_id, "Teacher hub")

        profile = get_teacher_profile(conn, teacher_id)
        active_goals = list_goals_for_teacher(conn, teacher_id, active_only=True)
        closed_goals = [g for g in list_goals_for_teacher(conn, teacher_id) if g not in active_goals]
        movement = rubric_score_movement_for_teacher(conn, teacher_id)
        open_actions = list_open_action_tracking_for_teacher(conn, teacher_id)
        active_cycles = list_cycles_for_teacher(conn, teacher_id, active_only=True)
        all_cycles = list_cycles_for_teacher(conn, teacher_id)
        closed_cycles = [c for c in all_cycles if c["closed_at"]]
        # Private notes: coach-only surface. Principal / district get an empty
        # list (they see aggregate data on their own hubs, not the coach's
        # scratchpad). The teacher role never reaches this line — redirected
        # above.
        if viewer["role"] == "coach":
            private_notes = list_private_notes_for_teacher(
                conn, teacher_id=teacher_id, caller_role="coach"
            )
        else:
            private_notes = []
        practice_log = list_practice_log_for_teacher(conn, teacher_id, limit=20)

        # Observations list (all, latest first). Archived observations drop
        # out of the main list; the coach reaches them via a separate
        # "Show archived observations" toggle rendered on the hub.
        observations = conn.execute(
            """SELECT o.id, o.video_filename, o.status, o.scored_at,
                      o.uploaded_at, o.failure_reason, o.deleted_at,
                      (SELECT rv.domain_assessments FROM report_versions rv
                       WHERE rv.observation_id = o.id
                       ORDER BY rv.version_number DESC LIMIT 1) AS latest_das
               FROM observations o
               WHERE o.teacher_id = ? AND o.deleted_at IS NULL
               ORDER BY o.uploaded_at DESC""",
            (teacher_id,),
        ).fetchall()
        obs_rows = []
        for o in observations:
            das = json.loads(o["latest_das"]) if o["latest_das"] else []
            ratings = {da["domain"]: da["overall_rating"] for da in das}
            obs_rows.append({
                "id": o["id"],
                "video_filename": o["video_filename"],
                "status": o["status"],
                "failure_reason": o["failure_reason"],
                "scored_at": _fmt_ts(o["scored_at"]),
                "uploaded_at": _fmt_ts(o["uploaded_at"]),
                "ratings": ratings,
            })

        # Count of this teacher's archived observations — used to render the
        # "Show N archived" toggle on the hub. The toggle expands into a
        # separate list below via the `archived_obs` view state.
        archived_obs_count = conn.execute(
            """SELECT COUNT(*) AS c FROM observations
               WHERE teacher_id = ? AND deleted_at IS NOT NULL""",
            (teacher_id,),
        ).fetchone()["c"]
        archived_obs = conn.execute(
            """SELECT o.id, o.video_filename, o.status, o.scored_at,
                      o.uploaded_at, o.deleted_at
               FROM observations o
               WHERE o.teacher_id = ? AND o.deleted_at IS NOT NULL
               ORDER BY o.deleted_at DESC""",
            (teacher_id,),
        ).fetchall() if archived_obs_count else []
        archived_obs_rows = [{
            "id": o["id"],
            "video_filename": o["video_filename"],
            "status": o["status"],
            "scored_at": _fmt_ts(o["scored_at"]),
            "uploaded_at": _fmt_ts(o["uploaded_at"]),
            "deleted_at": _fmt_ts(o["deleted_at"]),
        } for o in archived_obs]

        # --- Teacher-scoped attention items ---
        from datetime import datetime, timezone, timedelta, date as _date
        _now = datetime.now(timezone.utc)
        _2d = (_now - timedelta(days=2)).isoformat()
        _7d = (_now - timedelta(days=7)).isoformat()
        _3d = (_now - timedelta(days=3)).isoformat()

        def _days(iso):
            try:
                dt = _dt.fromisoformat(iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0, (_now - dt).days)
            except Exception:
                return 0

        t_attention = []
        for r in conn.execute(
            """SELECT id, scored_at FROM observations
               WHERE teacher_id = ? AND status = 'complete'
                 AND deleted_at IS NULL
                 AND (debrief_focus IS NULL OR debrief_focus = '')
                 AND scored_at IS NOT NULL AND scored_at < ?
               ORDER BY scored_at DESC LIMIT 3""",
            (teacher_id, _2d),
        ).fetchall():
            t_attention.append({
                "kind": "debrief", "obs_id": r["id"],
                "days": _days(r["scored_at"]),
            })
        for r in conn.execute(
            """SELECT id, bite_sized_action_text, created_at
               FROM bite_sized_action_tracking
               WHERE teacher_id = ? AND implementation IS NULL AND created_at < ?
               ORDER BY created_at ASC LIMIT 3""",
            (teacher_id, _7d),
        ).fetchall():
            t_attention.append({
                "kind": "action", "action_id": r["id"],
                "text": r["bite_sized_action_text"],
                "days": _days(r["created_at"]),
            })
        for c in active_cycles:
            ecd = c.get("expected_close_date")
            if not ecd:
                continue
            try:
                days_until = (_date.fromisoformat(ecd) - _now.date()).days
            except Exception:
                continue
            if days_until <= 7:
                t_attention.append({
                    "kind": "cycle", "cycle_id": c["id"],
                    "days_until": days_until, "expected_close_date": ecd,
                })
        for r in conn.execute(
            """SELECT id, title, status, COALESCE(updated_at, created_at) AS last_touched
               FROM lesson_plans
               WHERE teacher_id = ?
                 AND status IN ('submitted', 'revision_requested')
                 AND COALESCE(updated_at, created_at) < ?
               ORDER BY last_touched ASC LIMIT 3""",
            (teacher_id, _3d),
        ).fetchall():
            t_attention.append({
                "kind": "lp", "lp_id": r["id"], "title": r["title"],
                "status": r["status"], "days": _days(r["last_touched"]),
            })

        # Observation-due attention (overdue by district cadence).
        # We compute `next_obs_due` a few lines below; will append after.

        # --- District priorities + interval targets ---
        from datetime import datetime as _dtx
        _nowx = _dtx.now(timezone.utc)
        _year_x = f"{_nowx.year - 1}-{_nowx.year}" if _nowx.month < 8 else f"{_nowx.year}-{_nowx.year + 1}"
        _dc = get_district_context(conn, org_id=_SEEDED_IDS["org_id"], academic_year=_year_x)
        next_obs_due = _next_obs_due(
            conn, teacher_id,
            _dc.get("days_between_obs_target") if _dc else None,
        )
        if next_obs_due and next_obs_due["overdue"]:
            t_attention.append({
                "kind": "obs_due",
                "days_late": -next_obs_due["days_until"],
                "last_obs_at": next_obs_due["last_obs_at"],
            })
        district_priorities_for_teacher = []
        if _dc and _dc.get("district_priorities_json"):
            for p in _dc["district_priorities_json"]:
                if isinstance(p, dict):
                    district_priorities_for_teacher.append({
                        "name": p.get("name", ""),
                        "description": p.get("description", ""),
                        "domain": (p.get("target_domains") or [None])[0],
                    })
                else:
                    district_priorities_for_teacher.append({"name": str(p), "description": "", "domain": None})
        # Set of currently-valid priority names; used to flag goals whose
        # origin_priority_name no longer matches the district's active list
        # (renamed, retired, or removed via the admin editor).
        current_priority_names = {p["name"] for p in district_priorities_for_teacher if p.get("name")}

        # Teacher-scoped implementation stats
        t_action_rows = conn.execute(
            "SELECT implementation FROM bite_sized_action_tracking WHERE teacher_id = ?",
            (teacher_id,),
        ).fetchall()
        t_total = len(t_action_rows)
        t_assessed_rows = [r for r in t_action_rows if r["implementation"]]
        t_assessed = len(t_assessed_rows)
        t_impl_rate = None
        t_success_rate = None
        if t_assessed_rows:
            from collections import Counter as _C
            _c = _C(r["implementation"] for r in t_assessed_rows)
            t_impl_rate = round(
                (_c.get("full", 0) + 0.5 * _c.get("partial", 0)) / t_assessed * 100, 1
            )
            t_success_rate = round(_c.get("full", 0) / t_assessed * 100, 1)

    finally:
        conn.close()

    # Avatar palette index — deterministic per teacher name.
    _av_idx = sum(ord(c) for c in (teacher["name"] or "")) % 6

    # One-line summary for the hero card.
    def _pf(key):
        try:
            return profile.get(key) if hasattr(profile, "get") else getattr(profile, key, None)
        except Exception:
            return None

    _bits = []
    _yrs = _pf("years_teaching_total")
    if _yrs is not None:
        _bits.append(f"Year {_yrs}")
    _style = _pf("coaching_style_preference")
    if _style:
        _bits.append(f"{_style} coaching style")
    if active_cycles:
        prog = _cycle_progress(active_cycles[0])
        if prog:
            _bits.append(f"Week {prog['week_current']} of {prog['week_total']}")
        else:
            _bits.append("Active cycle")
    _hero_summary = " · ".join(_bits) if _bits else "Profile not yet completed"

    return TEMPLATES.TemplateResponse("teacher_detail.html", {
        "request": request,
        "teacher": {
            "id": teacher["id"], "name": teacher["name"],
            "archived_at": teacher["archived_at"],
        },
        "avatar_idx": _av_idx,
        "hero_summary": _hero_summary,
        "t_attention": t_attention,
        "t_impl_rate": t_impl_rate,
        "t_success_rate": t_success_rate,
        "t_total_actions": t_total,
        "t_assessed_actions": t_assessed,
        "profile": profile,
        "active_goals": active_goals,
        "closed_goals": closed_goals,
        "active_cycles": active_cycles,
        "closed_cycles": closed_cycles,
        "all_cycles": all_cycles,
        "movement": movement,
        "open_actions": open_actions,
        "private_notes": private_notes,
        "observations": obs_rows,
        "archived_observations": archived_obs_rows,
        "archived_obs_count": archived_obs_count,
        "domains": rubric.domains,
        "rubric": rubric,
        "self_rating_dimensions": SELF_RATING_DIMENSIONS,
        "district_priorities_for_teacher": district_priorities_for_teacher,
        "current_priority_names": current_priority_names,
        "next_obs_due": next_obs_due,
        "practice_log": practice_log,
    })


@app.get("/teachers/{teacher_id}/prep", response_class=HTMLResponse)
def teacher_prep_view(request: Request, teacher_id: str) -> HTMLResponse:
    """30-second pre-observation prep for the coach.

    Shows exactly the two things the coach needs before walking into the room:
      1. What the teacher is practicing (coach's most-recent published move,
         current bite-sized action, active goal + what's being released)
      2. The lesson plan for the class being observed (recent submitted plans)

    Deliberately excludes: private notes, prior observation summary, ratings,
    profile deep-dive. Those are elsewhere; this is the fast pre-obs glance.
    """
    # Coach/principal/district-only view — the teacher role never needs a
    # pre-obs page (nothing here is theirs to prep for). Refuse rather than
    # redirect: a teacher hitting this URL is either poking around or has a
    # stale link, and either way should get a clear stop rather than a silent
    # bounce.
    viewer = _current_viewer(request)
    if viewer["role"] == "teacher":
        raise HTTPException(403, "Pre-obs prep view is coach-facing only")
    conn = db_connect(DB_PATH)
    try:
        teacher = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not teacher:
            raise HTTPException(404, "Teacher not found")

        current_move = most_recent_published_move_for_teacher(conn, teacher_id)
        active_goals = list_goals_for_teacher(conn, teacher_id, active_only=True)
        active_goal = active_goals[0] if active_goals else None

        current_action = None
        row = conn.execute(
            """SELECT id, bite_sized_action_text, core_teacher_skill, related_domain,
                      created_at, implementation, teacher_account, teacher_account_at
               FROM bite_sized_action_tracking
               WHERE teacher_id = ?
               ORDER BY (implementation IS NULL) DESC, created_at DESC
               LIMIT 1""",
            (teacher_id,),
        ).fetchone()
        if row:
            current_action = dict(row)

        active_cycles = list_cycles_for_teacher(conn, teacher_id, active_only=True)
        active_cycle = active_cycles[0] if active_cycles else None
        prog = _cycle_progress(active_cycle) if active_cycle else None

        # Lesson plans — recent submitted / coach-reviewed / revision-requested.
        # Coach picks which matches today's lesson.
        lp_rows = conn.execute(
            """SELECT id, title, status, current_version, plan_start_date, plan_end_date,
                      COALESCE(updated_at, created_at) AS last_touched
               FROM lesson_plans
               WHERE teacher_id = ?
                 AND status IN ('submitted', 'coach_reviewed', 'revision_requested', 'approved')
               ORDER BY last_touched DESC LIMIT 5""",
            (teacher_id,),
        ).fetchall()
        recent_plans = [dict(r) for r in lp_rows]
    finally:
        conn.close()

    _av_idx = sum(ord(c) for c in (teacher["name"] or "")) % 6
    _first = teacher["name"].split()[0] if teacher["name"] else "there"

    return TEMPLATES.TemplateResponse("teacher_prep.html", {
        "request": request,
        "teacher": {"id": teacher["id"], "name": teacher["name"], "first": _first},
        "avatar_idx": _av_idx,
        "current_move": current_move,
        "current_action": current_action,
        "active_goal": active_goal,
        "active_cycle": active_cycle,
        "cycle_prog": prog,
        "recent_plans": recent_plans,
    })


@app.get("/teachers/{teacher_id}/teacher-view", response_class=HTMLResponse)
def teacher_facing_view(request: Request, teacher_id: str) -> HTMLResponse:
    """Sketch: teacher's own view of their growth arc.

    Coach-first coach-second is the reality of the existing teacher-detail
    page. This is the mirror: how the teacher themselves might see their
    coaching relationship. Growth-first, narrative-first, agentic.
    """
    # Role gate: teacher-role viewers may only see their OWN teacher-view.
    # A teacher URL-tampering to another teacher's id is refused (not
    # redirected — a redirect would be a silent hint that the id exists).
    viewer = _current_viewer(request)
    if viewer["role"] == "teacher" and viewer.get("teacher_id") != teacher_id:
        raise HTTPException(403, "Not authorized to view another teacher's page")
    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    conn = db_connect(DB_PATH)
    try:
        teacher = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not teacher:
            raise HTTPException(404, "Teacher not found")

        profile = get_teacher_profile(conn, teacher_id)
        active_goals = list_goals_for_teacher(conn, teacher_id, active_only=True)
        active_cycles = list_cycles_for_teacher(conn, teacher_id, active_only=True)
        movement = rubric_score_movement_for_teacher(conn, teacher_id)

        # Latest observation. Teacher NEVER sees raw AI output — the surface
        # they see is the coach's published move (from published_coach_moves).
        latest_obs_row = conn.execute(
            """SELECT o.id, o.video_filename, o.scored_at, o.uploaded_at, o.status
               FROM observations o
               WHERE o.teacher_id = ? AND o.status = 'complete'
                 AND o.deleted_at IS NULL
               ORDER BY o.scored_at DESC LIMIT 1""",
            (teacher_id,),
        ).fetchone()

        current_move = None          # move for the latest observation
        continuity_move = None       # fallback: most-recent published move overall
        awaiting_coach = False       # true when latest obs has no move yet
        if latest_obs_row:
            current_move = get_current_coach_move(conn, latest_obs_row["id"])
            if not current_move:
                awaiting_coach = True
                continuity_move = most_recent_published_move_for_teacher(conn, teacher_id)
        else:
            continuity_move = most_recent_published_move_for_teacher(conn, teacher_id)

        # Current bite-sized action (latest unassessed for this teacher).
        # Filter out actions whose source observation was archived.
        current_action_row = conn.execute(
            """SELECT bsat.id, bsat.core_teacher_skill, bsat.related_domain,
                      bsat.bite_sized_action_text,
                      bsat.created_at, bsat.implementation, bsat.evidence_notes,
                      bsat.teacher_account, bsat.teacher_account_at
               FROM bite_sized_action_tracking bsat
               JOIN observations src ON src.id = bsat.source_observation_id
               WHERE bsat.teacher_id = ? AND src.deleted_at IS NULL
               ORDER BY (bsat.implementation IS NULL) DESC, bsat.created_at DESC
               LIMIT 1""",
            (teacher_id,),
        ).fetchone()
        current_action = dict(current_action_row) if current_action_row else None

        # Existing HLM response on the latest observation (if any).
        existing_hlm_response = None
        response_is_stale_for_teacher = False
        if latest_obs_row:
            existing_hlm_response = get_hlm_response(conn, latest_obs_row["id"])
            # Teacher's response is stale to them too when the coach republished
            # a newer version after they responded — they haven't seen the
            # update yet.
            if (existing_hlm_response and current_move
                    and existing_hlm_response.get("published_coach_move_id")
                    and existing_hlm_response["published_coach_move_id"] != current_move["id"]):
                response_is_stale_for_teacher = True

        # Their observations list (compact).
        obs_rows = [dict(r) for r in conn.execute(
            """SELECT id, video_filename, status, uploaded_at, scored_at
               FROM observations WHERE teacher_id = ? AND deleted_at IS NULL
               ORDER BY uploaded_at DESC LIMIT 8""",
            (teacher_id,),
        ).fetchall()]

        # Practice log entries (recent).
        practice_log = list_practice_log_for_teacher(conn, teacher_id, limit=10)

        # Their lesson plans (compact).
        lp_rows = [dict(r) for r in conn.execute(
            """SELECT id, title, status, current_version,
                      COALESCE(updated_at, created_at) AS last_touched
               FROM lesson_plans WHERE teacher_id = ?
               ORDER BY last_touched DESC LIMIT 8""",
            (teacher_id,),
        ).fetchall()]

    finally:
        conn.close()

    active_cycle = active_cycles[0] if active_cycles else None
    prog = _cycle_progress(active_cycle) if active_cycle else None

    from datetime import datetime as __dt
    _hour = __dt.now().hour
    if _hour < 12: _greeting = "Good morning"
    elif _hour < 17: _greeting = "Good afternoon"
    else: _greeting = "Good evening"

    _first = teacher["name"].split()[0] if teacher["name"] else "there"

    # Consent — the banner on the teacher-view tells them the current state
    # and offers grant or revoke. Coach-role viewers see it read-only.
    _consent_conn = db_connect(DB_PATH)
    try:
        _consent = get_active_consent(_consent_conn, teacher_id=teacher_id)
    finally:
        _consent_conn.close()

    return TEMPLATES.TemplateResponse("teacher_view.html", {
        "request": request,
        "teacher": {"id": teacher["id"], "name": teacher["name"], "first": _first},
        "greeting": _greeting,
        "profile": profile,
        "active_cycle": active_cycle,
        "cycle_prog": prog,
        "active_goals": active_goals,
        "latest_obs": dict(latest_obs_row) if latest_obs_row else None,
        "current_move": current_move,
        "continuity_move": continuity_move,
        "awaiting_coach": awaiting_coach,
        "existing_hlm_response": existing_hlm_response,
        "response_is_stale_for_teacher": response_is_stale_for_teacher,
        "current_action": current_action,
        "observations": obs_rows,
        "lesson_plans": lp_rows,
        "movement": movement,
        "domains": rubric.domains,
        "self_rating_dimensions": SELF_RATING_DIMENSIONS,
        "practice_log": practice_log,
        "consent": _consent,
    })


@app.post("/teachers/{teacher_id}/consent/grant")
def grant_consent_route(request: Request, teacher_id: str) -> RedirectResponse:
    """Record consent for this teacher. Only the teacher themselves (or a
    coach in dev mode, so smoke tests can seed consent) may grant.
    """
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, "Consent needs a sign-in")
    # Teacher grants their own consent. Coach may grant only for pilot smoke
    # tests, and only when they own the teacher AND OBSERVER_DEV_LOGIN=1 is
    # set — production consent is always the teacher's affirmative act, never
    # a coach's on their behalf. Principals / district are refused entirely.
    role = viewer["role"]
    if role == "teacher":
        if viewer.get("teacher_id") != teacher_id:
            raise HTTPException(403, "Not your consent to grant")
    elif role == "coach":
        if not DEV_LOGIN_ENABLED:
            raise HTTPException(
                403,
                "Only the teacher may grant their own consent. "
                "In dev (OBSERVER_DEV_LOGIN=1) a coach may seed consent for their own caseload."
            )
        # Even in dev, refuse a coach forging consent for someone else's teacher.
        _require_teacher_access(viewer, teacher_id, "Consent grant")
    else:
        raise HTTPException(403, "Only the teacher may grant their own consent")
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT org_id FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        grant_consent(
            conn, org_id=row["org_id"], teacher_id=teacher_id,
            granted_by_user_id=viewer.get("user_id") or _SEEDED_IDS.get("user_id"),
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}/teacher-view", status_code=303)


@app.post("/teachers/{teacher_id}/consent/revoke")
def revoke_consent_route(request: Request, teacher_id: str) -> RedirectResponse:
    """Revoke consent for this teacher. Teacher-only — a coach can't unilaterally
    revoke on someone's behalf. Blocks future uploads; existing observations
    stay (district retention policy governs purge).
    """
    viewer = _current_viewer(request)
    if viewer.get("role") == "anon":
        raise HTTPException(401, "Consent needs a sign-in")
    if viewer["role"] != "teacher" or viewer.get("teacher_id") != teacher_id:
        raise HTTPException(403, "Only the teacher may revoke their own consent")
    conn = db_connect(DB_PATH)
    try:
        revoke_consent(conn, teacher_id=teacher_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}/teacher-view", status_code=303)


@app.get("/teachers/{teacher_id}/profile/{side}", response_class=HTMLResponse)
def profile_edit_page(request: Request, teacher_id: str, side: str) -> HTMLResponse:
    """Edit form for one side of the profile. side ∈ {'teacher', 'coach'}."""
    if side not in ("teacher", "coach"):
        raise HTTPException(404, "Unknown profile side")
    # Role gate: coach side is coach-only (a teacher must not see the coach's
    # ratings-with-context on themselves or anyone else). Teacher side is
    # editable by the teacher on their own record, or by their coach.
    viewer = _current_viewer(request)
    if side == "coach":
        _require_coach(viewer, "Coach profile edit")
    elif viewer["role"] == "teacher" and viewer.get("teacher_id") != teacher_id:
        raise HTTPException(403, "Not authorized to edit another teacher's profile")
    conn = db_connect(DB_PATH)
    try:
        teacher = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (teacher_id,)
        ).fetchone()
        if not teacher:
            raise HTTPException(404)
        profile = get_teacher_profile(conn, teacher_id)
    finally:
        conn.close()
    _av_idx = sum(ord(c) for c in (teacher["name"] or "")) % 6
    return TEMPLATES.TemplateResponse("profile_edit.html", {
        "request": request,
        "teacher": {"id": teacher["id"], "name": teacher["name"]},
        "avatar_idx": _av_idx,
        "profile": profile,
        "side": side,
        "self_rating_dimensions": SELF_RATING_DIMENSIONS,
    })


@app.post("/teachers/{teacher_id}/profile/teacher")
async def profile_save_teacher_side(
    teacher_id: str,
    years_teaching_total: Optional[str] = Form(None),
    years_teaching_subject: Optional[str] = Form(None),
    years_at_current_school: Optional[str] = Form(None),
    highest_credential: Optional[str] = Form(None),
    subjects_taught: Optional[str] = Form(None),
    grade_levels_taught: Optional[str] = Form(None),
    coaching_style_preference_select: Optional[str] = Form(None),
    coaching_style_preference_other: Optional[str] = Form(None),
    career_narrative_notes: Optional[str] = Form(None),
    career_goals_notes: Optional[str] = Form(None),
    request: Request = None,
) -> RedirectResponse:
    """POST from the teacher-side profile form."""
    _guard_teacher_write(request, teacher_id, "Teacher profile write")
    form = await request.form()
    self_ratings = {}
    for dim in SELF_RATING_DIMENSIONS:
        v = form.get(f"self_rating_{dim}")
        if v:
            try:
                self_ratings[dim] = int(v)
            except ValueError:
                pass

    def _int_or_none(s: Optional[str]) -> Optional[int]:
        if s is None or s == "":
            return None
        try:
            return int(s)
        except ValueError:
            return None

    def _list_or_none(s: Optional[str]) -> Optional[list]:
        if s is None or s.strip() == "":
            return None
        return [t.strip() for t in s.split(",") if t.strip()]

    # Resolve coaching style: 'other' → use the free-text; else use the selected value.
    if coaching_style_preference_select == "other":
        style_pref = (coaching_style_preference_other or "").strip() or None
    else:
        style_pref = (coaching_style_preference_select or "").strip() or None

    conn = db_connect(DB_PATH)
    try:
        upsert_teacher_profile_teacher_side(
            conn,
            teacher_id=teacher_id,
            years_teaching_total=_int_or_none(years_teaching_total),
            years_teaching_subject=_int_or_none(years_teaching_subject),
            years_at_current_school=_int_or_none(years_at_current_school),
            highest_credential=highest_credential or None,
            subjects_taught=_list_or_none(subjects_taught),
            grade_levels_taught=_list_or_none(grade_levels_taught),
            self_ratings=self_ratings or None,
            coaching_style_preference=style_pref,
            career_narrative_notes=career_narrative_notes or None,
            career_goals_notes=career_goals_notes or None,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.post("/teachers/{teacher_id}/profile/coach")
async def profile_save_coach_side(
    teacher_id: str,
    coach_notes_on_teacher: Optional[str] = Form(None),
    observed_style_notes: Optional[str] = Form(None),
    skill_development_narrative: Optional[str] = Form(None),
    request: Request = None,
) -> RedirectResponse:
    # Coach-only: coach ratings + coach's private notes on the teacher.
    _require_coach(_current_viewer(request), "Coach profile edit")
    _guard_teacher_write(request, teacher_id, "Coach profile edit")
    form = await request.form()
    coach_ratings = {}
    coach_ratings_context = {}
    for dim in SELF_RATING_DIMENSIONS:
        v = form.get(f"coach_rating_{dim}")
        if v:
            try:
                coach_ratings[dim] = int(v)
            except ValueError:
                pass
        ctx = form.get(f"coach_rating_ctx_{dim}")
        if ctx and ctx.strip():
            coach_ratings_context[dim] = ctx.strip()

    conn = db_connect(DB_PATH)
    try:
        upsert_teacher_profile_coach_side(
            conn,
            teacher_id=teacher_id,
            coach_notes_on_teacher=coach_notes_on_teacher or None,
            observed_style_notes=observed_style_notes or None,
            coach_ratings=coach_ratings or None,
            coach_ratings_context=coach_ratings_context or None,
            skill_development_narrative=skill_development_narrative or None,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.post("/teachers/{teacher_id}/goals")
def create_goal_route(
    request: Request,
    teacher_id: str,
    title: str = Form(...),
    description: Optional[str] = Form(None),
    proposed_by: str = Form("coach"),
    teacher_notes: Optional[str] = Form(None),
    coach_notes: Optional[str] = Form(None),
    related_rubric_domain: Optional[str] = Form(None),
    related_core_teacher_skill: Optional[str] = Form(None),
    success_indicators: Optional[str] = Form(None),
    coaching_cycle_id: Optional[str] = Form(None),
    releasing: Optional[str] = Form(None),
    origin_priority_name: Optional[str] = Form(None),
    agree_immediately: Optional[str] = Form(None),
) -> RedirectResponse:
    """Create a professional goal. Optionally attach to a coaching cycle.
    ``success_indicators`` is a newline-separated text field.

    Coach-only: goals are proposed by the coach. Teacher-role acknowledges
    via the agree route once the goal is proposed.
    """
    _require_coach(_current_viewer(request), "Goal creation")
    viewer = _guard_teacher_write(request, teacher_id, "Goal creation")
    _conn_arch = db_connect(DB_PATH)
    try:
        _refuse_if_archived(_conn_arch, teacher_id, "Goal creation")
    finally:
        _conn_arch.close()
    # Whitelist proposed_by so malformed input surfaces as 400, not 500.
    # The DB helper itself validates too; catch here for the friendlier error.
    if proposed_by not in ("coach", "teacher", "joint"):
        raise HTTPException(400, f"proposed_by must be coach/teacher/joint, got {proposed_by!r}")
    indicators = None
    if success_indicators:
        indicators = [line.strip() for line in success_indicators.splitlines() if line.strip()]

    conn = db_connect(DB_PATH)
    try:
        goal_id = create_goal(
            conn,
            org_id=viewer.get("org_id") or _SEEDED_IDS["org_id"],
            teacher_id=teacher_id,
            coaching_cycle_id=coaching_cycle_id or None,
            title=title,
            description=description or None,
            proposed_by=proposed_by,
            success_indicators=indicators,
            teacher_notes=teacher_notes or None,
            coach_notes=coach_notes or None,
            related_rubric_domain=related_rubric_domain or None,
            related_core_teacher_skill=related_core_teacher_skill or None,
        )
        if releasing and releasing.strip():
            set_goal_releasing(conn, goal_id=goal_id, releasing=releasing.strip())
        if origin_priority_name and origin_priority_name.strip():
            conn.execute(
                "UPDATE professional_goals SET origin_priority_name = ? WHERE id = ?",
                (origin_priority_name.strip(), goal_id),
            )
            conn.commit()
        if agree_immediately:
            agree_goal(conn, goal_id)

        # Retroactive attach: open (unassessed) bite-sized actions on the same
        # related_domain that are either orphan OR attached to a goal that
        # closed unmet/partial/abandoned — cascade them to the new goal.
        # (Actions on closed_MET goals stay where they are — those served their
        # purpose and shouldn't get repurposed into a fresh cycle.)
        if related_rubric_domain:
            attached = conn.execute(
                """UPDATE bite_sized_action_tracking
                   SET goal_id = ?
                   WHERE teacher_id = ? AND related_domain = ?
                     AND implementation IS NULL
                     AND (goal_id IS NULL
                          OR goal_id IN (
                              SELECT id FROM professional_goals
                              WHERE status IN ('closed_partial', 'closed_unmet', 'abandoned')
                          ))""",
                (goal_id, teacher_id, related_rubric_domain),
            )
            conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


# ---------------------------------------------------------------------------
# Coaching cycles
# ---------------------------------------------------------------------------


@app.post("/teachers/{teacher_id}/cycles")
def create_cycle_route(
    request: Request,
    teacher_id: str,
    title: Optional[str] = Form(None),
    notes: Optional[str] = Form(None),
    expected_close_date: Optional[str] = Form(None),
) -> RedirectResponse:
    """Open a new coaching cycle. Hard constraint: one active cycle per teacher.
    Default expected_close_date is opened_at + 21 days.
    Coach-only.
    """
    _require_coach(_current_viewer(request), "Cycle creation")
    viewer = _guard_teacher_write(request, teacher_id, "Cycle creation")
    conn = db_connect(DB_PATH)
    try:
        _refuse_if_archived(conn, teacher_id, "Cycle creation")
        existing = list_cycles_for_teacher(conn, teacher_id, active_only=True)
        if existing:
            active = existing[0]
            raise HTTPException(
                409,
                f"Teacher already has an active cycle "
                f"(opened {active.get('opened_at', '')[:10]}). Close it before opening a new one."
            )
        try:
            cycle_id = create_cycle(
                conn,
                org_id=viewer.get("org_id") or _SEEDED_IDS["org_id"],
                teacher_id=teacher_id,
                coach_user_id=_viewer_uid(viewer),
                title=(title or "Coaching cycle"),
                notes=notes or None,
                expected_close_date=expected_close_date or None,
            )
        except sqlite3.IntegrityError as e:
            # Storage-layer partial-unique index caught a concurrent open of
            # a second cycle. The other POST already succeeded; surface it as
            # 409 the same as the app-level check-then-insert path.
            if "uq_one_open_cycle_per_teacher" in str(e):
                raise HTTPException(
                    409,
                    "Another open request landed first — this teacher already has an active cycle."
                )
            raise
    finally:
        conn.close()
    return RedirectResponse(url=f"/cycles/{cycle_id}", status_code=303)


@app.post("/cycles/{cycle_id}/update-close-date")
def update_cycle_close_date_route(
    request: Request,
    cycle_id: str,
    expected_close_date: str = Form(...),
) -> RedirectResponse:
    _require_coach(_current_viewer(request), "Cycle date edit")
    _guard_cycle_write(request, cycle_id, "Cycle date edit")
    from pipeline.db import update_cycle_expected_close
    conn = db_connect(DB_PATH)
    try:
        try:
            update_cycle_expected_close(conn, cycle_id, expected_close_date)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()
    return RedirectResponse(url=f"/cycles/{cycle_id}", status_code=303)


@app.post("/cycles/{cycle_id}/growth-story")
def cycle_growth_story_edit(
    request: Request,
    cycle_id: str,
    growth_story: str = Form(...),
) -> RedirectResponse:
    """Edit the growth story on a closed (or still-active) cycle.

    Coach-only — teachers see the story but don't author it. Small edits
    after close are common as the coach's reflection lands.
    """
    if request.state.viewer["role"] != "coach":
        raise HTTPException(403, "Only the coach edits growth stories")
    _guard_cycle_write(request, cycle_id, "Growth story edit")
    conn = db_connect(DB_PATH)
    try:
        conn.execute(
            "UPDATE coaching_cycles SET growth_story = ? WHERE id = ?",
            (growth_story.strip() or None, cycle_id),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/cycles/{cycle_id}", status_code=303)


@app.post("/cycles/{cycle_id}/close")
def close_cycle_route(
    request: Request,
    cycle_id: str,
    teacher_id: str = Form(...),
    closing_notes: Optional[str] = Form(None),
    growth_story: Optional[str] = Form(None),
    confirm_empty: Optional[str] = Form(None),
) -> RedirectResponse:
    """Close a cycle. If it has 0 observations or 0 goals, require explicit
    ``confirm_empty=1`` to prevent accidental close of a neglected cycle.
    Coach-only — closing a cycle is a records-of-record action.
    """
    _require_coach(_current_viewer(request), "Cycle close")
    _guard_cycle_write(request, cycle_id, "Cycle close")
    conn = db_connect(DB_PATH)
    try:
        n_obs = conn.execute(
            "SELECT COUNT(*) AS c FROM observations WHERE coaching_cycle_id = ? AND status = 'complete' AND deleted_at IS NULL",
            (cycle_id,),
        ).fetchone()["c"]
        n_goals = conn.execute(
            "SELECT COUNT(*) AS c FROM professional_goals WHERE coaching_cycle_id = ?",
            (cycle_id,),
        ).fetchone()["c"]
        n_open_goals = conn.execute(
            """SELECT COUNT(*) AS c FROM professional_goals
               WHERE coaching_cycle_id = ? AND status IN ('proposed', 'active')""",
            (cycle_id,),
        ).fetchone()["c"]
        # Additional close-hygiene checks: work-in-progress that would be
        # silently stranded by closing. Each maps to a workflow surface we
        # spent the audit shoring up — closing a cycle without noticing these
        # negates the shoring.
        # (a) Bite-sized actions issued in this cycle that were never assessed:
        n_unassessed_actions = conn.execute(
            """SELECT COUNT(*) AS c FROM bite_sized_action_tracking ba
               WHERE ba.source_observation_id IN (
                   SELECT id FROM observations
                   WHERE coaching_cycle_id = ? AND deleted_at IS NULL
               ) AND ba.implementation IS NULL""",
            (cycle_id,),
        ).fetchone()["c"]
        # (b) Teacher HLM responses ('adjust'/'talk') that the coach never
        # acknowledged. 'resonates' does not need explicit ack — it's the
        # green-light response and closing on it is fine.
        n_unacked_hlm = conn.execute(
            """SELECT COUNT(*) AS c FROM hlm_responses hr
               WHERE hr.observation_id IN (
                   SELECT id FROM observations
                   WHERE coaching_cycle_id = ? AND deleted_at IS NULL
               ) AND hr.acknowledged_at IS NULL
                 AND hr.response_type IN ('adjust', 'talk')""",
            (cycle_id,),
        ).fetchone()["c"]
        # (c) Observations in this cycle with no published coach move — the
        # AI ran, but the coach never mediated. That's a coaching gap this
        # tool exists to prevent.
        n_obs_without_move = conn.execute(
            """SELECT COUNT(*) AS c FROM observations o
               WHERE o.coaching_cycle_id = ? AND o.status = 'complete'
                 AND o.deleted_at IS NULL
                 AND NOT EXISTS (
                     SELECT 1 FROM published_coach_moves pcm
                     WHERE pcm.observation_id = o.id AND pcm.superseded_at IS NULL
                 )""",
            (cycle_id,),
        ).fetchone()["c"]
        warnings = []
        if n_obs == 0: warnings.append("no_obs")
        if n_goals == 0: warnings.append("no_goals")
        if n_open_goals > 0: warnings.append("open_goals")
        if n_unassessed_actions > 0: warnings.append(f"unassessed_actions:{n_unassessed_actions}")
        if n_unacked_hlm > 0: warnings.append(f"unacked_hlm:{n_unacked_hlm}")
        if n_obs_without_move > 0: warnings.append(f"obs_no_move:{n_obs_without_move}")
        if warnings and not confirm_empty:
            return RedirectResponse(
                url=f"/cycles/{cycle_id}?close_warn={','.join(warnings)}", status_code=303
            )
        from pipeline.db import CycleAlreadyClosedError
        try:
            close_cycle(conn, cycle_id,
                        closing_notes=closing_notes or None,
                        growth_story=(growth_story.strip() if growth_story else None) or None)
        except CycleAlreadyClosedError as e:
            raise HTTPException(409, str(e))
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.get("/cycles/{cycle_id}", response_class=HTMLResponse)
def cycle_detail(request: Request, cycle_id: str, close_warn: Optional[str] = None) -> HTMLResponse:
    return _render_cycle(request, cycle_id, template="cycle_detail.html", close_warn=close_warn)


@app.get("/cycles/{cycle_id}/print", response_class=HTMLResponse)
def cycle_print(request: Request, cycle_id: str) -> HTMLResponse:
    """Print-optimized cycle report. Coach hits Cmd+P → Save as PDF."""
    return _render_cycle(request, cycle_id, template="cycle_print.html")


def _render_cycle(request: Request, cycle_id: str, *, template: str, close_warn: Optional[str] = None) -> HTMLResponse:
    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    conn = db_connect(DB_PATH)
    try:
        cycle = get_cycle(conn, cycle_id)
        if not cycle:
            raise HTTPException(404, "Cycle not found")
        # Role gate: cycle detail carries coach-authored narrative (growth
        # story, goal deliberation, private domain lists). Teacher-role can
        # only see cycles belonging to them; principal/district/coach see all.
        # Denied AFTER the existence check so a teacher URL-tampering to a
        # non-existent cycle gets 404 (leaks no id), and to another teacher's
        # cycle gets 403 (leaks only that a cycle with that id exists — same
        # as the observation gate at _viewer_from_obs).
        viewer = _current_viewer(request)
        if viewer["role"] == "teacher" and viewer.get("teacher_id") != cycle["teacher_id"]:
            raise HTTPException(403, "Not authorized to view this cycle")
        teacher = conn.execute(
            "SELECT id, name FROM teachers WHERE id = ?", (cycle["teacher_id"],)
        ).fetchone()
        goals = list_goals_in_cycle(conn, cycle_id)
        observations = list_observations_in_cycle(conn, cycle_id)
        # For each observation, pull its ratings + tracking counts.
        obs_ratings = []
        for o in observations:
            das_row = conn.execute(
                """SELECT rv.domain_assessments FROM report_versions rv
                   WHERE rv.observation_id = ?
                   ORDER BY rv.version_number DESC LIMIT 1""",
                (o["id"],),
            ).fetchone()
            das = json.loads(das_row["domain_assessments"]) if das_row and das_row["domain_assessments"] else []
            ratings = {da["domain"]: da["overall_rating"] for da in das}
            obs_ratings.append({**o, "ratings": ratings})

        # --- Cycle-scoped bite-sized action implementation rate ---
        # Actions issued by observations WITHIN this cycle:
        obs_ids = [o["id"] for o in observations]
        actions_in_cycle = []
        if obs_ids:
            placeholders = ",".join("?" for _ in obs_ids)
            rows = conn.execute(
                f"""SELECT * FROM bite_sized_action_tracking
                    WHERE source_observation_id IN ({placeholders})
                    ORDER BY created_at ASC""",
                obs_ids,
            ).fetchall()
            actions_in_cycle = [dict(r) for r in rows]
    finally:
        conn.close()

    # Implementation-rate stats
    total_actions = len(actions_in_cycle)
    assessed = [a for a in actions_in_cycle if a.get("implementation")]
    by_status = {"full": 0, "partial": 0, "not_observed": 0, "regressed": 0}
    for a in assessed:
        by_status[a["implementation"]] = by_status.get(a["implementation"], 0) + 1
    implementation_rate = None
    if assessed:
        # "Rate" = (full + 0.5 * partial) / assessed count
        implementation_rate = round(
            (by_status["full"] + 0.5 * by_status["partial"]) / len(assessed) * 100, 1
        )
    action_stats = {
        "total_issued": total_actions,
        "total_assessed": len(assessed),
        "awaiting": total_actions - len(assessed),
        "by_status": by_status,
        "implementation_rate_pct": implementation_rate,
    }

    # Impact summary: for each targeted rubric domain (from goals), show the
    # rating movement across the cycle's observations + delta (first → last).
    targeted_domains = list(dict.fromkeys(
        g["related_rubric_domain"] for g in goals if g.get("related_rubric_domain")
    ))
    impact = []
    for d in targeted_domains:
        trail = [(o["scored_at"] or o["uploaded_at"], o["ratings"].get(d))
                 for o in obs_ratings if o["ratings"].get(d)]
        delta = None
        if len(trail) >= 2:
            first = rubric.score_for(trail[0][1])
            last = rubric.score_for(trail[-1][1])
            delta = last - first
        if trail:
            impact.append({"domain": d, "trail": trail, "delta": delta,
                           "n_observations": len(trail)})

    _av_idx = sum(ord(c) for c in (teacher["name"] or "")) % 6

    # Print-report scaffolding: prepared-on date + span in weeks. The cycle
    # detail page shows week progress from the cycle-progress helper; the
    # print report needs its own thing because closed cycles report a fixed
    # span, not a live "week X of Y".
    from datetime import datetime as _dt_print, date as _date_print
    _prepared_on = _dt_print.now(timezone.utc).date().isoformat()
    _cycle_span_weeks = None
    _cycle_span_days = None
    try:
        _opened = _date_print.fromisoformat(cycle["opened_at"][:10])
        _end = _date_print.fromisoformat((cycle["closed_at"] or _prepared_on)[:10])
        _cycle_span_days = (_end - _opened).days
        _cycle_span_weeks = max(1, round(_cycle_span_days / 7))
    except Exception:
        pass

    return TEMPLATES.TemplateResponse(template, {
        "request": request,
        "cycle": cycle,
        "teacher": {"id": teacher["id"], "name": teacher["name"]},
        "avatar_idx": _av_idx,
        "close_warn": close_warn,
        "goals": goals,
        "observations": obs_ratings,
        "impact": impact,
        "action_stats": action_stats,
        "actions_in_cycle": actions_in_cycle,
        "domains": rubric.domains,
        "is_active": cycle.get("closed_at") is None,
        "prepared_on": _prepared_on,
        "cycle_span_weeks": _cycle_span_weeks,
        "cycle_span_days": _cycle_span_days,
    })


@app.post("/observations/{observation_id}/attach-cycle")
def attach_observation_cycle_route(
    request: Request,
    observation_id: str,
    coaching_cycle_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Attach or detach an observation from a coaching cycle. Coach-only.
    Enforces same-teacher: the cycle and the observation must belong to the
    same teacher, else the attach would corrupt cycle-scoped impact rollups.
    """
    _require_coach(_current_viewer(request), "Attach observation to cycle")
    _guard_obs_write(request, observation_id, "Attach observation to cycle")
    conn = db_connect(DB_PATH)
    try:
        if coaching_cycle_id:
            obs_tid = _obs_teacher_id(conn, observation_id)
            cyc_row = conn.execute(
                "SELECT teacher_id FROM coaching_cycles WHERE id = ?",
                (coaching_cycle_id,),
            ).fetchone()
            if not cyc_row or not obs_tid or cyc_row["teacher_id"] != obs_tid:
                raise HTTPException(400, "Cycle and observation belong to different teachers.")
        attach_observation_to_cycle(
            conn, observation_id=observation_id,
            cycle_id=coaching_cycle_id or None,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}", status_code=303)


@app.post("/observations/{observation_id}/attach-lesson-plan")
def attach_observation_lesson_plan_route(
    request: Request,
    observation_id: str,
    lesson_plan_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Attach or detach an observation from a specific lesson plan. Coach-only.
    Same-teacher enforced: cross-teacher plan attach would corrupt the
    practice-log join and the auto-attach ambiguity resolver on future obs.
    """
    _require_coach(_current_viewer(request), "Attach observation to lesson plan")
    _guard_obs_write(request, observation_id, "Attach observation to lesson plan")
    conn = db_connect(DB_PATH)
    try:
        if lesson_plan_id:
            obs_tid = _obs_teacher_id(conn, observation_id)
            lp_row = conn.execute(
                "SELECT teacher_id FROM lesson_plans WHERE id = ?",
                (lesson_plan_id,),
            ).fetchone()
            if not lp_row or not obs_tid or lp_row["teacher_id"] != obs_tid:
                raise HTTPException(400, "Plan and observation belong to different teachers.")
        conn.execute(
            "UPDATE observations SET lesson_plan_id = ? WHERE id = ?",
            (lesson_plan_id or None, observation_id),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}", status_code=303)


@app.get("/admin/district-context", response_class=HTMLResponse)
def district_context_page(request: Request, year: Optional[str] = None) -> HTMLResponse:
    """Admin form for the current org's district context for a chosen academic year."""
    _deny_teacher(_current_viewer(request), "District-context admin")
    if year is None:
        # Default to current academic year (August cutoff)
        from datetime import datetime as _dt
        now = _dt.now()
        year = f"{now.year}-{now.year + 1}" if now.month >= 8 else f"{now.year - 1}-{now.year}"

    conn = db_connect(DB_PATH)
    try:
        ctx = get_district_context(conn, org_id=_SEEDED_IDS["org_id"], academic_year=year)
        documents = list_district_documents(
            conn, org_id=_SEEDED_IDS["org_id"], academic_year=year,
        )
    finally:
        conn.close()

    return TEMPLATES.TemplateResponse("district_context.html", {
        "request": request,
        "academic_year": year,
        "ctx": ctx,
        "documents": documents,
        "rubric_domains": get_rubric(DEFAULT_RUBRIC_ID).domains,
    })


@app.post("/admin/district-documents")
async def upload_district_document(
    request: Request,
    academic_year: str = Form(...),
    title: str = Form(...),
    doc_type: Optional[str] = Form(None),
    document: UploadFile = File(...),
) -> RedirectResponse:
    """Upload a PDF or DOCX. Text is extracted and stored so it can be
    injected into the AI's context. Not-teacher only — district-document
    text feeds the AI prompt for every teacher, so a teacher-role write
    would be a district-wide injection vector.
    """
    viewer = _current_viewer(request)
    _deny_teacher(viewer, "District-documents upload")
    import uuid as _uuid
    doc_id = str(_uuid.uuid4())
    ext = Path(document.filename or "upload").suffix or ".bin"
    dest_dir = UPLOADS_DIR.parent / "district_documents" / doc_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (document.filename or f"upload{ext}")
    with dest_path.open("wb") as f:
        while chunk := await document.read(1024 * 1024):
            f.write(chunk)

    from pipeline.text_extract import extract_text
    extracted = extract_text(dest_path)

    conn = db_connect(DB_PATH)
    try:
        create_district_document(
            conn,
            org_id=viewer.get("org_id") or _SEEDED_IDS["org_id"],
            title=title.strip(),
            doc_type=(doc_type or "").strip() or None,
            file_ref=str(dest_path),
            original_filename=document.filename or "upload",
            extracted_text=extracted,
            uploaded_by_user_id=_viewer_uid(viewer),
            academic_year=academic_year,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin/district-context?year={academic_year}", status_code=303)


@app.post("/admin/district-documents/{doc_id}/archive")
def archive_district_document_route(
    request: Request,
    doc_id: str, academic_year: str = Form(...),
) -> RedirectResponse:
    _deny_teacher(_current_viewer(request), "District-documents archive")
    conn = db_connect(DB_PATH)
    try:
        archive_district_document(conn, doc_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin/district-context?year={academic_year}", status_code=303)


@app.post("/admin/district-context")
async def district_context_save(request: Request) -> RedirectResponse:
    """Save district context. Priorities and initiatives are newline-separated;
    initiatives can be `name | grades | description` per line for a bit of structure.
    year_arc_raw is one phase per line: `phase | start_month | end_month | description`.
    """
    _deny_teacher(_current_viewer(request), "District-context admin")
    form = await request.form()

    # Required
    academic_year = form.get("academic_year") or ""
    if not academic_year.strip():
        raise HTTPException(400, "academic_year required")

    year_start_date = form.get("year_start_date") or None
    year_end_date = form.get("year_end_date") or None
    priorities = form.get("priorities") or None
    initiatives = form.get("initiatives") or None
    year_arc_raw = form.get("year_arc_raw") or None
    _iv = lambda k: int(form[k]) if form.get(k) else None
    observations_per_year_target = _iv("observations_per_year_target")
    days_between_obs_target = _iv("days_between_obs_target")
    days_from_obs_to_debrief_target = _iv("days_from_obs_to_debrief_target")

    def _lines(s: Optional[str]) -> list:
        if not s:
            return []
        return [line.strip() for line in s.splitlines() if line.strip()]

    # Priorities: pull all `priority_name_*` fields from the form.
    # Fallback to the legacy plain-textarea `priorities` field for backwards compat.
    priorities_structured = []
    for key in form.keys():
        if not key.startswith("priority_name_"):
            continue
        suffix = key[len("priority_name_"):]
        name = (form.get(f"priority_name_{suffix}") or "").strip()
        if not name:
            continue
        desc = (form.get(f"priority_desc_{suffix}") or "").strip() or None
        domain = (form.get(f"priority_domain_{suffix}") or "").strip() or None
        entry = {"name": name}
        if desc:
            entry["description"] = desc
        if domain:
            entry["target_domains"] = [domain]
        priorities_structured.append(entry)

    priorities_list = priorities_structured or _lines(priorities) or None

    # Rename cascade: if a priority at position N was renamed (old name → new
    # name), update every goal that references the old name via
    # origin_priority_name so tag-based rollups don't lose their tie.
    conn_rename = db_connect(DB_PATH)
    try:
        _prev = conn_rename.execute(
            "SELECT district_priorities_json FROM district_context WHERE academic_year = ?",
            (academic_year,),
        ).fetchone()
        if _prev and _prev["district_priorities_json"] and priorities_structured:
            try:
                _prev_list = json.loads(_prev["district_priorities_json"])
            except Exception:
                _prev_list = []
            # Positional cascade is only safe when list length hasn't changed
            # (i.e. the admin renamed in place). If items were added or removed,
            # position no longer identifies "the same priority" — cascading
            # blindly would rename orphaned goals to the wrong priority. In
            # that case we leave old goals tagged with their prior name; they
            # will render a "retired priority" badge on the teacher view.
            if len(_prev_list) == len(priorities_structured):
                for i, new_entry in enumerate(priorities_structured):
                    old_entry = _prev_list[i]
                    old_name = old_entry["name"] if isinstance(old_entry, dict) else str(old_entry)
                    new_name = new_entry["name"]
                    if old_name and new_name and old_name != new_name:
                        conn_rename.execute(
                            "UPDATE professional_goals SET origin_priority_name = ? WHERE origin_priority_name = ?",
                            (new_name, old_name),
                        )
                conn_rename.commit()
    finally:
        conn_rename.close()

    initiatives_list = None
    if initiatives:
        items = []
        for line in _lines(initiatives):
            parts = [p.strip() for p in line.split("|")]
            entry = {"name": parts[0]}
            if len(parts) >= 2 and parts[1]:
                entry["grades"] = [g.strip() for g in parts[1].split(",") if g.strip()]
            if len(parts) >= 3 and parts[2]:
                entry["description"] = parts[2]
            items.append(entry)
        initiatives_list = items or None

    year_arc = None
    if year_arc_raw:
        arcs = []
        for line in _lines(year_arc_raw):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3:
                continue
            try:
                arcs.append({
                    "phase": parts[0],
                    "start_month": int(parts[1]),
                    "end_month": int(parts[2]),
                    "description": parts[3] if len(parts) >= 4 else "",
                })
            except ValueError:
                continue
        year_arc = arcs or None

    conn = db_connect(DB_PATH)
    try:
        upsert_district_context(
            conn,
            org_id=_SEEDED_IDS["org_id"],
            academic_year=academic_year,
            year_arc=year_arc,
            district_priorities=priorities_list,
            district_initiatives=initiatives_list,
            year_start_date=year_start_date or None,
            year_end_date=year_end_date or None,
            observations_per_year_target=observations_per_year_target,
            days_between_obs_target=days_between_obs_target,
            days_from_obs_to_debrief_target=days_from_obs_to_debrief_target,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/admin/district-context?year={academic_year}", status_code=303)


# ---------------------------------------------------------------------------
# Bulk user upload — roster import via CSV
# ---------------------------------------------------------------------------
#
# CSV columns (header row required):
#   name                    — display name (required)
#   email                   — required for coach/principal/district; optional for teachers
#   role                    — teacher | coach | principal | district
#   employee_id             — optional; district's unique id
#   assigned_coach_email    — teachers only; coach must already exist or be earlier in the CSV
#   grade_levels            — teachers only; comma-separated inside a quoted cell ("6,7,8")
#   subjects                — teachers only; comma-separated inside a quoted cell ("Math,Science")
#
# Coach-facing only. Idempotent: re-uploading the same CSV skips existing rows
# rather than overwriting profile fields. Archived matches error the row (the
# admin must restore or use a different id).


_CSV_ROLE_TO_DB_ROLE = {
    # Users table CHECK constraint only allows admin/coach/teacher_self_serve/viewer,
    # so principal and district land as 'admin' at the DB layer. The cookie-based
    # viewer system reads roles separately, so this doesn't affect access.
    "coach": "coach",
    "principal": "admin",
    "district": "admin",
}


def _split_csv_list(raw: Optional[str]) -> Optional[list]:
    """Split a comma-separated cell into a list, stripping whitespace and
    dropping empties. Returns None if nothing survives.
    """
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p and p.strip()]
    return parts or None


@app.get("/admin/users/upload", response_class=HTMLResponse)
def users_upload_page(request: Request) -> HTMLResponse:
    """Show the CSV-upload form + result table (empty on GET)."""
    # Coach-only — bulk-inserting users into the org has the same blast radius
    # as any other write-to-users route in this app, and no existing route lets
    # principal or district viewers write. `_require_coach` matches the rest
    # of the write-path pattern; using `_deny_teacher` here (the earlier draft)
    # let principal/district POST rosters, which they shouldn't.
    _require_coach(_current_viewer(request), "User upload")
    return TEMPLATES.TemplateResponse("users_upload.html", {
        "request": request,
        "results": None,
        "totals": None,
    })


@app.post("/admin/users/upload", response_class=HTMLResponse)
async def users_upload_submit(
    request: Request,
    csv_file: UploadFile = File(...),
) -> HTMLResponse:
    """Parse the uploaded CSV, insert each row, and render a per-row status
    report. Single-pass — earlier coach rows must appear before the teacher
    rows that reference them via ``assigned_coach_email``.
    """
    # Coach-only — bulk-inserting users into the org has the same blast radius
    # as any other write-to-users route in this app, and no existing route lets
    # principal or district viewers write. `_require_coach` matches the rest
    # of the write-path pattern; using `_deny_teacher` here (the earlier draft)
    # let principal/district POST rosters, which they shouldn't.
    _require_coach(_current_viewer(request), "User upload")
    import csv
    import io

    # Cap the upload so a 500MB file (accidental or malicious) can't OOM the
    # worker. 4MB is generous for a roster CSV — a district of 10k teachers
    # at ~200 bytes/row is 2MB.
    MAX_UPLOAD_BYTES = 4 * 1024 * 1024
    raw = await csv_file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"CSV is larger than {MAX_UPLOAD_BYTES // (1024*1024)}MB. "
            f"Split it into batches and re-upload.",
        )
    # UTF-8 with BOM handling (Excel exports) → fall back to Latin-1 so a
    # malformed encoding still parses instead of 500ing on the admin.
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    # Normalize header names — Excel / Sheets exports often produce headers
    # like ``Name`` or ``Email `` (trailing space from a Sheets column). Strip
    # and lowercase so the row.get() calls below match consistently regardless
    # of how the CSV was authored. csv.DictReader's ``fieldnames`` is
    # mutable, so we override it after construction rather than rebuilding
    # the CSV text.
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [(h or "").strip().lower() for h in reader.fieldnames]

    results = []
    conn = db_connect(DB_PATH)
    try:
        org_id = _SEEDED_IDS["org_id"]
        # Pre-fetch coach email → user_id lookup so teacher rows can attach.
        # Refreshed after each successful coach insert so a CSV with coaches
        # and their teachers in the same file still works. Keys are lowercased
        # so an assigned_coach_email of ``Alex@District.org`` still matches an
        # existing coach stored as ``alex@district.org``.
        coach_lookup = {
            (r["email"] or "").lower(): r["id"]
            for r in conn.execute(
                "SELECT id, email FROM users WHERE org_id = ? AND role = 'coach' AND is_active = 1",
                (org_id,),
            ).fetchall()
        }

        for lineno, row in enumerate(reader, start=2):  # header is line 1
            name = (row.get("name") or "").strip()
            email = (row.get("email") or "").strip() or None
            role = (row.get("role") or "").strip().lower()
            employee_id = (row.get("employee_id") or "").strip() or None
            assigned_coach_email = (row.get("assigned_coach_email") or "").strip() or None
            grade_levels = _split_csv_list(row.get("grade_levels"))
            subjects = _split_csv_list(row.get("subjects"))

            entry = {
                "line": lineno, "name": name, "email": email, "role": role,
                "status": None, "note": "",
            }

            if not name:
                entry["status"] = "error"
                entry["note"] = "'name' column is required"
                results.append(entry)
                continue
            if role not in ("teacher", "coach", "principal", "district"):
                entry["status"] = "error"
                entry["note"] = f"unknown role: {role!r} (expected teacher / coach / principal / district)"
                results.append(entry)
                continue

            try:
                if role == "teacher":
                    coach_user_id = None
                    if assigned_coach_email:
                        # Case-insensitive lookup — see coach_lookup construction.
                        coach_user_id = coach_lookup.get(assigned_coach_email.lower())
                        if not coach_user_id:
                            entry["status"] = "error"
                            entry["note"] = (
                                f"assigned_coach_email {assigned_coach_email!r} not found among active coaches "
                                f"(list the coach earlier in the CSV, or add them first)"
                            )
                            results.append(entry)
                            continue
                    tid, was_created = bulk_create_teacher(
                        conn, org_id=org_id, name=name,
                        email=email, employee_id=employee_id,
                        assigned_coach_user_id=coach_user_id,
                        grade_levels=grade_levels, subjects=subjects,
                    )
                    entry["status"] = "created" if was_created else "already on file"
                    entry["id"] = tid

                else:
                    # coach / principal / district → users table
                    if not email:
                        entry["status"] = "error"
                        entry["note"] = f"'email' column is required for role={role!r}"
                        results.append(entry)
                        continue
                    db_role = _CSV_ROLE_TO_DB_ROLE[role]
                    # Check for an existing user first so we can refuse a
                    # role-conflict — otherwise `get_or_create_user` would
                    # silently return the existing user id regardless of role,
                    # and a later teacher row referencing this email would
                    # attach its `assigned_coach_user_id` to what the DB knows
                    # as an admin, not a coach.
                    existing = conn.execute(
                        "SELECT id, role FROM users WHERE org_id = ? AND email = ?",
                        (org_id, email),
                    ).fetchone()
                    if existing and existing["role"] != db_role:
                        entry["status"] = "error"
                        entry["note"] = (
                            f"a user with email {email!r} already exists as "
                            f"role={existing['role']!r}; refusing to reuse them "
                            f"as role={role!r}"
                        )
                        results.append(entry)
                        continue
                    uid = get_or_create_user(
                        conn, org_id=org_id, email=email, name=name, role=db_role,
                    )
                    entry["status"] = "ok"
                    entry["id"] = uid
                    if role == "coach":
                        # Refresh the lookup so later teacher rows in this batch
                        # can reference the coach we just added.
                        coach_lookup[email.lower()] = uid
                    if role != "coach":
                        entry["note"] = (
                            f"role {role!r} stored as 'admin' at the storage layer "
                            f"(principal/district are cookie-side roles in this build)"
                        )

            except ArchivedTeacherError as e:
                entry["status"] = "error"
                entry["note"] = str(e)
            except ValueError as e:
                # Cross-match ambiguity in bulk_create_teacher, and any other
                # validation failure that surfaces as a plain ValueError from
                # helpers below. Surface the message directly.
                entry["status"] = "error"
                entry["note"] = str(e)
            except sqlite3.IntegrityError as e:
                entry["status"] = "error"
                entry["note"] = f"integrity error: {e}"
            except Exception as e:  # pragma: no cover — defensive
                entry["status"] = "error"
                entry["note"] = f"{type(e).__name__}: {e}"

            results.append(entry)

    finally:
        conn.close()

    totals = {
        "created": sum(1 for r in results if r["status"] == "created"),
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "already_on_file": sum(1 for r in results if r["status"] == "already on file"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "total": len(results),
    }

    return TEMPLATES.TemplateResponse("users_upload.html", {
        "request": request,
        "results": results,
        "totals": totals,
    })


@app.post("/goals/{goal_id}/attach-cycle")
def attach_goal_cycle_route(
    request: Request,
    goal_id: str,
    teacher_id: str = Form(...),
    coaching_cycle_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach-only: attaching a goal to a cycle drives cycle-scoped impact
    rollups; a mis-attach would corrupt the growth story math. Same-teacher
    enforced (never allow cross-teacher goal↔cycle wiring).
    """
    _require_coach(_current_viewer(request), "Attach goal to cycle")
    _guard_goal_write(request, goal_id, "Attach goal to cycle")
    _guard_teacher_write(request, teacher_id, "Attach goal to cycle")
    conn = db_connect(DB_PATH)
    try:
        goal_row = conn.execute(
            "SELECT teacher_id FROM professional_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if not goal_row:
            raise HTTPException(404, "Goal not found")
        if coaching_cycle_id:
            cyc_row = conn.execute(
                "SELECT teacher_id FROM coaching_cycles WHERE id = ?", (coaching_cycle_id,)
            ).fetchone()
            if not cyc_row or cyc_row["teacher_id"] != goal_row["teacher_id"]:
                raise HTTPException(400, "Goal and cycle belong to different teachers.")
        attach_goal_to_cycle(
            conn, goal_id=goal_id, cycle_id=coaching_cycle_id or None,
        )
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{goal_row['teacher_id']}", status_code=303)


@app.post("/goals/{goal_id}/agree")
def agree_goal_route(request: Request, goal_id: str, teacher_id: str = Form(...)) -> RedirectResponse:
    """Goal agreement: the teacher (on their own goals) or the coach can
    move a proposed goal to active. Anyone else is denied. Teacher-id used
    only for the redirect target is validated against the goal's owner.
    """
    _guard_goal_write(request, goal_id, "Goal agree")
    conn = db_connect(DB_PATH)
    try:
        goal_row = conn.execute(
            "SELECT teacher_id FROM professional_goals WHERE id = ?", (goal_id,)
        ).fetchone()
        if not goal_row:
            raise HTTPException(404, "Goal not found")
        from pipeline.db import GoalTransitionError
        try:
            agree_goal(conn, goal_id)
        except GoalTransitionError as e:
            raise HTTPException(409, str(e))
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{goal_row['teacher_id']}", status_code=303)


@app.post("/goals/{goal_id}/close")
async def close_goal_route(
    goal_id: str,
    request: Request,
    teacher_id: str = Form(...),
    outcome: Optional[str] = Form(None),
    outcome_reason: Optional[str] = Form(None),
    outcome_notes: Optional[str] = Form(None),
    carry_forward: Optional[str] = Form(None),
) -> RedirectResponse:
    """Close a goal with structured semantics.

    The success-indicator checklist arrives as multiple form fields named
    ``indicator_met[<idx>]``. Coach can override the derived outcome via
    ``outcome`` (dropdown); if not provided, we derive from the checklist:
        all met  → closed_met
        some met → closed_partial
        none met → closed_unmet
    """
    form = await request.form()

    _require_coach(_current_viewer(request), "Goal close")
    _guard_goal_write(request, goal_id, "Goal close")
    conn = db_connect(DB_PATH)
    try:
        goal_row = conn.execute(
            "SELECT success_indicators, coaching_cycle_id, teacher_id FROM professional_goals WHERE id = ?",
            (goal_id,),
        ).fetchone()
        if not goal_row:
            raise HTTPException(404, "Goal not found")
        # Ignore the form-supplied teacher_id; use the goal's actual owner
        # for the redirect target. Prevents a mistyped/stale form from
        # writing the close to one goal but redirecting to another teacher.
        teacher_id = goal_row["teacher_id"]
        indicators = []
        if goal_row["success_indicators"]:
            try:
                indicators = json.loads(goal_row["success_indicators"])
            except Exception:
                indicators = []

        # Extract checkbox state
        met = []
        for i, ind in enumerate(indicators):
            if form.get(f"indicator_met_{i}"):
                met.append(ind)

        # Derive outcome if not explicitly set
        if not outcome or outcome == "auto":
            if indicators:
                if len(met) == len(indicators):
                    outcome = "closed_met"
                elif len(met) > 0:
                    outcome = "closed_partial"
                else:
                    outcome = "closed_unmet"
            else:
                outcome = "closed_partial"  # no indicators to check — coach must pick manually next time

        close_goal(
            conn, goal_id, outcome,
            indicators_met=met if indicators else None,
            outcome_reason=(outcome_reason or None) if outcome == "closed_unmet" else None,
            outcome_notes=(outcome_notes or None),
        )

        # Carry-forward: seed a new proposed goal linked to the source.
        if carry_forward and outcome in ("closed_partial", "closed_unmet"):
            carry_forward_goal(
                conn, from_goal_id=goal_id,
                new_coaching_cycle_id=goal_row["coaching_cycle_id"],
            )
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.post("/actions/{action_id}/goal")
def set_action_goal_route(
    request: Request,
    action_id: str,
    goal_id: str = Form(""),
) -> RedirectResponse:
    """Coach links (or unlinks) a bite-sized action to a specific goal.
    Coach-only. Same-teacher enforced: a cross-teacher goal_id would silently
    cross-wire the impact rollup — same class of bug as attach-cycle.
    """
    _require_coach(_current_viewer(request), "Action → goal link")
    _guard_action_write(request, action_id, "Action → goal link")
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT teacher_id FROM bite_sized_action_tracking WHERE id = ?", (action_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
        # Empty string clears the tie; a non-empty id must be on the same teacher.
        if goal_id:
            goal_row = conn.execute(
                "SELECT teacher_id FROM professional_goals WHERE id = ?", (goal_id,)
            ).fetchone()
            if not goal_row or goal_row["teacher_id"] != row["teacher_id"]:
                raise HTTPException(400, "Action and goal belong to different teachers.")
        from pipeline.db import set_action_goal
        set_action_goal(conn, action_id=action_id, goal_id=(goal_id or None))
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{row['teacher_id']}", status_code=303)


@app.post("/teachers/{teacher_id}/private-notes")
def add_private_note_route(
    request: Request,
    teacher_id: str,
    body: str = Form(...),
    observation_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach-only: private notes are the coach's scratchpad about the teacher.
    A teacher-role write here would forge coach-authored notes on their own
    or another teacher's record.
    """
    _require_coach(_current_viewer(request), "Private notes")
    viewer = _guard_teacher_write(request, teacher_id, "Private notes")
    if not body.strip():
        raise HTTPException(400, "Note body required")
    conn = db_connect(DB_PATH)
    try:
        # _refuse_if_archived raises HTTPException(409) on an archived teacher.
        # It MUST be inside the try/finally, otherwise the raise short-circuits
        # the outer body and conn.close() never fires — leaking a sqlite3.Connection
        # each time a coach posts to an archived-teacher URL.
        _refuse_if_archived(conn, teacher_id, "Private note")
        add_coach_private_note(
            conn,
            org_id=viewer.get("org_id") or _SEEDED_IDS["org_id"],
            author_user_id=_viewer_uid(viewer),
            teacher_id=teacher_id,
            body=body.strip(),
            observation_id=observation_id or None,
        )
    finally:
        conn.close()
    # If the note was tied to an observation, return there; otherwise the teacher page.
    if observation_id:
        return RedirectResponse(url=f"/observations/{observation_id}", status_code=303)
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.post("/teachers/{teacher_id}/private-notes/{note_id}/delete")
def delete_private_note_route(
    request: Request,
    teacher_id: str,
    note_id: str,
    next: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach-only: delete one of the coach's own private notes. Hard delete —
    the note is the coach's scratchpad, not a record.

    Author guard is enforced at the storage layer: the note is removed only
    when its ``author_user_id`` matches the caller. Today, single-user auth
    means every coach viewer IS the author; the guard is defense-in-depth
    for a future multi-coach world.
    """
    _require_coach(_current_viewer(request), "Private-note delete")
    viewer = _guard_teacher_write(request, teacher_id, "Private-note delete")
    conn = db_connect(DB_PATH)
    try:
        delete_private_note(
            conn, note_id=note_id, author_user_id=_viewer_uid(viewer)
        )
    finally:
        conn.close()
    dest = next if (next and _is_safe_same_origin_path(next)) else f"/teachers/{teacher_id}"
    return RedirectResponse(url=dest, status_code=303)


# ---------------------------------------------------------------------------
# Archive / restore — teachers and observations.
#
# Teachers archive with a cascade (see pipeline.db.archive_teacher). Restore
# un-archives but does not un-cascade — a restored teacher starts fresh.
# Observations archive using the existing `deleted_at` slot; aggregates
# filter it out but by-id lookups still work so the coach can restore.
# ---------------------------------------------------------------------------


@app.post("/teachers/{teacher_id}/archive")
def archive_teacher_route(
    request: Request, teacher_id: str
) -> RedirectResponse:
    _require_coach(_current_viewer(request), "Teacher archive")
    _guard_teacher_write(request, teacher_id, "Teacher archive")
    conn = db_connect(DB_PATH)
    try:
        summary = archive_teacher(conn, teacher_id=teacher_id)
    finally:
        conn.close()
    # Roster is the natural landing after archive — the teacher's hub still
    # loads (by-id lookups aren't filtered), but the coach is done with it
    # for now. Carry a tiny summary in the query string for the toast.
    from urllib.parse import urlencode
    q = urlencode({
        "archived": teacher_id,
        "cycles_closed": summary["cycles_closed"],
        "goals_abandoned": summary["goals_abandoned"],
        "plans_archived": summary["plans_archived"],
    })
    return RedirectResponse(url=f"/teachers?{q}", status_code=303)


@app.post("/teachers/{teacher_id}/restore")
def restore_teacher_route(
    request: Request, teacher_id: str
) -> RedirectResponse:
    _require_coach(_current_viewer(request), "Teacher restore")
    _guard_teacher_write(request, teacher_id, "Teacher restore")
    conn = db_connect(DB_PATH)
    try:
        restore_teacher(conn, teacher_id=teacher_id)
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{teacher_id}", status_code=303)


@app.post("/observations/{observation_id}/archive")
def archive_observation_route(
    request: Request, observation_id: str
) -> RedirectResponse:
    """Soft-delete an observation. Mis-uploads (wrong teacher, bad video,
    uploaded twice) drop out of aggregates. Related rows stay (the coach's
    published move, the action tracking row, the HLM response) — keeping
    the trail matters for audit; aggregate queries join through the
    observation and filter ``o.deleted_at IS NULL``.
    """
    _require_coach(_current_viewer(request), "Observation archive")
    _guard_obs_write(request, observation_id, "Observation archive")
    conn = db_connect(DB_PATH)
    try:
        # Get teacher_id for the redirect back to their hub — that's where
        # the coach was, and where they'll notice the observation now missing.
        row = conn.execute(
            "SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Observation not found")
        archive_observation(conn, observation_id=observation_id)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/teachers/{row['teacher_id']}?archived_obs={observation_id}",
        status_code=303,
    )


@app.post("/observations/{observation_id}/restore")
def restore_observation_route(
    request: Request, observation_id: str
) -> RedirectResponse:
    _require_coach(_current_viewer(request), "Observation restore")
    _guard_obs_write(request, observation_id, "Observation restore")
    conn = db_connect(DB_PATH)
    try:
        restore_observation(conn, observation_id=observation_id)
    finally:
        conn.close()
    return RedirectResponse(
        url=f"/observations/{observation_id}", status_code=303
    )


# ---------------------------------------------------------------------------
# Auth: magic-link sign-in, sign-out, dev mail viewer.
#
# Flow the user experiences:
#   1. Land on any gated page while not signed in → bounced to /signin?next=…
#   2. Type email → POST /signin → we mint a token, queue an email, show the
#      "check your email" page.
#   3. Click the link in the email → GET /auth/{token} → we mint a session,
#      set the HttpOnly cookie, redirect to `next` or the dashboard.
#   4. POST /signout → revoke session, clear cookie.
#
# During pilot/dev, /dev/mail lists the recent outbound_mail rows so the
# clickable magic link is one hop away from the /signin page — no SMTP
# needed to test the loop.
# ---------------------------------------------------------------------------


@app.get("/signin", response_class=HTMLResponse)
def signin_page(
    request: Request, next: Optional[str] = None, error: Optional[str] = None,
) -> HTMLResponse:
    # If already signed in, kick them where they were headed (or the dashboard).
    v = _current_viewer(request)
    if v.get("role") != "anon":
        dest = next if (next and _is_safe_same_origin_path(next)) else "/"
        return RedirectResponse(url=dest, status_code=303)
    return TEMPLATES.TemplateResponse("signin.html", {
        "request": request,
        "next": next if (next and _is_safe_same_origin_path(next)) else "",
        "error": error,
        "dev_login_enabled": DEV_LOGIN_ENABLED,
    })


@app.post("/signin")
def signin_submit(
    request: Request,
    email: str = Form(...),
    next: Optional[str] = Form(None),
) -> HTMLResponse:
    """Mint a magic-link token, queue the sign-in email, land on the
    "check your email" page. We show the same acknowledgment page whether
    or not the email is on file — telling an unknown email that they don't
    have an account is a free account-enumeration oracle.
    """
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return RedirectResponse(
            url=f"/signin?error=bad_email&next={next or ''}", status_code=303,
        )
    safe_next = next if (next and _is_safe_same_origin_path(next)) else "/"
    conn = db_connect(DB_PATH)
    try:
        token, user_id = create_magic_link_token(
            conn, email=email, next_url=safe_next,
            ip=(request.client.host if request.client else None),
        )
        base = _base_url_for_email(request)
        link = f"{base}/auth/{token}"
        subject = "Sign in to Classroom Observer"
        body = (
            f"Click to sign in — this link works for {MAGIC_LINK_TTL_MINUTES} minutes:\n\n"
            f"{link}\n\n"
            f"If you didn't ask to sign in, you can ignore this."
        )
        # Always queue — even for unknown emails — so an attacker probing
        # doesn't see a shorter round-trip on "email not found".
        queue_email(
            conn, to_email=email, subject=subject, body_text=body,
            related_token_id=token,
        )
        # Log the link server-side so pilot/dev can find it without SMTP.
        # Never log for unknown emails (avoids putting typo'd addresses in
        # the log; the outbound_mail row is enough).
        if user_id:
            import logging
            logging.getLogger("uvicorn.error").info(
                "Magic link for %s → %s (expires in %d min)",
                email, link, MAGIC_LINK_TTL_MINUTES,
            )
    finally:
        conn.close()
    return TEMPLATES.TemplateResponse("signin_check_email.html", {
        "request": request, "email": email,
    })


@app.get("/auth/{token}", response_class=HTMLResponse)
def auth_consume(request: Request, token: str) -> HTMLResponse:
    """Consume a magic-link token, mint a session, set the cookie, redirect."""
    conn = db_connect(DB_PATH)
    try:
        result = consume_magic_link_token(conn, token=token)
        if not result:
            return RedirectResponse(
                url="/signin?error=bad_link", status_code=303,
            )
        sid = create_session(
            conn, user_id=result["user_id"],
            ip=(request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent", "")[:512],
            magic_link_token_id=token,
        )
    finally:
        conn.close()
    dest = result["next_url"] if (result["next_url"] and _is_safe_same_origin_path(result["next_url"])) else "/"
    resp = RedirectResponse(url=dest, status_code=303)
    # HttpOnly: JS can't read the session cookie (XSS payload can't lift it).
    # SameSite=lax: cross-origin GET navigation (following email link) still
    # carries the cookie; cross-origin POST does not.
    # secure=True is set only when the request scheme was HTTPS — a local
    # http:// pilot must still work.
    is_https = request.url.scheme == "https"
    resp.set_cookie(
        SESSION_COOKIE, sid,
        max_age=SESSION_TTL_DAYS * 24 * 3600,
        path="/", httponly=True, samesite="lax", secure=is_https,
    )
    return resp


@app.post("/signout")
def signout(request: Request) -> RedirectResponse:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        conn = db_connect(DB_PATH)
        try:
            revoke_session(conn, session_id=sid)
        finally:
            conn.close()
    resp = RedirectResponse(url="/signin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/dev/mail", response_class=HTMLResponse)
def dev_mail_viewer(request: Request) -> HTMLResponse:
    """Dev-only: recent outbound mail. Refuses unless OBSERVER_DEV_LOGIN=1
    is set — otherwise this would be an inbox-peek oracle in production.
    """
    if not DEV_LOGIN_ENABLED:
        raise HTTPException(404, "Not found")
    conn = db_connect(DB_PATH)
    try:
        rows = list_recent_outbound_mail(conn, limit=30)
    finally:
        conn.close()
    base = _base_url_for_email(request)
    for r in rows:
        # Show a clickable link when the mail is a magic-link email.
        if r.get("related_token_id"):
            r["magic_link"] = f"{base}/auth/{r['related_token_id']}"
    return TEMPLATES.TemplateResponse("dev_mail.html", {
        "request": request, "mails": rows,
    })


# ---------------------------------------------------------------------------
# Viewer switching + two-sided negotiation routes
# ---------------------------------------------------------------------------


@app.post("/whoami/switch")
def switch_viewer(
    request: Request,
    role: str = Form(...),
    teacher_id: Optional[str] = Form(None),
    redirect_to: Optional[str] = Form(None),
) -> RedirectResponse:
    """Set the viewer cookie and redirect somewhere sensible.

    Destination priority:
      1. explicit ``redirect_to`` form field (must be a same-origin path starting with '/')
      2. Referer header (kept only its path)
      3. sensible default: teacher-view for teacher role, dashboard for coach

    role='coach' clears the teacher scope; role='teacher' requires teacher_id.

    Dev-only: refuses when OBSERVER_DEV_LOGIN is unset, so a stray form
    submit in production can't flip role.
    """
    if not DEV_LOGIN_ENABLED:
        raise HTTPException(404, "Not found")
    if role == "coach":
        cookie_value = "coach"
    elif role == "principal":
        cookie_value = "principal"
    elif role == "district":
        cookie_value = "district"
    elif role == "teacher" and teacher_id:
        cookie_value = f"teacher:{teacher_id}"
    else:
        raise HTTPException(400, "Bad role/teacher_id")

    # Determine if this is a ROLE CHANGE (different cookie identity). If so,
    # Referer isn't right — the page they were on probably doesn't make sense
    # for the new role. Explicit ``redirect_to`` still wins.
    current_cookie = request.cookies.get(VIEWER_COOKIE) or "coach"
    role_is_changing = cookie_value != current_cookie

    dest = None
    if redirect_to and _is_safe_same_origin_path(redirect_to):
        dest = redirect_to
    if not dest and not role_is_changing:
        ref = request.headers.get("referer", "")
        for prefix in ("http://", "https://"):
            if ref.startswith(prefix):
                slash = ref.find("/", len(prefix))
                if slash > 0:
                    dest = ref[slash:]
                break
    if not dest:
        if role == "teacher":
            dest = f"/teachers/{teacher_id}/teacher-view"
        elif role == "principal":
            dest = "/principal"
        elif role == "district":
            dest = "/district"
        else:
            dest = "/"

    resp = RedirectResponse(url=dest, status_code=303)
    # httponly=True: no client-side JS needs to read this cookie, and setting
    # it defense-in-depth blunts an XSS payload that would otherwise read or
    # rewrite the viewer role. samesite=strict prevents a cross-site auto-
    # submit from flipping the viewer role from another origin (the write
    # gates would catch the follow-up mutations too, but no reason to let
    # the flip happen in the first place).
    resp.set_cookie(VIEWER_COOKIE, cookie_value, max_age=365 * 24 * 3600,
                    httponly=True, samesite="strict")
    return resp


@app.post("/actions/{action_id}/teacher-account")
def action_set_teacher_account(request: Request, action_id: str, teacher_account: str = Form(...)) -> RedirectResponse:
    """Teacher (or coach on the teacher's behalf) records their own account of trying the action."""
    _guard_action_write(request, action_id, "Teacher account on action")
    conn = db_connect(DB_PATH)
    try:
        set_teacher_account_on_action(conn, action_id=action_id, teacher_account=teacher_account.strip())
        row = conn.execute(
            "SELECT source_observation_id, teacher_id FROM bite_sized_action_tracking WHERE id = ?",
            (action_id,),
        ).fetchone()
    finally:
        conn.close()
    # Return whichever context the writer likely came from.
    referer = request.headers.get("referer", "")
    if "/teacher-view" in referer and row:
        return RedirectResponse(url=f"/teachers/{row['teacher_id']}/teacher-view", status_code=303)
    if row:
        return RedirectResponse(url=f"/observations/{row['source_observation_id']}#actions", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/observations/{observation_id}/hlm-response")
def obs_hlm_response(
    request: Request,
    observation_id: str,
    response_type: str = Form(...),
    teacher_note: Optional[str] = Form(None),
) -> RedirectResponse:
    """Teacher records their response to the COACH'S published move.

    Requires that a coach's move has been published for this observation —
    the teacher never negotiates with AI output directly, only with what the
    coach chose to publish.

    Teacher-role only: if a coach POSTed here their user_id would land in
    ``hlm_responses.teacher_user_id``, misattributing the response and
    silently closing an attention item the teacher never touched. Refuse
    everyone but the observation's own teacher.
    """
    viewer = _guard_obs_write(request, observation_id, "HLM response")
    if viewer.get("role") != "teacher":
        raise HTTPException(
            403,
            "Only the teacher responds to their coach's move. "
            "Coaches use the teacher-view (via /whoami/switch in dev) to submit as the teacher."
        )
    conn = db_connect(DB_PATH)
    try:
        # Confirm the viewer IS this observation's teacher, not just any teacher.
        # (Belt-and-suspenders on top of _guard_obs_write's teacher-access check.)
        obs_row = conn.execute(
            "SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)
        ).fetchone()
        if not obs_row or obs_row["teacher_id"] != viewer.get("teacher_id"):
            raise HTTPException(403, "Not your observation")
        move = get_current_coach_move(conn, observation_id)
        if not move:
            raise HTTPException(409, "Your coach hasn't published a move for this observation yet")
        upsert_hlm_response(
            conn,
            observation_id=observation_id,
            response_type=response_type,
            teacher_note=(teacher_note or None),
            teacher_user_id=_viewer_uid(viewer),
        )
        # Attach the response to the specific published-move version.
        conn.execute(
            "UPDATE hlm_responses SET published_coach_move_id = ? WHERE observation_id = ?",
            (move["id"], observation_id),
        )
        conn.commit()
        row = conn.execute("SELECT teacher_id FROM observations WHERE id = ?", (observation_id,)).fetchone()
    finally:
        conn.close()
    if row:
        return RedirectResponse(url=f"/teachers/{row['teacher_id']}/teacher-view", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.post("/observations/{observation_id}/hlm-response/acknowledge")
def obs_hlm_ack(request: Request, observation_id: str) -> RedirectResponse:
    """Coach acknowledges the teacher's response — closes the attention item."""
    if request.state.viewer["role"] != "coach":
        raise HTTPException(403, "Only the coach can acknowledge")
    viewer = _guard_obs_write(request, observation_id, "HLM response acknowledge")
    conn = db_connect(DB_PATH)
    try:
        acknowledge_hlm_response(conn, observation_id=observation_id, coach_user_id=_viewer_uid(viewer))
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}", status_code=303)


@app.post("/goals/{goal_id}/releasing")
def goal_set_releasing(request: Request, goal_id: str, releasing: str = Form("")) -> RedirectResponse:
    """Set Bridges' 'ending' text on a goal — what the teacher is setting aside."""
    _guard_goal_write(request, goal_id, "Goal releasing")
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute("SELECT teacher_id FROM professional_goals WHERE id = ?", (goal_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        set_goal_releasing(conn, goal_id=goal_id, releasing=(releasing.strip() or None))
    finally:
        conn.close()
    return RedirectResponse(url=f"/teachers/{row['teacher_id']}", status_code=303)


# ---------------------------------------------------------------------------
# Coach's published move — the coach's edited version of the AI's suggestion.
# Teacher never sees the raw AI move; only this.
# ---------------------------------------------------------------------------


@app.post("/observations/{observation_id}/coach-move/publish")
def obs_publish_coach_move(
    request: Request,
    observation_id: str,
    move_text: str = Form(...),
    gbf_step_id: Optional[str] = Form(None),
    related_core_teacher_skill: Optional[str] = Form(None),
    derived_from_ai: Optional[str] = Form(None),
) -> RedirectResponse:
    """Coach publishes a new move for the teacher (creates or supersedes)."""
    if request.state.viewer["role"] != "coach":
        raise HTTPException(403, "Only the coach publishes moves")
    viewer = _guard_obs_write(request, observation_id, "Coach move publish")
    if not move_text.strip():
        raise HTTPException(400, "move_text required")
    conn = db_connect(DB_PATH)
    try:
        try:
            publish_coach_move(
                conn,
                observation_id=observation_id,
                move_text=move_text.strip(),
                gbf_step_id=(gbf_step_id or None),
                related_core_teacher_skill=(related_core_teacher_skill or None),
                derived_from_ai=bool(derived_from_ai),
                published_by_user_id=_viewer_uid(viewer),
            )
        except sqlite3.IntegrityError as e:
            # `uq_pcm_current` caught a concurrent publish for the same
            # observation — two coach tabs or two coaches raced. The other
            # writer's move stands; surface as 409 rather than a 500 with a
            # raw traceback. Note that publish_coach_move commits per statement
            # so any partial state (superseded_at flip, hlm ack null) may have
            # landed for the losing branch — the winning branch's supersede
            # will overwrite. The ack-null side effect is deliberately
            # idempotent (already null after the winner's supersede).
            if "uq_pcm_current" in str(e):
                raise HTTPException(
                    409,
                    "Another publish landed first — reload the observation and re-review the current move."
                )
            raise
    finally:
        conn.close()
    return RedirectResponse(url=f"/observations/{observation_id}", status_code=303)


@app.post("/coach-moves/{move_id}/edit")
def obs_edit_coach_move(
    request: Request,
    move_id: str,
    move_text: str = Form(...),
    gbf_step_id: Optional[str] = Form(None),
    related_core_teacher_skill: Optional[str] = Form(None),
) -> RedirectResponse:
    """In-place edit of the current published move (no new version)."""
    if request.state.viewer["role"] != "coach":
        raise HTTPException(403, "Only the coach edits moves")
    if not move_text.strip():
        raise HTTPException(400, "move_text required")
    # Move id → observation id → teacher id → access check. Same posture as
    # other entity-scoped writes.
    conn = db_connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT observation_id FROM published_coach_moves WHERE id = ?", (move_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404)
    finally:
        conn.close()
    _guard_obs_write(request, row["observation_id"], "Coach move edit")
    conn = db_connect(DB_PATH)
    try:
        edit_coach_move(
            conn, move_id=move_id, move_text=move_text.strip(),
            gbf_step_id=(gbf_step_id or None),
            related_core_teacher_skill=(related_core_teacher_skill or None),
        )
        return RedirectResponse(url=f"/observations/{row['observation_id']}", status_code=303)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Principal dashboard — school-level rollup + coach effectiveness + AI oversight
# ---------------------------------------------------------------------------


@app.get("/principal", response_class=HTMLResponse)
def principal_dashboard(request: Request) -> HTMLResponse:
    """School dashboard: aggregate patterns, coach effectiveness, AI oversight.

    Ordered by the principal's own priority (from workflow interview):
      1. School-wide patterns (common weak domain, rating trajectory)
      2. Coach effectiveness (caseload health, cycle completion, AI usage)
      3. Teachers at a glance (drill-down entry point)
      4. Recent AI-coach interactions — principals see AI raw reads for oversight.
    """
    _deny_teacher(_current_viewer(request), "Principal dashboard")
    from collections import Counter as _Counter
    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    conn = db_connect(DB_PATH)
    try:
        # School-wide rubric ratings distribution across published reports.
        rating_counts = {d: _Counter() for d in rubric.domains}
        for row in conn.execute(
            """SELECT rv.domain_assessments FROM report_versions rv
               JOIN observations o ON o.id = rv.observation_id
               WHERE o.status = 'complete' AND o.deleted_at IS NULL
                 AND rv.published_at IS NOT NULL"""
        ):
            try:
                das = json.loads(row["domain_assessments"] or "[]")
            except Exception:
                continue
            for da in das:
                d = da.get("domain")
                r = da.get("overall_rating")
                if d in rating_counts and r:
                    rating_counts[d][r] += 1

        # Weakest domain = highest % of {Ineffective + Minimally Effective}.
        weakest = None
        for d in rubric.domains:
            counts = rating_counts[d]
            total = sum(counts.values())
            if total == 0:
                continue
            weak = counts.get("Ineffective", 0) + counts.get("Minimally Effective", 0)
            pct = round(weak / total * 100, 1)
            if not weakest or pct > weakest["pct"]:
                weakest = {"domain": d, "pct": pct, "n_weak": weak, "n_total": total}

        # Per-teacher deltas: first-obs vs. last-obs on each domain.
        deltas_by_domain = {d: {"up": 0, "flat": 0, "down": 0, "teachers": []} for d in rubric.domains}
        for t in conn.execute("SELECT id, name FROM teachers WHERE archived_at IS NULL").fetchall():
            movement = rubric_score_movement_for_teacher(conn, t["id"])
            if len(movement) < 2:
                continue
            first, last = movement[0], movement[-1]
            for d in rubric.domains:
                r1 = first["ratings"].get(d)
                r2 = last["ratings"].get(d)
                if not r1 or not r2:
                    continue
                delta = rubric.score_for(r2) - rubric.score_for(r1)
                slot = "up" if delta > 0 else ("down" if delta < 0 else "flat")
                deltas_by_domain[d][slot] += 1
                deltas_by_domain[d]["teachers"].append({"name": t["name"], "delta": delta})

        # Coach effectiveness.
        coach_stats = []
        for row in conn.execute(
            """SELECT u.id, u.name FROM users u WHERE u.role = 'coach' ORDER BY u.name"""
        ).fetchall():
            uid = row["id"]
            caseload = conn.execute(
                "SELECT COUNT(*) AS c FROM teachers WHERE assigned_coach_user_id = ? AND archived_at IS NULL",
                (uid,),
            ).fetchone()["c"]
            cycles_open = conn.execute(
                "SELECT COUNT(*) AS c FROM coaching_cycles WHERE coach_user_id = ? AND closed_at IS NULL",
                (uid,),
            ).fetchone()["c"]
            cycles_closed = conn.execute(
                "SELECT COUNT(*) AS c FROM coaching_cycles WHERE coach_user_id = ? AND closed_at IS NOT NULL",
                (uid,),
            ).fetchone()["c"]
            moves_pub = conn.execute(
                "SELECT COUNT(*) AS c FROM published_coach_moves WHERE published_by_user_id = ?",
                (uid,),
            ).fetchone()["c"]
            moves_from_ai = conn.execute(
                "SELECT COUNT(*) AS c FROM published_coach_moves WHERE published_by_user_id = ? AND derived_from_ai = 1",
                (uid,),
            ).fetchone()["c"]
            ai_pct = round(moves_from_ai / moves_pub * 100, 0) if moves_pub else None
            coach_stats.append({
                "id": uid, "name": row["name"], "caseload": caseload,
                "cycles_open": cycles_open, "cycles_closed": cycles_closed,
                "moves_published": moves_pub, "moves_from_ai": moves_from_ai,
                "ai_pct": ai_pct,
            })

        # Teachers at-a-glance.
        teachers = [dict(r) for r in conn.execute(
            """SELECT t.id, t.name,
                      (SELECT COUNT(*) FROM observations WHERE teacher_id=t.id AND status='complete' AND deleted_at IS NULL) AS obs_count,
                      (SELECT COUNT(*) FROM coaching_cycles WHERE teacher_id=t.id AND closed_at IS NULL) AS active_cycles,
                      (SELECT COUNT(*) FROM professional_goals WHERE teacher_id=t.id AND status IN ('active','proposed')) AS active_goals
               FROM teachers t
               WHERE t.archived_at IS NULL
               ORDER BY t.name"""
        ).fetchall()]

        # Recent AI-coach interactions for oversight.
        recent_ai = []
        for row in conn.execute(
            """SELECT o.id AS obs_id, o.scored_at, t.name AS teacher_name, t.id AS teacher_id,
                      pcm.id AS move_id, pcm.derived_from_ai, pcm.move_text,
                      pcm.published_at, rv.coaching_recommendations AS cr_json
               FROM observations o
               JOIN teachers t ON t.id = o.teacher_id
               LEFT JOIN published_coach_moves pcm ON pcm.observation_id = o.id AND pcm.superseded_at IS NULL
               LEFT JOIN report_versions rv ON rv.observation_id = o.id AND rv.published_at IS NOT NULL
               WHERE o.status = 'complete'
                 AND o.deleted_at IS NULL AND t.archived_at IS NULL
               ORDER BY o.scored_at DESC LIMIT 8"""
        ).fetchall():
            ai_move_text = None
            try:
                raw = json.loads(row["cr_json"] or "{}")
                if isinstance(raw, dict):
                    ai_move_text = (raw.get("highest_leverage_move") or {}).get("move")
            except Exception:
                pass
            # Rough classifier: identical → accepted, otherwise → edited or fresh
            classification = "no_move" if not row["move_text"] else (
                "accepted_verbatim" if ai_move_text and row["move_text"].strip() == ai_move_text.strip()
                else ("edited_from_ai" if row["derived_from_ai"] else "coach_fresh")
            )
            recent_ai.append({
                "obs_id": row["obs_id"],
                "scored_at": row["scored_at"],
                "teacher_name": row["teacher_name"],
                "teacher_id": row["teacher_id"],
                "ai_move_text": ai_move_text,
                "coach_move_text": row["move_text"],
                "classification": classification,
            })
    finally:
        conn.close()

    return TEMPLATES.TemplateResponse("principal_dashboard.html", {
        "request": request,
        "rubric_domains": rubric.domains,
        "rubric_levels": rubric.rating_levels,
        "rating_counts": {d: dict(c) for d, c in rating_counts.items()},
        "weakest": weakest,
        "deltas_by_domain": deltas_by_domain,
        "coach_stats": coach_stats,
        "teachers": teachers,
        "recent_ai": recent_ai,
    })


# ---------------------------------------------------------------------------
# District dashboard — strategic view: school-by-school + initiative tracking
# ---------------------------------------------------------------------------


@app.get("/district", response_class=HTMLResponse)
def district_dashboard(request: Request) -> HTMLResponse:
    """District director view: strategic patterns across schools.

    Priorities (from workflow interview):
      1. School-by-school comparison
      2. District initiative tracking — are teachers/coaches moving on the
         stated priorities named in district_context and district_documents?

    Deliberately NOT here: individual teacher drill-down (principals do that),
    compliance reporting (separate view #6).
    """
    _deny_teacher(_current_viewer(request), "District dashboard")
    from datetime import datetime as _dt, date as _date
    from collections import Counter as _Counter

    rubric = get_rubric(DEFAULT_RUBRIC_ID)
    conn = db_connect(DB_PATH)
    try:
        # Schools: in this data model, each organization ≈ one school.
        # (A future "schools" table under organizations would let one district
        # organization span multiple schools; for MVP, org-per-school.)
        schools = []
        for org in conn.execute(
            "SELECT id, name, slug FROM organizations ORDER BY name"
        ).fetchall():
            oid = org["id"]
            t_count = conn.execute(
                "SELECT COUNT(*) AS c FROM teachers WHERE org_id = ? AND archived_at IS NULL",
                (oid,),
            ).fetchone()["c"]
            obs_count = conn.execute(
                "SELECT COUNT(*) AS c FROM observations o JOIN teachers t ON t.id = o.teacher_id "
                "WHERE t.org_id = ? AND o.status = 'complete' AND o.deleted_at IS NULL AND t.archived_at IS NULL",
                (oid,),
            ).fetchone()["c"]
            active_cycles = conn.execute(
                "SELECT COUNT(*) AS c FROM coaching_cycles c JOIN teachers t ON t.id = c.teacher_id "
                "WHERE t.org_id = ? AND c.closed_at IS NULL",
                (oid,),
            ).fetchone()["c"]
            moves_pub = conn.execute(
                "SELECT COUNT(*) AS c FROM published_coach_moves pcm "
                "JOIN observations o ON o.id = pcm.observation_id "
                "JOIN teachers t ON t.id = o.teacher_id "
                "WHERE t.org_id = ? AND o.deleted_at IS NULL AND t.archived_at IS NULL",
                (oid,),
            ).fetchone()["c"]
            # School-level rating distribution — sum weak-rating counts.
            weak_pct = None
            weakest_domain = None
            for d in rubric.domains:
                counts = _Counter()
                for row in conn.execute(
                    """SELECT rv.domain_assessments FROM report_versions rv
                       JOIN observations o ON o.id = rv.observation_id
                       JOIN teachers t ON t.id = o.teacher_id
                       WHERE t.org_id = ? AND o.status='complete'
                         AND o.deleted_at IS NULL AND t.archived_at IS NULL
                         AND rv.published_at IS NOT NULL""",
                    (oid,),
                ):
                    try:
                        das = json.loads(row["domain_assessments"] or "[]")
                    except Exception:
                        continue
                    for da in das:
                        if da.get("domain") == d and da.get("overall_rating"):
                            counts[da["overall_rating"]] += 1
                total = sum(counts.values())
                if total == 0:
                    continue
                weak = counts.get("Ineffective", 0) + counts.get("Minimally Effective", 0)
                pct = round(weak / total * 100, 1)
                if weak_pct is None or pct > weak_pct:
                    weak_pct = pct
                    weakest_domain = d
            schools.append({
                "id": oid, "name": org["name"], "slug": org["slug"],
                "teachers": t_count, "observations": obs_count,
                "active_cycles": active_cycles, "moves_published": moves_pub,
                "weak_pct": weak_pct, "weakest_domain": weakest_domain,
            })

        # District priorities in force — pull the current year's arc + docs.
        _now = _dt.now(timezone.utc)
        year = f"{_now.year - 1}-{_now.year}" if _now.month < 8 else f"{_now.year}-{_now.year + 1}"
        # For MVP: assume single-org district; pull that org's context.
        first_org = conn.execute("SELECT id FROM organizations LIMIT 1").fetchone()
        district_context = None
        district_docs = []
        if first_org:
            dc_row = conn.execute(
                "SELECT * FROM district_context WHERE org_id = ? AND academic_year = ?",
                (first_org["id"], year),
            ).fetchone()
            if dc_row:
                district_context = dict(dc_row)
                if district_context.get("year_arc_json"):
                    try:
                        district_context["year_arc"] = json.loads(district_context["year_arc_json"])
                    except Exception:
                        district_context["year_arc"] = []
                else:
                    district_context["year_arc"] = []
                # `district_priorities_json` is stored as a JSON string; we
                # replace it with a parsed list under the same key so the
                # engagement loop below can iterate dicts (not characters,
                # which is what iterating a raw JSON string would produce).
                _pjson = district_context.get("district_priorities_json")
                if isinstance(_pjson, str):
                    try:
                        district_context["district_priorities_json"] = json.loads(_pjson)
                    except Exception:
                        district_context["district_priorities_json"] = []
            district_docs = [dict(r) for r in conn.execute(
                """SELECT id, title, doc_type, original_filename, uploaded_at
                   FROM district_documents
                   WHERE org_id = ? AND archived_at IS NULL
                   ORDER BY uploaded_at DESC""",
                (first_org["id"],),
            ).fetchall()]

        # Initiative tracking: prefer exact-match on origin_priority_name (set
        # when a coach starts a goal from a district-priority chip). Fall back
        # to fuzzy keyword-match for un-tagged goals so the earlier data still shows.
        engagement = []
        # Structured (exact-match): count goals per priority name.
        priorities_named = []
        if district_context and district_context.get("district_priorities_json"):
            for p in district_context["district_priorities_json"]:
                priorities_named.append(p["name"] if isinstance(p, dict) else str(p))
        for name in priorities_named:
            # Goals tagged explicitly with this priority
            tagged = conn.execute(
                """SELECT COUNT(*) AS c FROM professional_goals
                   WHERE origin_priority_name = ?
                     AND status IN ('proposed', 'active', 'closed_met', 'closed_partial')""",
                (name,),
            ).fetchone()["c"]
            # Outcomes on the tagged ones
            met = conn.execute(
                "SELECT COUNT(*) AS c FROM professional_goals WHERE origin_priority_name = ? AND status = 'closed_met'",
                (name,),
            ).fetchone()["c"]
            partial = conn.execute(
                "SELECT COUNT(*) AS c FROM professional_goals WHERE origin_priority_name = ? AND status = 'closed_partial'",
                (name,),
            ).fetchone()["c"]
            engagement.append({
                "keyword": name,
                "goal_matches": tagged,
                "closed_met": met,
                "closed_partial": partial,
                "kind": "priority",
            })
        # Retired priorities: names that goals still carry but that no longer
        # appear in the current-year district_priorities_json. Length-change
        # edits (in admin/district-context) intentionally skip the rename
        # cascade to avoid mis-tagging; those goals surface with a "retired
        # priority" badge on the teacher hub. The district loses visibility
        # into their outcomes without this rollup — so pull them here too,
        # labelled distinctly so a viewer sees they're historical.
        current_set = set(priorities_named)
        retired_rows = conn.execute(
            """SELECT origin_priority_name AS name,
                      COUNT(*) AS c,
                      SUM(CASE WHEN status = 'closed_met' THEN 1 ELSE 0 END) AS met,
                      SUM(CASE WHEN status = 'closed_partial' THEN 1 ELSE 0 END) AS partial
               FROM professional_goals
               WHERE origin_priority_name IS NOT NULL
                 AND status IN ('proposed', 'active', 'closed_met', 'closed_partial')
               GROUP BY origin_priority_name""",
        ).fetchall()
        for r in retired_rows:
            if r["name"] in current_set:
                continue  # already reported under priorities_named
            engagement.append({
                "keyword": r["name"],
                "goal_matches": r["c"],
                "closed_met": r["met"] or 0,
                "closed_partial": r["partial"] or 0,
                "kind": "retired_priority",
            })

        # Fallback keyword-match for older goals not tagged with origin_priority_name.
        keywords = set()
        for doc in district_docs:
            title = (doc.get("title") or "").lower()
            for word in title.split():
                w = word.strip(".,()-:;")
                if len(w) >= 4:
                    keywords.add(w)
        if district_context and district_context.get("year_arc"):
            for phase in district_context["year_arc"]:
                name = (phase.get("name") or "").lower()
                for word in name.split():
                    w = word.strip(".,()-:;")
                    if len(w) >= 4:
                        keywords.add(w)
        for kw in sorted(keywords)[:12]:
            match_count = conn.execute(
                """SELECT COUNT(*) AS c FROM professional_goals
                   WHERE status IN ('proposed', 'active')
                     AND origin_priority_name IS NULL
                     AND (LOWER(title) LIKE ? OR LOWER(description) LIKE ?)""",
                (f"%{kw}%", f"%{kw}%"),
            ).fetchone()["c"]
            if match_count > 0:
                engagement.append({
                    "keyword": kw, "goal_matches": match_count, "kind": "keyword",
                })
        # Sort: active priorities first, then retired priorities, then
        # keyword-matches — each group descending by goal_matches.
        _kind_rank = {"priority": 0, "retired_priority": 1, "keyword": 2}
        engagement.sort(key=lambda e: (_kind_rank.get(e.get("kind"), 3), -e["goal_matches"]))

        # District-wide totals for the header.
        total_teachers = conn.execute(
            "SELECT COUNT(*) AS c FROM teachers WHERE archived_at IS NULL"
        ).fetchone()["c"]
        total_schools = len(schools)
        total_active_cycles = sum(s["active_cycles"] for s in schools)
    finally:
        conn.close()

    return TEMPLATES.TemplateResponse("district_dashboard.html", {
        "request": request,
        "schools": schools,
        "district_context": district_context,
        "district_docs": district_docs,
        "engagement": engagement,
        "keywords_scanned": len(keywords),
        "total_teachers": total_teachers,
        "total_schools": total_schools,
        "total_active_cycles": total_active_cycles,
        "academic_year": year,
    })


# ---------------------------------------------------------------------------
# Compliance / audit — observation counts per teacher per academic year
# ---------------------------------------------------------------------------


def _compliance_rows(conn, *, target: int, academic_year: str) -> list:
    """Return per-teacher observation counts scoped to the academic year.

    academic_year format: "2026-2027" (August of first → July of second).

    Status bands:
      on_track = meets or exceeds target
      at_risk  = has SOME observations but under target
      behind   = zero observations, and the teacher was on the roster with
                 enough runway to be observed (created ≥ 21 days before the
                 end of the reporting window, or before the window opened)
      n/a      = zero observations AND recently added — not enough runway
                 to have been observed yet. Distinguishes a mid-year hire /
                 recently-onboarded teacher from a real compliance miss.

    Target must be positive; caller guards this too, but we defend here so
    the "on_track" band can't collapse to always-true on a nonsense target.
    """
    from datetime import date as _date, datetime as _dtc, timedelta as _tdc
    import re as _re_c
    if target is None or target <= 0:
        # Fallback to 6 (the historical default) rather than mislabelling
        # everyone. Caller should already guard, but this keeps the report
        # meaningful even if the guard is bypassed.
        target = 6
    # Validate academic_year shape. Callers pass a URL param through; a
    # bogus "year=foo" would raise ValueError on the split/unpack and later
    # crash `_dtc.fromisoformat`. Reject up front with a caller-friendly error.
    if not _re_c.fullmatch(r"\d{4}-\d{4}", academic_year or ""):
        raise ValueError(f"academic_year must be YYYY-YYYY, got {academic_year!r}")
    start_year, end_year = academic_year.split("-")
    # Half-open interval [year_start, year_end_exclusive): compare with < on
    # the upper bound to a bare next-day date. The prior code compared
    # <= "YYYY-07-31T23:59:59" which is a naive string compare — an ISO
    # timestamp of "2026-07-31T23:59:59+00:00" sorts GREATER than the
    # naked "T23:59:59" boundary (the longer prefix wins), so a record
    # right at end-of-day would be off-by-one excluded. `< next_day` also
    # sidesteps timezone-suffix drift because any tz-attached ISO string
    # is sorted purely by leading "YYYY-MM-DD" before it hits the "T".
    year_start = f"{start_year}-08-01"
    year_end_exclusive = f"{end_year}-08-01"
    year_end = f"{end_year}-07-31"  # kept for the runway_cutoff below
    # Runway threshold: 21 days before "as of today OR window end, whichever
    # is earlier". A teacher created inside this window hasn't had enough
    # time to be observed at the district cadence; they're 'n/a', not behind.
    today = _dtc.now(timezone.utc).date().isoformat()
    horizon = min(today, year_end)
    runway_cutoff = (_dtc.fromisoformat(horizon).date() - _tdc(days=21)).isoformat()

    rows = []
    for t in conn.execute(
        """SELECT t.id, t.name, t.assigned_coach_user_id, t.created_at, o.name AS org_name
           FROM teachers t
           LEFT JOIN organizations o ON o.id = t.org_id
           WHERE t.archived_at IS NULL
           ORDER BY t.name"""
    ).fetchall():
        obs_count = conn.execute(
            """SELECT COUNT(*) AS c FROM observations
               WHERE teacher_id = ? AND status = 'complete' AND deleted_at IS NULL
                 AND COALESCE(observed_at, scored_at, uploaded_at) >= ?
                 AND COALESCE(observed_at, scored_at, uploaded_at) < ?""",
            (t["id"], year_start, year_end_exclusive),
        ).fetchone()["c"]
        date_row = conn.execute(
            """SELECT MIN(COALESCE(observed_at, scored_at, uploaded_at)) AS first_at,
                      MAX(COALESCE(observed_at, scored_at, uploaded_at)) AS last_at
               FROM observations WHERE teacher_id = ? AND status = 'complete'
                 AND deleted_at IS NULL
                 AND COALESCE(observed_at, scored_at, uploaded_at) >= ?
                 AND COALESCE(observed_at, scored_at, uploaded_at) < ?""",
            (t["id"], year_start, year_end_exclusive),
        ).fetchone()
        coach = None
        if t["assigned_coach_user_id"]:
            u = conn.execute(
                "SELECT name FROM users WHERE id = ?", (t["assigned_coach_user_id"],)
            ).fetchone()
            coach = u["name"] if u else None
        if obs_count >= target:
            status = "on_track"
        elif obs_count > 0:
            status = "at_risk"
        else:
            # Zero observations. Distinguish "no runway" from "behind".
            # NULL / empty created_at → treat as "n/a": we can't prove the
            # teacher had runway to be observed, so "behind" would over-
            # accuse. Legacy rows missing created_at surface as n/a rather
            # than getting silently counted as compliance failures.
            created_at = (t["created_at"] or "")[:10]
            if not created_at:
                status = "n/a"
            elif created_at > runway_cutoff:
                status = "n/a"
            else:
                status = "behind"
        rows.append({
            "id": t["id"], "name": t["name"],
            "school": t["org_name"] or "—",
            "coach": coach or "unassigned",
            "obs_count": obs_count, "target": target,
            "first_at": (date_row["first_at"] or "")[:10] if date_row else "",
            "last_at": (date_row["last_at"] or "")[:10] if date_row else "",
            "status": status,
            "created_at": (t["created_at"] or "")[:10],
        })
    return rows


@app.get("/compliance", response_class=HTMLResponse)
def compliance_report(
    request: Request,
    target: Optional[int] = None,
    year: Optional[str] = None,
) -> HTMLResponse:
    """Observation-count compliance report — per teacher per academic year.

    Admin-facing (principal + district). Sortable by column. Export as CSV.

    Target priority: explicit ?target=N → district's observations_per_year_target → 6.
    Default year: current academic year — override with ?year=YYYY-YYYY.
    """
    _deny_teacher(_current_viewer(request), "Compliance report")
    from datetime import datetime as _dt2
    _now2 = _dt2.now(timezone.utc)
    if not year:
        year = f"{_now2.year - 1}-{_now2.year}" if _now2.month < 8 else f"{_now2.year}-{_now2.year + 1}"

    conn = db_connect(DB_PATH)
    try:
        target_source = "url" if target is not None else "district"
        if target is None:
            dc = get_district_context(conn, org_id=_SEEDED_IDS["org_id"], academic_year=year)
            if dc and dc.get("observations_per_year_target"):
                target = int(dc["observations_per_year_target"])
            else:
                target = 6
                target_source = "default"
        # Guard nonsense targets (0, negative, or a URL-supplied string that
        # slipped past int()). Fall back to 6 and note the fallback so the
        # coach can see the report isn't using their bogus input.
        if target <= 0:
            target = 6
            target_source = "fallback_from_bad_target"
        try:
            rows = _compliance_rows(conn, target=target, academic_year=year)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()

    on_track = sum(1 for r in rows if r["status"] == "on_track")
    at_risk = sum(1 for r in rows if r["status"] == "at_risk")
    behind = sum(1 for r in rows if r["status"] == "behind")
    na = sum(1 for r in rows if r["status"] == "n/a")
    total_obs = sum(r["obs_count"] for r in rows)

    return TEMPLATES.TemplateResponse("compliance.html", {
        "request": request,
        "rows": rows,
        "target": target,
        "academic_year": year,
        "totals": {
            "teachers": len(rows), "on_track": on_track, "at_risk": at_risk,
            "behind": behind, "na": na,
            "observations": total_obs,
        },
    })


@app.get("/compliance/export.csv")
def compliance_export(
    request: Request,
    target: int = 6,
    year: Optional[str] = None,
):
    """CSV export of the compliance report — matches district reporting templates."""
    _deny_teacher(_current_viewer(request), "Compliance CSV export")
    import csv
    import io
    import re
    from datetime import datetime as _dt3
    _now3 = _dt3.now(timezone.utc)
    if not year:
        year = f"{_now3.year - 1}-{_now3.year}" if _now3.month < 8 else f"{_now3.year}-{_now3.year + 1}"
    # Guard nonsense targets — same fallback as the HTML report so the CSV
    # and the browser view can never disagree.
    if target <= 0:
        target = 6
    # Sanitize the year token for the filename header (no path traversal,
    # no injection). Anything unusual falls back to a safe generic label.
    safe_year = year if re.fullmatch(r"\d{4}-\d{4}", year or "") else "export"

    conn = db_connect(DB_PATH)
    try:
        try:
            rows = _compliance_rows(conn, target=target, academic_year=year)
        except ValueError as e:
            raise HTTPException(400, str(e))
    finally:
        conn.close()

    def _csv_safe(v):
        """Escape CSV formula-injection payloads. Excel / Sheets evaluates a
        cell whose first character is =, +, -, @, tab, or CR as a formula —
        `=HYPERLINK("http://evil", "click")` in a teacher name would fire
        on open. Prefix a single quote to neutralize, per OWASP guidance.
        """
        if v is None:
            return ""
        s = str(v)
        if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + s
        return s

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "Teacher name", "School", "Assigned coach", "Complete observations",
        "Target", "Status", "First observation", "Most recent observation",
        "Academic year",
    ])
    for r in rows:
        writer.writerow([
            _csv_safe(r["name"]), _csv_safe(r["school"]), _csv_safe(r["coach"]),
            r["obs_count"], r["target"], r["status"].replace("_", " "),
            r["first_at"], r["last_at"], safe_year,
        ])
    csv_bytes = buf.getvalue().encode("utf-8")
    filename = f"compliance-{safe_year}-target{int(target)}.csv"
    from starlette.responses import Response
    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Practice log — micro-attempts between observations
# ---------------------------------------------------------------------------


@app.post("/teachers/{teacher_id}/practice-log")
def add_practice_log_route(
    request: Request,
    teacher_id: str,
    entry_text: str = Form(...),
    action_id: Optional[str] = Form(None),
) -> RedirectResponse:
    """Teacher (or coach on their behalf) records a practice-log entry.

    Auto-links to the teacher's current active cycle. Optional ``action_id``
    ties the entry to a specific bite-sized action being practiced.
    """
    viewer = _guard_teacher_write(request, teacher_id, "Practice log entry")
    if not entry_text.strip():
        raise HTTPException(400, "entry_text required")
    conn = db_connect(DB_PATH)
    try:
        _refuse_if_archived(conn, teacher_id, "Practice log entry")
        row = conn.execute("SELECT org_id FROM teachers WHERE id = ?", (teacher_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        # Validate action_id ownership. A stale form or a mistyped id could
        # otherwise attach this entry to another teacher's action, silently
        # cross-wiring practice-log enrichment (action_skill, related_domain
        # from the join in list_practice_log_for_teacher). Treat any mismatch
        # as "no action" rather than a hard error — the coach may have removed
        # the action since the form loaded, and losing the tie is fine; losing
        # the entry itself would be worse.
        clean_action_id = None
        if action_id:
            action_row = conn.execute(
                "SELECT teacher_id FROM bite_sized_action_tracking WHERE id = ?",
                (action_id,),
            ).fetchone()
            if action_row and action_row["teacher_id"] == teacher_id:
                clean_action_id = action_id
        role = "teacher" if viewer["role"] == "teacher" else "coach"
        add_practice_log_entry(
            conn, org_id=row["org_id"], teacher_id=teacher_id,
            entry_text=entry_text.strip(),
            created_by_role=role,
            action_id=clean_action_id,
        )
    finally:
        conn.close()
    # Route back to whichever surface the caller came from.
    ref = request.headers.get("referer", "")
    if "/teacher-view" in ref:
        return RedirectResponse(url=f"/teachers/{teacher_id}/teacher-view#practice-log", status_code=303)
    return RedirectResponse(url=f"/teachers/{teacher_id}#practice-log", status_code=303)
