"""Get Better Faster scope & sequence — structured for use in prompts and UI.

Source: Uncommon Schools, "Get Better Faster Scope & Sequence" (Bambrick-Santoyo,
Uncommon Schools; PDF provided by the user). Encoded here as structured Python
so the AI can name a specific action step in the highest_leverage_move, and the
coach can select one when specifying the debrief focus.

Each action step has a stable id like ``phase2_mgmt_teacher_radar`` so it can
be referenced from other tables (observations.debrief_focus_gbf_id).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class GBFStep:
    id: str            # stable id (used as FK from other tables)
    phase_id: str      # phase1 / phase2 / phase3 / phase4 / stretch_it
    phase_label: str   # human-readable phase title
    trajectory: str    # 'management' or 'rigor'
    number: int        # step number within trajectory (1-based)
    name: str          # e.g. "Routines & Procedures 101"
    headline: str      # short verb-phrase description
    bullets: List[str] # the "how" bullets from the PDF


# ---------------------------------------------------------------------------
# The scope-and-sequence data itself
# ---------------------------------------------------------------------------

_PHASE_LABELS = {
    "phase1":     "Phase 1: Pre-Teaching (Summer PD)",
    "phase2":     "Phase 2: Days 1–30",
    "phase3":     "Phase 3: Days 31–60",
    "phase4":     "Phase 4: Days 61–90",
    "stretch_it": "Stretch It (Next Steps)",
}


def _mk(phase_id: str, trajectory: str, number: int, name: str,
        headline: str, bullets: List[str]) -> GBFStep:
    slug = name.lower()
    slug = "".join(c if c.isalnum() else "_" for c in slug)
    slug = "_".join(w for w in slug.split("_") if w)
    step_id = f"{phase_id}_{trajectory[:4]}_{slug}"
    return GBFStep(
        id=step_id,
        phase_id=phase_id,
        phase_label=_PHASE_LABELS[phase_id],
        trajectory=trajectory,
        number=number,
        name=name,
        headline=headline,
        bullets=bullets,
    )


STEPS: List[GBFStep] = [
    # ==============================================================
    # Phase 1 — Pre-Teaching (Summer PD)
    # ==============================================================
    _mk("phase1", "management", 1, "Routines & Procedures 101",
        "Design and roll out critical routines.",
        [
            "Plan & practice critical routines and procedures moment-by-moment: explain the routine, script teacher and student actions, plan for students who don't follow it.",
            "Plan and practice the roll-out: model the routine (the 'I Do'), plan what to do when students don't get it right.",
        ]),
    _mk("phase1", "management", 2, "Strong Voice",
        "Stand and speak with purpose.",
        [
            "Square Up, Stand Still: when giving instructions, stop moving and strike a formal pose.",
            "Formal Register: when giving instructions, use formal register — tone and word choice.",
        ]),
    _mk("phase1", "rigor", 1, "Develop Effective Lesson Plans 101",
        "Build the foundation of an effective lesson rooted in what students need to learn.",
        [
            "Write precise learning objectives that are data-driven, curriculum-plan-driven, and accomplishable in one lesson.",
            "Deliver a basic 'I Do' as a core part of the lesson.",
            "Design an exit ticket (brief final mini-assessment) aligned to the objective.",
        ]),
    _mk("phase1", "rigor", 2, "Internalize Existing Lesson Plans",
        "Make existing plans your own.",
        [
            "Internalize & rehearse key parts of the lesson, including the 'I Do' and all key instructions.",
            "Build time stamps into the lesson plan and follow them.",
        ]),

    # ==============================================================
    # Phase 2 — Days 1–30
    # ==============================================================
    _mk("phase2", "management", 1, "What to Do",
        "Give crisp instructions — economy of language.",
        [
            "Economy of Language: give crisp instructions with as few words as possible (3-word directions).",
            "Check for understanding on complex instructions.",
        ]),
    _mk("phase2", "management", 2, "Routines & Procedures 201",
        "Revise and perfect routines.",
        [
            "Revise any routine that needs more attention to detail or is inefficient — what students and teachers do at each moment.",
            "Do It Again: have students redo the routine if not done correctly.",
            "Cut it Short: know when to stop Do It Again.",
        ]),
    _mk("phase2", "management", 3, "Teacher Radar",
        "Know when students are off-task.",
        [
            "Deliberately scan for on-task behavior; choose 3-4 hot spots to scan constantly.",
            "Be Seen Looking: crane your neck to appear to see all corners.",
            "Circulate the room with purpose (break the plane); stand at corners on the perimeter.",
            "Move away from the student who's speaking to monitor the whole room.",
        ]),
    _mk("phase2", "management", 4, "Whole-Class Reset",
        "Re-establish behavioral expectations decisively.",
        [
            "Planned reset when the routine has slowly weakened over prior classes.",
            "In-the-moment reset when a class veers off task during the period: stop teaching, square up, clear What to Do ('Pencils down. Eyes on me.'), pick up tone and energy.",
        ]),
    _mk("phase2", "rigor", 3, "Write the Exemplar",
        "Set the bar for excellence.",
        [
            "Script out the ideal written responses you want students to produce during independent practice.",
            "Align independent practice to the rigor of the upcoming interim assessment.",
        ]),
    _mk("phase2", "rigor", 4, "Independent Practice",
        "Set up daily routines that build opportunities for independent practice.",
        [
            "Write first, talk second: writing tasks before class discussion so every student answers independently.",
            "Implement a daily entry prompt (Do Now).",
            "Implement and review a longer independent practice and/or a daily Exit Ticket.",
        ]),
    _mk("phase2", "rigor", 5, "Monitor Aggressively",
        "Check independent work to know whether they're learning.",
        [
            "Create & implement a monitoring pathway: seating chart, fastest writers first, then those who need support.",
            "Monitor quality: check answers against your exemplar, track correct/incorrect.",
            "Pen in hand: mark up student work as you circulate — coding system, minimal verbal intervention.",
        ]),

    # ==============================================================
    # Phase 3 — Days 31–60
    # ==============================================================
    _mk("phase3", "management", 5, "Build the Momentum",
        "Create energy and challenge in the room.",
        [
            "Simple challenge: 'I know you're 4th graders, but I have a 5th grade problem I bet you could master.'",
            "Sparkle: speak faster, walk faster, vary your voice, smile.",
        ]),
    _mk("phase3", "management", 6, "Pacing",
        "Create the illusion of speed.",
        [
            "Hand-held timer for lesson time stamps + student audio cue.",
            "Rate of questioning: <2 seconds between student response and teacher pick-back-up.",
            "Countdowns to work the clock ('do that in 5..4..3..2..1').",
            "Call and Response for key words.",
        ]),
    _mk("phase3", "management", 7, "Engage All Students",
        "Make sure all students participate.",
        [
            "Cold call students; make sure to call on all.",
            "Brief (15-30s) Turn & Talks.",
            "Alternate: cold call, choral response, all hands, turn & talks.",
        ]),
    _mk("phase3", "management", 8, "Narrate the Positive",
        "Narrate what students do well.",
        [
            "'I like how Javon has gotten straight to work on his writing.'",
            "Look at off-task students while narrating positive.",
            "Praise answers that are above and beyond, or strong effort.",
        ]),
    _mk("phase3", "management", 9, "Individual Student Corrections",
        "Redirect with the least invasive intervention.",
        [
            "Anticipate off-task behavior; rehearse the next two moves.",
            "Ladder: proximity → eye contact → non-verbal → say name quickly → small consequence.",
        ]),
    _mk("phase3", "rigor", 6, "Habits of Evidence",
        "Teach students to work from and cite evidence.",
        [
            "Teach students to annotate with purpose (summarize, analyze, find best evidence).",
            "Teach and prompt students to cite key evidence in their responses.",
        ]),
    _mk("phase3", "rigor", 7, "Check for Whole-Group Understanding",
        "Gather evidence on whole-group learning.",
        [
            "Poll the room ('How many chose A? B? C? D?' or whiteboard show-me).",
            "Target the error: focus discussion on where students most struggle.",
        ]),
    _mk("phase3", "rigor", 8, "Re-teaching 101 — Model",
        "Model for students how to think/solve/write.",
        [
            "Give a clear listening/note-taking task before the model, then debrief.",
            "Model the thinking, not just a procedure; vary tone/cadence to highlight thinking.",
            "We Do and You Do: guided then independent at-bats.",
        ]),

    # ==============================================================
    # Phase 4 — Days 61–90
    # ==============================================================
    _mk("phase4", "management", 10, "Engaged Small Group Work",
        "Maximize learning for every student during group work.",
        [
            "Explicit step-by-step instructions; visible tasks; a role for every person.",
            "Timed instructions with benchmarks per window.",
            "Monitor visual evidence of group progress every 5-10 minutes.",
            "Verbally enforce individual & group accountability.",
        ]),
    _mk("phase4", "rigor", 9, "Re-teaching 201 — Guided Discourse",
        "Let students unpack their own errors & build a solution.",
        [
            "Show-Call: post student work (exemplar or incorrect) and ask why.",
            "Stamp the understanding: 'What are the keys to remember?' or 'Give me a rule.'",
            "At-bats: guided practice, then independent.",
        ]),
    _mk("phase4", "rigor", 10, "Universal Prompts",
        "Push the thinking back on students with universal prompts.",
        [
            "Wait time after challenging questions.",
            "Pre-call: warn a student you're calling on them next.",
            "Roll back the answer.",
            "'Tell me more.' / 'What makes you think that?' / 'How do you know?' / 'Why is that important?'",
            "Close the loop: return to students with wrong answers.",
        ]),
    _mk("phase4", "rigor", 11, "Habits of Discussion",
        "Teach and model habits that strengthen class conversation.",
        [
            "Keep neutral / manage your tell.",
            "Agree/Build off of: 'I agree with ____ and I'd like to add….'",
            "Disagree respectfully: 'I disagree with ____. I would argue….'",
        ]),

    # ==============================================================
    # Stretch It (Next Steps) — Rigor only, per the PDF
    # ==============================================================
    _mk("stretch_it", "rigor", 12, "Strategic Prompts",
        "Ask strategic questions to targeted students in response to student error.",
        [
            "Prompt students to access previously learned knowledge; use a prompting guide.",
            "Call on students based on learning needs (data-driven).",
            "Students prompting students: use habits of discussion to critique each other.",
        ]),
    _mk("stretch_it", "rigor", 13, "Go Conceptual",
        "Get students to do the conceptual thinking.",
        [
            "Verbalize a conceptual understanding, not just an answer: 'That's the procedure. Now tell me why.'",
            "Upgrade vocabulary: technical/academic language.",
            "Stretch it: harder extension questions; alternative solutions; counter-arguments.",
        ]),
]


# ---------------------------------------------------------------------------
# Lookups + helpers
# ---------------------------------------------------------------------------

STEPS_BY_ID: Dict[str, GBFStep] = {s.id: s for s in STEPS}


def all_steps_by_phase() -> Dict[str, Dict[str, List[GBFStep]]]:
    """Return a nested dict: phase_id -> trajectory -> [steps]."""
    out: Dict[str, Dict[str, List[GBFStep]]] = {}
    for s in STEPS:
        out.setdefault(s.phase_id, {}).setdefault(s.trajectory, []).append(s)
    return out


def get_step(step_id: str) -> "GBFStep | None":
    return STEPS_BY_ID.get(step_id)


def render_for_prompt(max_bullets_per_step: int = 2) -> str:
    """Render the scope-and-sequence as a compact markdown block for the AI
    prompt. Bullets truncated to keep prompt size manageable.
    """
    out: List[str] = []
    out.append("# Get Better Faster scope & sequence")
    out.append("")
    out.append("Uncommon Schools' developmental progression of coaching moves. "
               "Two parallel trajectories (Management, Rigor) across four phases.")
    out.append("")
    grouped = all_steps_by_phase()
    for phase_id in ["phase1", "phase2", "phase3", "phase4", "stretch_it"]:
        out.append(f"## {_PHASE_LABELS[phase_id]}")
        for traj in ("management", "rigor"):
            steps = grouped.get(phase_id, {}).get(traj, [])
            if not steps:
                continue
            out.append(f"**{traj.capitalize()} trajectory**")
            for s in steps:
                out.append(f"- [{s.id}] **{s.name}** — {s.headline}")
                for b in s.bullets[:max_bullets_per_step]:
                    out.append(f"    - {b}")
        out.append("")
    return "\n".join(out)
