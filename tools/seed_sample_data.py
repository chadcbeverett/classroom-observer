#!/usr/bin/env python3
"""Seed the local DB with distinct sample data for the 4 baseline teachers.

Each teacher gets a different scenario so the walkthrough shows the range:

  Summey       Year 2 (early career) — fresh open cycle, one active goal,
                                       observation NOT yet attached to cycle.
  Dulaney      Year 6 (mid career)  — active cycle with observation attached,
                                       one private coach note.
  Lopez        Year 14 (veteran)    — one closed cycle (met) + one fresh
                                       active cycle. Observation on closed one.
  Livingston   Year 8 (mid career)  — active cycle with observation attached.

Plus a district_context row for the current academic year.

Idempotent: wipes any prior sample-added rows for the 4 teachers before re-seeding.
Only touches teacher_profiles / professional_goals / coaching_cycles /
coach_private_notes / district_context / observations.coaching_cycle_id.
Does NOT touch observations, report_versions, or bite_sized_action_tracking.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.db import (
    connect,
    upsert_teacher_profile_teacher_side,
    upsert_teacher_profile_coach_side,
    create_cycle, close_cycle,
    create_goal, agree_goal, close_goal,
    add_coach_private_note,
    upsert_district_context,
    attach_observation_to_cycle,
    attach_goal_to_cycle,
)

DB = ROOT / "reports" / "observations.sqlite"


def _get(conn, sql, *params):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def _list(conn, sql, *params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def wipe_sample_data(conn, teacher_ids: list) -> None:
    if not teacher_ids:
        return
    placeholders = ",".join("?" for _ in teacher_ids)
    # Wipe cycles + goals + private notes + profiles + detach observations
    conn.execute(f"UPDATE observations SET coaching_cycle_id = NULL WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.execute(f"UPDATE professional_goals SET coaching_cycle_id = NULL WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.execute(f"DELETE FROM coach_private_notes WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.execute(f"DELETE FROM professional_goals WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.execute(f"DELETE FROM coaching_cycles WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.execute(f"DELETE FROM teacher_profiles WHERE teacher_id IN ({placeholders})", teacher_ids)
    conn.commit()


def seed_summey(conn, org_id, coach_id, teacher):
    tid = teacher["id"]
    obs = _get(conn, "SELECT id FROM observations WHERE teacher_id = ? LIMIT 1", tid)
    upsert_teacher_profile_teacher_side(
        conn, teacher_id=tid,
        years_teaching_total=2, years_teaching_subject=2, years_at_current_school=1,
        highest_credential="bachelors",
        subjects_taught=["Biology", "Health Science"],
        grade_levels_taught=["9", "10"],
        self_ratings={"classroom_management": 3, "content_expertise": 3,
                      "student_engagement": 3, "formative_assessment": 2,
                      "family_communication": 2},
        coaching_style_preference="warm",
        career_narrative_notes="Career switcher from biotech. First year at this school after year in a charter.",
        career_goals_notes="Grow into a strong biology teacher; explore leading lab-based projects.",
    )
    upsert_teacher_profile_coach_side(
        conn, teacher_id=tid,
        observed_style_notes="High energy, strong content, still building classroom voice for redirects.",
        coach_notes_on_teacher="Growing quickly. Responds well to concrete modeling.",
    )
    cycle_id = create_cycle(
        conn, org_id=org_id, teacher_id=tid, coach_user_id=coach_id,
        title="Year 2 foundations cycle",
        notes=("Six-week focus on foundational procedures and student engagement. "
               "Weekly touch-points. First observation will be next week."),
    )
    goal_id = create_goal(
        conn, org_id=org_id, teacher_id=tid, coaching_cycle_id=cycle_id,
        title="Build predictable transition routines",
        description=("Move from teacher-narrated to student-executed transitions between activities. "
                     "Target 3 predictable transitions per lesson."),
        proposed_by="joint",
        success_indicators=[
            "Most transitions complete in under 90 seconds",
            "Students initiate transitions from a visual cue",
        ],
        teacher_notes="I want to stop losing 5 minutes to every transition.",
        coach_notes="Great candidate for a first quick win.",
        related_rubric_domain="Student Engagement",
        related_core_teacher_skill="Using efficient routines and procedures",
    )
    agree_goal(conn, goal_id)
    # Observation NOT attached — represents the "just opened cycle" state
    return {"cycle_id": cycle_id, "goal_ids": [goal_id]}


def seed_dulaney(conn, org_id, coach_id, teacher):
    tid = teacher["id"]
    obs = _get(conn, "SELECT id FROM observations WHERE teacher_id = ? LIMIT 1", tid)
    upsert_teacher_profile_teacher_side(
        conn, teacher_id=tid,
        years_teaching_total=6, years_teaching_subject=5, years_at_current_school=3,
        highest_credential="masters",
        subjects_taught=["ELA", "Reading Intervention"],
        grade_levels_taught=["7", "8"],
        self_ratings={"classroom_management": 4, "content_expertise": 4,
                      "student_engagement": 3, "formative_assessment": 3,
                      "family_communication": 4},
        coaching_style_preference="direct",
        career_narrative_notes="Traditional teacher-prep path. Third year at this school after 3 in an urban district.",
        career_goals_notes="Interested in eventually leading a PLC or coaching newer teachers.",
    )
    upsert_teacher_profile_coach_side(
        conn, teacher_id=tid,
        observed_style_notes="Warm, high-rapport classroom. Discourse routes strongly through the teacher.",
        coach_notes_on_teacher="Accepts direct feedback well. Wants specific moves, not concepts.",
    )
    cycle_id = create_cycle(
        conn, org_id=org_id, teacher_id=tid, coach_user_id=coach_id,
        title="Fall discourse cycle",
        notes=("Six-week focus on shifting discourse from teacher-mediated to student-to-student. "
               "Observation-heavy: baseline + 2 mid-cycle + close."),
    )
    goal_id = create_goal(
        conn, org_id=org_id, teacher_id=tid, coaching_cycle_id=cycle_id,
        title="Increase peer-to-peer discourse in whole-group discussion",
        description="Move from teacher → student → teacher to student → student exchanges.",
        proposed_by="joint",
        success_indicators=[
            "6+ peer-to-peer exchanges per lesson",
            "3+ scripted peer-response prompts used per lesson",
        ],
        teacher_notes="I want to interrupt my instinct to respond to every student answer.",
        coach_notes="Teacher has the rapport and management foundation — this is a discipline stretch.",
        related_rubric_domain="Academic Ownership",
        related_core_teacher_skill="Providing opportunities for students to respond to and build on their peers' ideas",
    )
    agree_goal(conn, goal_id)
    if obs:
        attach_observation_to_cycle(conn, observation_id=obs["id"], cycle_id=cycle_id)
    add_coach_private_note(
        conn, org_id=org_id, author_user_id=coach_id, teacher_id=tid,
        body=("Working theory: over-affirmation is the block. Every 'I would agree with you' closes the exchange. "
              "Consider a 5-second silence experiment next visit."),
    )
    return {"cycle_id": cycle_id, "goal_ids": [goal_id]}


def seed_lopez(conn, org_id, coach_id, teacher):
    tid = teacher["id"]
    obs = _get(conn, "SELECT id FROM observations WHERE teacher_id = ? LIMIT 1", tid)
    upsert_teacher_profile_teacher_side(
        conn, teacher_id=tid,
        years_teaching_total=14, years_teaching_subject=14, years_at_current_school=8,
        highest_credential="masters",
        subjects_taught=["Character Education", "SEL", "Advisory"],
        grade_levels_taught=["7", "8"],
        self_ratings={"classroom_management": 5, "content_expertise": 5,
                      "student_engagement": 4, "formative_assessment": 3,
                      "family_communication": 5},
        coaching_style_preference="socratic",
        career_narrative_notes="Fourteen years in the district. Athletic coach as well as classroom teacher.",
        career_goals_notes="Focused on deepening reflection and metacognition in students.",
    )
    upsert_teacher_profile_coach_side(
        conn, teacher_id=tid,
        observed_style_notes="Deep student trust. Excellent rapport. Cognitive demand can be shallow.",
        coach_notes_on_teacher="Prefers to arrive at moves via questioning. Push with Socratic prompts, not directives.",
    )
    # Closed cycle first
    closed_cycle_id = create_cycle(
        conn, org_id=org_id, teacher_id=tid, coach_user_id=coach_id,
        title="Spring reflection routines cycle",
        notes=("Six-week focus on establishing peer-response routines in advisory. "
               "Observation-heavy structure."),
    )
    closed_goal_id = create_goal(
        conn, org_id=org_id, teacher_id=tid, coaching_cycle_id=closed_cycle_id,
        title="Establish peer-response routines in advisory circle",
        description="Introduce and normalize student-to-student build-on prompts during CARES pillar reflections.",
        proposed_by="joint",
        success_indicators=[
            "Students use a 'build on' sentence stem at least 3× per session",
            "Teacher speaks less than half of total discussion time",
        ],
        teacher_notes="Ready to try structure without losing warmth.",
        coach_notes="Foundation move; will pay off in every future discussion.",
        related_rubric_domain="Academic Ownership",
        related_core_teacher_skill="Providing opportunities for students to respond to and build on their peers' ideas",
    )
    agree_goal(conn, closed_goal_id)
    if obs:
        attach_observation_to_cycle(conn, observation_id=obs["id"], cycle_id=closed_cycle_id)
    close_goal(conn, closed_goal_id, "closed_met")
    close_cycle(
        conn, closed_cycle_id,
        closing_notes=("Goal met. Peer-response routine established consistently. "
                       "Teacher naturally extended the practice into other reflection prompts."),
    )
    # Now open a fresh active cycle (only allowed because the prior is closed)
    active_cycle_id = create_cycle(
        conn, org_id=org_id, teacher_id=tid, coach_user_id=coach_id,
        title="Fall depth-of-reflection cycle",
        notes=("Building on last cycle's peer-response routines. "
               "Focus: pushing reflection depth via probing questions."),
    )
    active_goal_id = create_goal(
        conn, org_id=org_id, teacher_id=tid, coaching_cycle_id=active_cycle_id,
        title="Deepen reflection with probing follow-ups",
        description="After students share, script 2 follow-up prompts that push analysis over affirmation.",
        proposed_by="teacher",
        success_indicators=[
            "≥ 2 scripted probing follow-ups per discussion",
            "Student responses show cause/effect reasoning at least once per session",
        ],
        teacher_notes="I want to move past 'good sharing' into 'why does that matter'.",
        coach_notes="Perfect next-step goal now that the peer-response routine is stable.",
        related_rubric_domain="Academic Ownership",
        related_core_teacher_skill="Posing questions or providing lesson activities that require students to cite evidence to support their thinking",
    )
    agree_goal(conn, active_goal_id)
    return {"closed_cycle_id": closed_cycle_id, "active_cycle_id": active_cycle_id,
            "goal_ids": [closed_goal_id, active_goal_id]}


def seed_livingston(conn, org_id, coach_id, teacher):
    tid = teacher["id"]
    obs = _get(conn, "SELECT id FROM observations WHERE teacher_id = ? LIMIT 1", tid)
    upsert_teacher_profile_teacher_side(
        conn, teacher_id=tid,
        years_teaching_total=8, years_teaching_subject=6, years_at_current_school=4,
        highest_credential="masters",
        subjects_taught=["Math"],
        grade_levels_taught=["6", "7"],
        self_ratings={"classroom_management": 4, "content_expertise": 4,
                      "student_engagement": 3, "formative_assessment": 3,
                      "family_communication": 3},
        coaching_style_preference="structured",
        career_narrative_notes="Second-generation teacher. Moved into middle school from elementary two years ago.",
        career_goals_notes="Building expertise in math discourse before considering coaching roles.",
    )
    upsert_teacher_profile_coach_side(
        conn, teacher_id=tid,
        observed_style_notes="Clear structure and pacing. Cognitive work concentrated on procedural steps.",
        coach_notes_on_teacher="Wants concrete plans with time-boxed steps. Responds well to lesson-plan scripting.",
    )
    cycle_id = create_cycle(
        conn, org_id=org_id, teacher_id=tid, coach_user_id=coach_id,
        title="Math discourse cycle",
        notes=("Four-week focus on moving from procedural call-and-response to student-to-student "
               "explanation of reasoning."),
    )
    goal_id = create_goal(
        conn, org_id=org_id, teacher_id=tid, coaching_cycle_id=cycle_id,
        title="Increase student-to-student math discourse",
        description=("Move from teacher-narrated Step 1/Step 2 to student-explained reasoning between peers. "
                     "Target 2-3 turn-and-talks per lesson."),
        proposed_by="coach",
        success_indicators=[
            "2-3 turn-and-talks per lesson with cold-call share-outs",
            "Students explain reasoning in complete sentences using math vocabulary",
        ],
        teacher_notes="Not sure how to keep pace if I add these — need to see it modeled.",
        coach_notes="Reasonable concern; will co-plan first two lessons.",
        related_rubric_domain="Academic Ownership",
        related_core_teacher_skill="Structuring and delivering lesson activities so that students do an appropriate amount of the thinking required by the lesson",
    )
    agree_goal(conn, goal_id)
    if obs:
        attach_observation_to_cycle(conn, observation_id=obs["id"], cycle_id=cycle_id)
    return {"cycle_id": cycle_id, "goal_ids": [goal_id]}


def seed_district_context(conn, org_id):
    upsert_district_context(
        conn, org_id=org_id, academic_year="2026-2027",
        year_arc=[
            {"phase": "onboarding", "start_month": 8, "end_month": 8,
             "description": "Setup, norming, roster review."},
            {"phase": "baseline_observations", "start_month": 9, "end_month": 10,
             "description": "Baseline observations. Cycles open in October."},
            {"phase": "cycles", "start_month": 11, "end_month": 3,
             "description": "Full coaching-cycle season. Focus on cycle-scoped growth."},
            {"phase": "renewal", "start_month": 4, "end_month": 4,
             "description": "Reflection cycle. Set goals for next year."},
            {"phase": "testing_prep", "start_month": 5, "end_month": 6,
             "description": "State-testing prep. Coaching stays pragmatic and outcome-focused."},
        ],
        district_priorities=[
            "Student discourse",
            "Formative assessment",
            "Culturally responsive practices",
        ],
        district_initiatives=[
            {"name": "New ELA curriculum roll-out", "grades": ["6", "7", "8"],
             "description": "Wit & Wisdom implementation year 1."},
            {"name": "Restorative practices PD", "grades": ["K-12"],
             "description": "Monthly staff PD on restorative circles."},
        ],
    )


def main() -> None:
    conn = connect(DB)
    org = _get(conn, "SELECT id FROM organizations LIMIT 1")
    coach = _get(conn, "SELECT id FROM users WHERE role = 'coach' LIMIT 1")
    if not org or not coach:
        raise SystemExit("No org or coach in DB. Run tools/import_baselines.py first.")

    teachers_by_name = {t["name"]: t for t in _list(
        conn, "SELECT id, name FROM teachers WHERE name IN ('Summey','Dulaney','Lopez','Livingston')"
    )}
    expected = {"Summey", "Dulaney", "Lopez", "Livingston"}
    missing = expected - set(teachers_by_name)
    if missing:
        raise SystemExit(f"Missing teachers in DB: {missing}. Run tools/import_baselines.py first.")

    ids = [t["id"] for t in teachers_by_name.values()]
    wipe_sample_data(conn, ids)
    print(f"Wiped prior sample data for {len(ids)} teachers.")

    summey = seed_summey(conn, org["id"], coach["id"], teachers_by_name["Summey"])
    print(f"Seeded Summey: cycle={summey['cycle_id'][:8]}...")

    dulaney = seed_dulaney(conn, org["id"], coach["id"], teachers_by_name["Dulaney"])
    print(f"Seeded Dulaney: cycle={dulaney['cycle_id'][:8]}...")

    lopez = seed_lopez(conn, org["id"], coach["id"], teachers_by_name["Lopez"])
    print(f"Seeded Lopez: closed={lopez['closed_cycle_id'][:8]}..., active={lopez['active_cycle_id'][:8]}...")

    livingston = seed_livingston(conn, org["id"], coach["id"], teachers_by_name["Livingston"])
    print(f"Seeded Livingston: cycle={livingston['cycle_id'][:8]}...")

    seed_district_context(conn, org["id"])
    print("Seeded district context for 2026-2027.")

    conn.close()
    print("\nAll sample data seeded.")


if __name__ == "__main__":
    main()
