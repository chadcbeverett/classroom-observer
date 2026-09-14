"""Rubric configuration — the pluggable piece of the pipeline.

Everything rubric-specific (domain names, rating levels, prompt language,
vocabulary examples) lives here. Other modules take a ``Rubric`` instance
and never hardcode the rubric's contents.

To add a new rubric (e.g., Danielson, CLASS, a district custom):
    1. Add a new ``Rubric(...)`` instance below.
    2. Register it in ``RUBRICS`` by id.
    3. Users select via ``--rubric <id>`` on the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass(frozen=True)
class Rubric:
    """A rubric definition.

    Attributes:
        id: Stable identifier, e.g. ``"tntp_core_4pt_2014"``. Used on the CLI
            and stored in observation metadata so old outputs remain
            interpretable if a rubric evolves.
        name: Human-readable name shown to users and coaches.
        pdf_path: Path to the rubric PDF, attached to Claude as a document
            block so the model can quote descriptor language verbatim.
        domains: Ordered list of performance area names, verbatim from the
            rubric. Order determines display order everywhere.
        rating_levels: Ordered list of rating labels, LOW to HIGH. The score
            for a rating is its 1-indexed position (Ineffective=1, ..., Effective=4
            for TNTP; other rubrics may have 3 or 5 levels).
        essential_questions: Mapping of domain name → the rubric's Essential
            Question for that domain.
        core_teacher_skill_note: Rubric-specific description of the coaching
            construct — for TNTP it's Core Teacher Skills, for Danielson it
            would be Framework Components, etc. Used in the prompt so the
            model uses the right terminology.
        vocabulary_examples: Rubric-specific phrases the model is encouraged
            to use in ``what_was_observed``. Injected into the system prompt.
        coaching_philosophy: A paragraph describing the coaching model this
            rubric prescribes — e.g., TNTP's "bite-sized weekly action" pattern.
        scoring_notes: Rubric-specific scoring guidance — preponderance rules,
            anti-inflation notes, common Minimally Effective indicators, etc.
    """
    id: str
    name: str
    pdf_path: Path
    domains: List[str]
    rating_levels: List[str]
    essential_questions: Dict[str, str]
    core_teacher_skill_note: str
    vocabulary_examples: List[str]
    coaching_philosophy: str
    scoring_notes: str

    # Optional per-domain sub-descriptor hints for the prompt; leave empty to
    # let the model discover them from the PDF.
    sub_descriptor_hints: Dict[str, List[str]] = field(default_factory=dict)

    def score_for(self, rating: str) -> int:
        """1-indexed numeric score for a rating. Raises if not a valid rating."""
        return self.rating_levels.index(rating) + 1

    def is_valid_rating(self, rating: str) -> bool:
        return rating in self.rating_levels

    def is_valid_domain(self, domain: str) -> bool:
        return domain in self.domains

    @property
    def top_rating(self) -> str:
        return self.rating_levels[-1]

    @property
    def bottom_rating(self) -> str:
        return self.rating_levels[0]

    @property
    def num_levels(self) -> int:
        return len(self.rating_levels)


# ---------------------------------------------------------------------------
# Registered rubrics
# ---------------------------------------------------------------------------

_RUBRICS_DIR = Path(__file__).resolve().parent.parent / "rubrics"


TNTP_CORE_4PT_2014 = Rubric(
    id="tntp_core_4pt_2014",
    name="TNTP Core Teaching Rubric (4-point, 2014)",
    pdf_path=_RUBRICS_DIR / "TNTPCoreTeachingRubric_4pt_2014.pdf",
    domains=[
        "Student Engagement",
        "Essential Content",
        "Academic Ownership",
        "Demonstration of Learning",
    ],
    rating_levels=["Ineffective", "Minimally Effective", "Developing", "Effective"],
    essential_questions={
        "Student Engagement": "Are all students engaged in the work of the lesson from start to finish?",
        "Essential Content": "Are all students working with content aligned to the appropriate standards for their subject and grade?",
        "Academic Ownership": "Are all students responsible for doing the thinking in this classroom?",
        "Demonstration of Learning": "Do all students demonstrate that they are learning?",
    },
    core_teacher_skill_note=(
        "TNTP names the coaching construct 'Core Teacher Skills' — a non-exhaustive list of "
        "teacher skills and behaviors listed under each performance area in the rubric PDF. "
        "Core Teacher Skills are NOT part of scoring; they are used only for coaching. After "
        "scoring, select 1-2 Core Teacher Skills to prioritize for the next development cycle."
    ),
    vocabulary_examples=[
        "most students complete instructional tasks",
        "students complete instructional tasks, volunteer responses, and/or ask appropriate questions",
        "behavioral expectations are clear / unclear",
        "students execute transitions and procedures in an orderly and efficient manner",
        "lesson is aligned to the appropriate standards",
        "students do the cognitive work of the lesson",
        "discourse routes through the teacher",
        "students respond to and build on each other's ideas",
        "checks for understanding capture data from all students / sample volunteers",
        "students use complete sentences and academic language",
    ],
    coaching_philosophy=(
        "TNTP's coaching philosophy prescribes a bite-sized, observable action the teacher can "
        "execute within the next week. Actions should be concrete, sequenced, and observable — "
        "not vague aspirations. Include a counting mechanic (sticky-note tally, number of moves "
        "per lesson) so the teacher can self-monitor and the coach can verify on the next visit."
    ),
    scoring_notes=(
        "TNTP uses preponderance of evidence — the majority of sub-descriptors determines the "
        "overall rating. Rules:\n"
        "  - If the MAJORITY of sub-descriptors are at level X, the overall is X. Do not round up.\n"
        "  - If sub-descriptors are evenly split between two levels, take the LOWER level.\n"
        "  - A single Effective sub-descriptor does NOT lift the domain to Effective.\n"
        "  - A 3-1 split with three Developing and one Minimally Effective → Developing.\n"
        "  - A 2-2 or worse with Minimally Effective in the mix → Minimally Effective.\n"
        "\n"
        "The 'Developing' rating is the bar for PROFICIENT practice, not 'trying hard.' Most "
        "observed lessons in TNTP norming data are Minimally Effective. Common signs the lesson "
        "is Minimally Effective:\n"
        "  - The teacher does most of the talking and thinking.\n"
        "  - Checks for understanding sample only volunteers.\n"
        "  - Directions need to be repeated 3+ times.\n"
        "  - Some students (25-60%) are off-task or unclear at any given moment.\n"
        "  - Discourse is overwhelmingly teacher → student → teacher.\n"
        "  - The lesson is engaging but cognitive demand is recall, not analysis.\n"
        "\n"
        "Default to the LOWER rating when uncertain."
    ),
)


# Registry of available rubrics. Users select by id via ``--rubric <id>`` on the CLI.
RUBRICS: Dict[str, Rubric] = {
    TNTP_CORE_4PT_2014.id: TNTP_CORE_4PT_2014,
}


def get_rubric(rubric_id: str) -> Rubric:
    """Look up a rubric by id. Raises KeyError with the list of known ids."""
    if rubric_id not in RUBRICS:
        known = ", ".join(sorted(RUBRICS.keys()))
        raise KeyError(f"Unknown rubric id: {rubric_id!r}. Known rubrics: {known}")
    return RUBRICS[rubric_id]


DEFAULT_RUBRIC_ID = TNTP_CORE_4PT_2014.id
