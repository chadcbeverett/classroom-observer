"""Draft a rehearsal plan for a published coaching move.

The coaching loop names one move and then waits to see whether it happened.
Nothing in it asks the teacher to perform the move correctly, repeatedly,
before the next live lesson — which is the step the practice literature treats
as the one that makes a move stick. This module drafts that step.

The draft is a draft. The coach edits it, owns it, and runs it with the teacher;
nothing here reaches the teacher directly, which keeps the coach-mediated gate
intact. `derived_from_ai` is recorded on the plan the same way it is on the move,
so oversight can see whether coaches are editing drafts or rubber-stamping them.

The phase constraint is enforced in code rather than requested in the prompt. A
teacher early in the year rehearsing a late-phase technique is the specific
failure the scope and sequence exists to prevent, and a prompt instruction is
not a constraint.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional, Sequence

import anthropic

from .gbf import get_step
from .schemas import PracticePlan

MODEL = "claude-opus-4-7"

_STRIP_KEYS = {
    "minLength", "maxLength", "pattern", "format",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "maxItems", "default",
}


def _sanitize_schema(node):
    if isinstance(node, dict):
        if node.get("type") == "object":
            node["additionalProperties"] = False
        if "minItems" in node and node["minItems"] not in (0, 1):
            del node["minItems"]
        for key in list(node.keys()):
            if key in _STRIP_KEYS:
                del node[key]
        for key in ("properties", "$defs", "definitions"):
            for v in node.get(key, {}).values():
                _sanitize_schema(v)
        if isinstance(node.get("items"), dict):
            _sanitize_schema(node["items"])
        for key in ("anyOf", "allOf", "oneOf"):
            for v in node.get(key, []):
                _sanitize_schema(v)
    return node


SYSTEM_PROMPT = """You draft a short rehearsal plan that a coach will run with a
teacher, in person, before that teacher next teaches.

You are writing for the coach to perform, not for the teacher to read. Write it
the way a good coach would run it: concrete, quick, and about doing rather than
discussing.

# What makes a usable plan

The objective names a behaviour the teacher will be able to perform by the end
of practice. Not "understand why cold calling matters" — "cold call three
students in under thirty seconds without losing the thread of the question."

The model script is what the coach says and does to show the move, 30 to 60
seconds of it. Write the actual words where the words are the point. A coach
should be able to perform it from the page without preparing.

Rounds escalate. The first is clean: the move alone, nothing in the way. Each
later round adds one difficulty — a student who answers wrongly, one who says
nothing, a question that arrives mid-transition. Three or four rounds total. The
first round has no curveball; later rounds each have exactly one.

Every round has a success criterion the coach can see in the moment. "The
teacher waits three full seconds before calling a name" is observable. "The
teacher seems more confident" is not.

The do-over rule tells the coach when to stop and run the round again instead of
talking about it. Practice works because the correct version gets repeated, so
the rule should trigger on the move itself breaking down, not on general
imperfection.

# Hard constraints

Rehearse the published move. Not a better move you would have chosen, not an
adjacent skill — the one the coach named. If the move seems weak to you, draft
the best rehearsal of it anyway; the coach decides what to teach.

Stay inside the teacher's current development phase. The phase and its action
steps are given below. Do not reach for a technique from a later phase because
it would make the rehearsal richer.

No student names. Rehearsal scenarios involve a coach playing a generic student.
Write "a student answers with one word", never a name from the lesson.

Keep the whole plan runnable in ten minutes. A plan a coach skips because there
is no time is worth less than a shorter one they actually run."""


def _context_block(
    *,
    move_text: str,
    gbf_step_id: Optional[str],
    teacher_name: Optional[str],
    evidence_moments: Sequence[str] = (),
    rubric_domain: Optional[str] = None,
) -> str:
    lines = ["## The move the coach published", "", move_text.strip(), ""]

    step = get_step(gbf_step_id) if gbf_step_id else None
    if step is not None:
        lines += [
            "## The teacher's current development phase",
            "",
            f"Phase: {step.phase_label}",
            f"Action step: {step.name}",
            "",
            "Rehearse within this phase. Techniques from later phases are out of "
            "scope for this teacher right now, however useful they might be.",
            "",
        ]
    else:
        lines += [
            "## Development phase",
            "",
            "No phase is tagged on this move. Keep the rehearsal to fundamentals "
            "and avoid advanced technique.",
            "",
        ]

    if rubric_domain:
        lines += [f"## Rubric area", "", rubric_domain, ""]

    if evidence_moments:
        lines += ["## What prompted the move, from the lesson", ""]
        lines += [f"- {m}" for m in evidence_moments[:3]]
        lines += [
            "",
            "Use these to make the rehearsal resemble this teacher's actual "
            "classroom. Do not quote them back at the teacher during practice.",
            "",
        ]

    lines += [
        "## Output",
        "",
        "Draft the plan. The coach will edit it before running it.",
    ]
    return "\n".join(lines)


def draft_practice_plan(
    *,
    move_text: str,
    gbf_step_id: Optional[str] = None,
    teacher_name: Optional[str] = None,
    evidence_moments: Sequence[str] = (),
    rubric_domain: Optional[str] = None,
    client: Optional[anthropic.Anthropic] = None,
    max_tokens: int = 4000,
) -> PracticePlan:
    """Draft a rehearsal plan for one published move.

    Raises ValueError on an empty move. The returned plan is a draft with no
    status of its own; persisting and publishing it is the caller's decision.
    """
    if not (move_text or "").strip():
        raise ValueError("Cannot draft a practice plan without a coaching move.")

    client = client or anthropic.Anthropic(max_retries=4)
    schema = _sanitize_schema(PracticePlan.model_json_schema())

    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _context_block(
            move_text=move_text,
            gbf_step_id=gbf_step_id,
            teacher_name=teacher_name,
            evidence_moments=evidence_moments,
            rubric_domain=rubric_domain,
        )}],
    ) as stream:
        final = stream.get_final_message()

    raw = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")

    diag_dir = Path(os.environ.get("OBSERVER_DIAG_DIR", "/tmp"))
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "observer_last_practice_plan.json").write_text(raw)
    except OSError:
        pass

    if getattr(final, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"The model hit its output limit after {len(raw)} characters while "
            f"drafting this plan. Retry with a larger max_tokens."
        )

    return PracticePlan.model_validate_json(raw)


def plan_warnings(plan: PracticePlan, *, gbf_step_id: Optional[str] = None) -> List[str]:
    """Things the coach should look at before publishing this draft.

    Checked in code rather than trusted to the prompt, because the failures that
    matter here are quiet ones: a plan that reads well but never escalates is
    repetition without progression, and a coach skimming a tidy draft will not
    notice.
    """
    out: List[str] = []

    if len(plan.rounds) < 3:
        out.append(
            f"Only {len(plan.rounds)} round(s). A plan that does not escalate is "
            f"repetition rather than practice — add a round that makes the move harder."
        )
    if len(plan.rounds) > 4:
        out.append(
            f"{len(plan.rounds)} rounds may not fit a ten-minute rehearsal. "
            f"Consider cutting to the three that matter most."
        )

    with_curve = sum(1 for r in plan.rounds if (r.curveball or "").strip())
    if len(plan.rounds) > 1 and with_curve == 0:
        out.append(
            "No round introduces a difficulty. Every round is the clean version, "
            "so the teacher never practises recovering."
        )

    if plan.rounds and (plan.rounds[0].curveball or "").strip():
        out.append(
            "The first round already has a curveball. The opening round is "
            "usually the clean repetition that encodes the move correctly."
        )

    weak = [r.success_criterion for r in plan.rounds
            if any(w in (r.success_criterion or "").lower()
                   for w in ("seems", "feels", "appears", "comfortable", "confident", "better"))]
    if weak:
        out.append(
            f"{len(weak)} success criterion(s) describe how the teacher seems "
            f"rather than what they did — hard to call in the moment: "
            f"“{weak[0][:60]}”"
        )

    step = get_step(gbf_step_id) if gbf_step_id else None
    if step is None and gbf_step_id:
        out.append(
            f"The move is tagged to an unknown phase step ({gbf_step_id!r}), so "
            f"the draft was written without a phase constraint."
        )

    return out
