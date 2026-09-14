"""Pydantic schemas for observation outputs.

These schemas are RUBRIC-AGNOSTIC. Domain names and rating labels are typed as
plain strings, not enums, because the set of valid values depends on which
rubric is in use (TNTP has 4 domains and 4 rating levels; Danielson has 4
domains and 4 different levels; CLASS has different structure entirely).

Per-rubric validation — "is this a valid domain / rating for THIS rubric?" —
happens after Pydantic parsing via ``validate_against_rubric``. The API-facing
JSON schema also gets rubric-specific enum constraints injected at scoring time
(see ``pipeline.score._sanitize_schema``).
"""
from __future__ import annotations

from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

from .rubric import Rubric


class EvidenceItem(BaseModel):
    timestamp: str = Field(
        description="Timestamp in the observation as MM:SS (e.g., '04:32')."
    )
    source: str = Field(
        description="Whether evidence comes from spoken dialogue ('transcript'), "
        "a sampled frame ('frame'), or both."
    )
    quote_or_description: str = Field(
        description="Direct quote from the transcript, or description of what is visible in the frame."
    )


class DescriptorScore(BaseModel):
    """One sub-descriptor within a performance area."""
    descriptor: str = Field(
        description="Name of the sub-descriptor as it appears in the rubric."
    )
    rating: str = Field(
        description="Rating label — must be one of the rubric's rating levels."
    )
    evidence: List[EvidenceItem] = Field(
        description="Specific evidence supporting this rating. At least 2 items required."
    )
    rationale: str = Field(
        description="2-4 sentence explanation of why this rating was selected based on the preponderance of evidence."
    )


class DomainAssessment(BaseModel):
    """A full assessment of one performance area — scoring + rubric-tight narrative."""
    domain: str = Field(
        description="Performance area name — must be one of the rubric's domains."
    )
    essential_question: str = Field(
        description="The Essential Question for this performance area, copied verbatim from the rubric."
    )

    # Scoring
    descriptor_scores: List[DescriptorScore] = Field(
        description="One entry per sub-descriptor. Each descriptor name must appear EXACTLY ONCE per domain."
    )
    overall_rating: str = Field(
        description="Overall rating for the domain, using literal preponderance of evidence."
    )
    preponderance_summary: str = Field(
        description="2-3 sentences. MUST open with an explicit count of how many sub-descriptors fall "
        "at each level, then justify the overall rating by preponderance."
    )

    # Rubric-tight narrative
    rubric_descriptor_text: str = Field(
        description="Descriptor text from the rubric for this domain at the assigned rating level. "
        "Quote verbatim from the PDF."
    )
    what_was_observed: str = Field(
        description="3-6 sentences describing what was observed, using rubric vocabulary only. Cite "
        "[MM:SS] timestamps. No metaphors, similes, or coach-poetry that doesn't appear in the rubric."
    )
    distance_from_target: str = Field(
        description="1-2 sentences naming what the next rating up requires per the rubric."
    )
    relevant_core_teacher_skills: List[str] = Field(
        description="Coaching-construct names (Core Teacher Skills for TNTP; Framework Components for "
        "Danielson; etc.) — exact phrasing from the rubric under this performance area."
    )

    @field_validator("descriptor_scores")
    @classmethod
    def _unique_descriptors(cls, v: List[DescriptorScore]) -> List[DescriptorScore]:
        names = [d.descriptor for d in v]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(
                f"descriptor_scores must use unique names per domain; duplicated: {sorted(dupes)}"
            )
        return v


class CoachingRecommendation(BaseModel):
    """A prioritized coaching skill for the next development cycle."""
    core_teacher_skill: str = Field(
        description="Exact name of the coaching-construct entry from the rubric — do not invent."
    )
    related_domain: str = Field(
        description="Performance area name — must match one of the rubric's domains."
    )
    rationale: str = Field(
        description="Why this skill was selected, grounded in the observation evidence. Use rubric language."
    )
    bite_sized_action: str = Field(
        description="Specific, observable, executable-within-a-week action per the rubric's coaching philosophy."
    )
    success_indicators: List[str] = Field(
        description="2-3 observable signs the action is working, that a coach would see on the next visit."
    )


class HighestLeverageMove(BaseModel):
    """The single highest-leverage coaching move for THIS teacher AT THIS moment.

    This is the product's core differentiator — a context-aware recommendation
    that synthesizes teacher stage, active goals, prior trajectory, prior
    action follow-through, district priorities, and the Get Better Faster
    scope-and-sequence into one concrete move.
    """
    move: str = Field(
        description="The specific action the teacher will take. Concrete, observable, doable within a week."
    )
    related_core_teacher_skill: str = Field(
        description="The Core Teacher Skill name (exact rubric phrasing) this move targets."
    )
    related_domain: str = Field(
        description="Performance area name — must match one of the rubric's domains."
    )
    gbf_step_id: Optional[str] = Field(
        default=None,
        description="The Get Better Faster scope-and-sequence step id this move maps to "
        "(e.g., 'phase2_mgmt_teacher_radar'). Choose the earliest-phase step that fits the "
        "teacher's current stage of development — that is what makes the move highest-leverage. "
        "Populate whenever a GBF step reasonably fits; leave None only when no step applies."
    )
    gbf_step_name: Optional[str] = Field(
        default=None,
        description="Human-readable GBF step name (e.g., 'Teacher Radar'). Copy from the "
        "scope-and-sequence for readability. Should match the step referenced by gbf_step_id."
    )
    rationale: str = Field(
        description="Why THIS move over any other, given the specific context. MUST reference at "
        "least 2 specific context factors — e.g., 'given this is your third year and student "
        "discourse has been your active goal for 2 cycles'. Do not restate generic advice."
    )
    contextual_factors: List[str] = Field(
        description="Enumerate the specific context items that shaped the recommendation "
        "(teacher stage, active goal, prior trajectory pattern, district priority, GBF stage, etc.)."
    )
    success_signals_next_visit: List[str] = Field(
        description="2-3 concrete signs a coach would see on the next visit that indicate the "
        "move is working."
    )


class PriorActionAssessment(BaseModel):
    """Assessment of whether a prior bite-sized action was implemented in THIS observation."""
    tracking_id: str = Field(
        description="The ID of the bite_sized_action_tracking row this assessment corresponds to. "
        "Copy verbatim from the context injection."
    )
    core_teacher_skill: str = Field(
        description="Snapshot of the skill name (copy from the tracking record for readability)."
    )
    implementation: str = Field(
        description="One of: full / partial / not_observed / regressed."
    )
    evidence_notes: str = Field(
        description="2-3 sentences of specific evidence from THIS observation supporting the "
        "implementation rating. Cite [MM:SS] timestamps when possible."
    )


class ObservationReport(BaseModel):
    """Full output of one classroom observation scored against the selected rubric.

    The narrative coaching report is composed CLIENT-SIDE from these structured fields.
    """
    opening_paragraph: str = Field(
        description="1-2 sentences of neutral lesson context. NO rubric judgments."
    )
    overall_summary: str = Field(
        description="2-3 paragraph executive summary tying together the domain ratings, using rubric language."
    )
    domain_assessments: List[DomainAssessment] = Field(
        description="One entry per performance area in rubric order. Each entry contains both the "
        "scoring breakdown and the rubric-tight narrative."
    )
    coaching_recommendations: List[CoachingRecommendation] = Field(
        min_length=1, max_length=2,
        description="1-2 coaching-construct entries prioritized for the next development cycle. "
        "Each MUST already appear in the ``relevant_core_teacher_skills`` list of at least one "
        "domain assessment."
    )

    highest_leverage_move: Optional[HighestLeverageMove] = Field(
        default=None,
        description="The single highest-leverage coaching move given ALL relational context. "
        "Populated when teacher context is available. Left None when scoring without context "
        "(e.g., legacy imports)."
    )

    prior_action_assessments: List[PriorActionAssessment] = Field(
        default_factory=list,
        description="One entry per prior bite-sized action that was in flight at the time of this "
        "observation. Empty when there are no open prior actions. The `tracking_id` field on each "
        "entry must correspond verbatim to the ID provided in the context injection."
    )

    @field_validator("domain_assessments")
    @classmethod
    def _unique_domains(cls, v: List[DomainAssessment]) -> List[DomainAssessment]:
        names = [d.domain for d in v]
        if len(set(names)) != len(names):
            raise ValueError(
                f"domain_assessments must contain each performance area exactly once; got {names}"
            )
        return v


# ---------------------------------------------------------------------------
# Per-rubric validation (runs AFTER Pydantic parsing)
# ---------------------------------------------------------------------------


def validate_against_rubric(report: ObservationReport, rubric: Rubric) -> None:
    """Verify the report conforms to the rubric's specific domain and rating vocabulary.

    Pydantic parsed the structural shape. This checks the rubric-specific values:
      - All 4 domains present, spelled exactly.
      - Every rating (overall and sub-descriptor) is a valid rating for this rubric.
      - Coaching recommendations reference domains from the rubric.
      - Every prioritized Core Teacher Skill also appears in some domain's
        ``relevant_core_teacher_skills`` (structural coherence).

    Raises ``ValueError`` on any violation.
    """
    errors: List[str] = []

    # Domain coverage
    domain_names_in_report = [da.domain for da in report.domain_assessments]
    for expected in rubric.domains:
        if expected not in domain_names_in_report:
            errors.append(f"Missing domain assessment: {expected!r}")
    for actual in domain_names_in_report:
        if actual not in rubric.domains:
            errors.append(f"Unknown domain: {actual!r}. Rubric expects: {rubric.domains}")

    # Rating validity
    for da in report.domain_assessments:
        if not rubric.is_valid_rating(da.overall_rating):
            errors.append(
                f"Invalid overall rating {da.overall_rating!r} for domain {da.domain!r}. "
                f"Valid: {rubric.rating_levels}"
            )
        for ds in da.descriptor_scores:
            if not rubric.is_valid_rating(ds.rating):
                errors.append(
                    f"Invalid sub-descriptor rating {ds.rating!r} at "
                    f"{da.domain!r}/{ds.descriptor!r}. Valid: {rubric.rating_levels}"
                )

    # Coaching → domain
    for rec in report.coaching_recommendations:
        if not rubric.is_valid_domain(rec.related_domain):
            errors.append(
                f"Coaching recommendation references unknown domain {rec.related_domain!r}."
            )

    # Coaching skill must have been named in some domain's implicated skills.
    named_skills = set()
    for da in report.domain_assessments:
        named_skills.update(da.relevant_core_teacher_skills)
    for rec in report.coaching_recommendations:
        if rec.core_teacher_skill not in named_skills:
            errors.append(
                f"Coaching recommendation skill {rec.core_teacher_skill!r} was not implicated "
                f"in any domain's relevant_core_teacher_skills. Structural incoherence."
            )

    if errors:
        raise ValueError(
            "Report failed rubric-specific validation:\n  " + "\n  ".join(errors)
        )
