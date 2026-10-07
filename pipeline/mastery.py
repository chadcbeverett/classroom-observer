"""Which Get Better Faster steps has this teacher actually mastered?

The scope and sequence is developmental: a teacher works a step until it holds,
then moves on. The prompt already asks for "the earliest-phase step that fits
the teacher's actual stage of development" — but nothing recorded what that
stage was, so every observation re-guessed it from the lesson in front of it. A
teacher could be handed Teacher Radar three cycles running, or jumped to a
Phase 4 technique because one lesson happened to look advanced.

This joins the two halves that already exist. `published_coach_moves.gbf_step_id`
says which step was worked on; `move_transfer_checks` says whether it came back
in the next lesson. Together they say what a teacher has established.

Mastery is deliberately conservative. A step counts as held only when a transfer
check reached consistent or automatic, and the coach's confirmation is preferred
over the model's proposal — the same rule the transfer check itself uses. A step
that was worked on but never checked is "in progress", not mastered: silence is
not evidence of success, and treating it as such would march a teacher through
the sequence on the strength of nothing.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .gbf import STEPS, STEPS_BY_ID, _PHASE_LABELS

# Phase order for "earliest unfinished phase". stretch_it sits after the
# numbered phases by construction.
PHASE_ORDER: List[str] = ["phase1", "phase2", "phase3", "phase4", "stretch_it"]

# Transfer statuses that mean the step held.
HELD = ("consistent", "automatic")


@dataclass
class StepState:
    step_id: str
    name: str
    phase_id: str
    phase_label: str
    status: str                       # mastered | in_progress | untouched
    evidence: Optional[str] = None    # how we know, for the coach
    times_worked: int = 0


@dataclass
class MasteryPicture:
    teacher_id: str
    steps: Dict[str, StepState] = field(default_factory=dict)

    @property
    def mastered(self) -> List[StepState]:
        return [s for s in self.steps.values() if s.status == "mastered"]

    @property
    def in_progress(self) -> List[StepState]:
        return [s for s in self.steps.values() if s.status == "in_progress"]

    def phase_counts(self, phase_id: str) -> tuple:
        """(mastered, total) for one phase."""
        in_phase = [s for s in STEPS if s.phase_id == phase_id]
        done = sum(1 for s in in_phase
                   if self.steps.get(s.id) and self.steps[s.id].status == "mastered")
        return done, len(in_phase)

    @property
    def current_phase_id(self) -> str:
        """Earliest phase with work still open.

        "Open" means not every step is mastered. A teacher does not have to
        finish a phase to be working in it, but they should not be handed a
        later phase while an earlier one still has unmastered steps — that is
        the whole point of a sequence.
        """
        for phase in PHASE_ORDER:
            done, total = self.phase_counts(phase)
            if total and done < total:
                return phase
        return PHASE_ORDER[-1]

    @property
    def current_phase_label(self) -> str:
        return _PHASE_LABELS.get(self.current_phase_id, self.current_phase_id)


def teacher_mastery(conn, teacher_id: str) -> MasteryPicture:
    """Build the mastery picture from moves actually worked and checked."""
    pic = MasteryPicture(teacher_id=teacher_id)

    rows = conn.execute(
        """SELECT m.gbf_step_id AS step_id,
                  COALESCE(c.coach_confirmed_status, c.ai_status) AS status,
                  c.confirmed_at IS NOT NULL AS confirmed,
                  o.uploaded_at AS worked_at
             FROM published_coach_moves m
             JOIN observations o ON o.id = m.observation_id
        LEFT JOIN move_transfer_checks c ON c.coaching_move_id = m.id
            WHERE o.teacher_id = ? AND o.deleted_at IS NULL
              AND m.gbf_step_id IS NOT NULL
         ORDER BY o.uploaded_at ASC""",
        (teacher_id,),
    ).fetchall()

    for r in rows:
        sid = r["step_id"]
        step = STEPS_BY_ID.get(sid)
        if step is None:
            # A step id that no longer exists in the sequence. Skip rather than
            # invent a phase for it; the move itself is still on the record.
            continue
        prev = pic.steps.get(sid)
        times = (prev.times_worked if prev else 0) + 1

        status = r["status"]
        if status in HELD:
            source = "coach-confirmed" if r["confirmed"] else "from the transfer check"
            state, ev = "mastered", f"{status}, {source}"
        elif status:
            state, ev = "in_progress", f"last check: {status}"
        else:
            state, ev = "in_progress", "worked on, never checked"

        # Mastery, once reached, is not withdrawn by a later unchecked cycle.
        # A coach can override by confirming a lower status on a new check.
        if prev and prev.status == "mastered" and state != "mastered":
            state, ev = prev.status, prev.evidence

        pic.steps[sid] = StepState(
            step_id=sid, name=step.name, phase_id=step.phase_id,
            phase_label=step.phase_label, status=state, evidence=ev,
            times_worked=times,
        )

    return pic


def open_steps_in_phase(pic: MasteryPicture, phase_id: Optional[str] = None) -> List[dict]:
    """Steps in the teacher's current phase that are not yet mastered."""
    phase = phase_id or pic.current_phase_id
    out = []
    for s in STEPS:
        if s.phase_id != phase:
            continue
        st = pic.steps.get(s.id)
        if st and st.status == "mastered":
            continue
        out.append({
            "step_id": s.id,
            "name": s.name,
            "status": st.status if st else "untouched",
            "times_worked": st.times_worked if st else 0,
        })
    return out


def render_for_prompt(pic: MasteryPicture) -> str:
    """The mastery picture as context for the scoring call.

    Says what is established, what is open, and which phase the next move
    should come from — so the model stops re-deriving the teacher's stage from
    a single lesson.
    """
    lines = ["## Where this teacher is in the development sequence", ""]

    mastered = pic.mastered
    if mastered:
        lines.append("Established — do not recommend these again unless they have slipped:")
        for s in sorted(mastered, key=lambda x: PHASE_ORDER.index(x.phase_id)):
            lines.append(f"- {s.name} ({s.phase_label}) — {s.evidence}")
        lines.append("")
    else:
        lines.append("No step has been confirmed as holding yet.")
        lines.append("")

    progressing = pic.in_progress
    if progressing:
        lines.append("Worked on but not yet holding:")
        for s in progressing:
            worked = f", worked {s.times_worked}×" if s.times_worked > 1 else ""
            lines.append(f"- {s.name} ({s.phase_label}) — {s.evidence}{worked}")
        lines.append("")

    phase = pic.current_phase_id
    done, total = pic.phase_counts(phase)
    lines.append(f"Current phase: {pic.current_phase_label} ({done} of {total} steps established).")
    open_now = open_steps_in_phase(pic)
    if open_now:
        lines.append("Still open in this phase:")
        for s in open_now[:8]:
            tag = "" if s["status"] == "untouched" else f" ({s['status']})"
            lines.append(f"- {s['name']}{tag}")
    lines.append("")
    lines.append(
        "Choose the next move from this phase. Do not reach into a later phase "
        "while steps here are open — the sequence exists because the later ones "
        "depend on these. A step already established should only return if this "
        "lesson shows it slipping, and say so explicitly if you bring one back."
    )
    return "\n".join(lines)
