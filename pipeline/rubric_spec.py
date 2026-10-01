"""Rubric-agnostic specification, and the deterministic engine that scores against it.

Two things live here, and the separation is the point.

`RubricSpec` is what an arbitrary district rubric becomes once parsed: domains,
ordered rating levels, sub-descriptors, and the verbatim descriptor text for each
level. It carries a content hash so a score can name exactly which instrument
produced it, and an `AggregationRule` that states — as data, not as prose in a
prompt — how sub-descriptor ratings combine into a domain rating.

`aggregate_domain` applies that rule. The model never decides a domain rating.
It supplies evidence and per-sub-descriptor judgments; this function counts them.
Previously the aggregation rules were paragraphs of English inside the system
prompt ("do not round up", "an even split takes the lower level"), which meant
compliance was a matter of the model's cooperation. Now the rules are a struct
and the arithmetic happens in Python, so a given set of sub-descriptor ratings
always yields the same domain rating, and the rule that produced it is
recorded alongside the score.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# Sentinel used when a sub-descriptor lacks enough verified evidence to score.
# Distinct from the lowest rating: "we could not tell" is not "it was bad".
INSUFFICIENT = "Insufficient evidence"


class AggregationRule(BaseModel):
    """How sub-descriptor ratings combine into one domain rating.

    Defaults encode the preponderance convention the product launched with, so
    an ingested rubric that says nothing about aggregation behaves the way the
    existing one does.
    """

    method: Literal["preponderance"] = Field(
        default="preponderance",
        description="Only preponderance is implemented. Named so other methods "
                    "can be added without changing stored specs.",
    )
    tie_break: Literal["lower", "higher"] = Field(
        default="lower",
        description="Which level wins when two or more levels tie on count. "
                    "'lower' reproduces the convention that an evenly split "
                    "domain takes the weaker rating.",
    )
    min_evidence_per_descriptor: int = Field(
        default=2, ge=0,
        description="Verified evidence items required before a sub-descriptor "
                    "may carry a rating at all.",
    )
    min_scored_fraction: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="Fraction of a domain's sub-descriptors that must be scored "
                    "before the domain gets a rating. Below this the domain "
                    "returns insufficient evidence rather than a rating derived "
                    "from a minority of its descriptors.",
    )

    def describe(self) -> str:
        """One-line human description, stored next to each score."""
        return (
            f"{self.method}/tie-{self.tie_break}"
            f"/min_ev={self.min_evidence_per_descriptor}"
            f"/min_scored={self.min_scored_fraction:g}"
        )


class LevelText(BaseModel):
    """Verbatim rubric wording for one sub-descriptor at one rating level.

    A list of these rather than a {level: text} map. An open-ended map is the
    natural shape, but structured-output schemas close objects to a fixed set of
    properties, which leaves a map unable to carry any keys at all — the first
    run against a real rubric came back with every entry empty and no error.
    """

    level: str = Field(description="Rating label this text belongs to.")
    text: str = Field(description="The rubric's own wording, verbatim.")


class SubDescriptor(BaseModel):
    name: str = Field(description="Sub-descriptor name as it appears in the rubric.")
    level_text: List[LevelText] = Field(
        default_factory=list,
        description="Verbatim descriptor text per rating level. May cover only "
                    "some levels; rubrics vary in how fully they enumerate.",
    )

    def text_by_level(self) -> Dict[str, str]:
        """Convenience accessor for callers that want the map shape."""
        return {lt.level: lt.text for lt in self.level_text}


class RubricDomain(BaseModel):
    name: str = Field(description="Performance area name, verbatim.")
    essential_question: Optional[str] = Field(
        default=None, description="Guiding question, where the rubric states one."
    )
    sub_descriptors: List[SubDescriptor] = Field(default_factory=list)

    @field_validator("sub_descriptors")
    @classmethod
    def _unique_names(cls, v: List[SubDescriptor]) -> List[SubDescriptor]:
        seen = [s.name.strip().lower() for s in v]
        dupes = [n for n, c in Counter(seen).items() if c > 1]
        if dupes:
            raise ValueError(f"duplicate sub-descriptor names: {sorted(dupes)}")
        return v


class RubricSpec(BaseModel):
    """A district rubric, parsed and validated.

    `rating_levels` is ordered LOW to HIGH; everything downstream derives numeric
    score from position, so the order is load-bearing rather than cosmetic.
    """

    name: str
    version: str = Field(default="1", description="District's own version label.")
    source_note: Optional[str] = Field(
        default=None,
        description="Where this came from — filename, publisher, year. Shown to "
                    "the admin approving it.",
    )
    rating_levels: List[str] = Field(min_length=2)
    domains: List[RubricDomain] = Field(min_length=1)
    aggregation: AggregationRule = Field(default_factory=AggregationRule)

    # Prompt-shaping text. Optional: a rubric that supplies none still scores.
    coaching_philosophy: Optional[str] = None
    vocabulary_examples: List[str] = Field(default_factory=list)
    core_teacher_skill_note: Optional[str] = None

    @field_validator("rating_levels")
    @classmethod
    def _levels_unique(cls, v: List[str]) -> List[str]:
        if len({x.strip().lower() for x in v}) != len(v):
            raise ValueError("rating_levels must be unique")
        return [x.strip() for x in v]

    @model_validator(mode="after")
    def _levels_referenced_exist(self) -> "RubricSpec":
        known = {lvl.lower() for lvl in self.rating_levels}
        for d in self.domains:
            for sd in d.sub_descriptors:
                for lt in sd.level_text:
                    lvl = lt.level
                    if lvl.strip().lower() not in known:
                        raise ValueError(
                            f"{d.name} / {sd.name}: descriptor text is keyed to "
                            f"{lvl!r}, which is not one of {self.rating_levels}"
                        )
        names = [d.name.strip().lower() for d in self.domains]
        dupes = [n for n, c in Counter(names).items() if c > 1]
        if dupes:
            raise ValueError(f"duplicate domain names: {sorted(dupes)}")
        return self

    # ---- derived -------------------------------------------------------

    def score_for(self, rating: str) -> int:
        """1-indexed position of a rating label. Raises on an unknown label."""
        for i, lvl in enumerate(self.rating_levels, start=1):
            if lvl.strip().lower() == (rating or "").strip().lower():
                return i
        raise ValueError(f"{rating!r} is not one of {self.rating_levels}")

    @property
    def num_levels(self) -> int:
        return len(self.rating_levels)

    @property
    def domain_names(self) -> List[str]:
        return [d.name for d in self.domains]

    def content_hash(self) -> str:
        """Stable hash of the scoring-relevant content.

        Deliberately excludes `source_note`, which is provenance rather than
        substance: re-uploading the same rubric from a differently named file
        should not read as a different instrument. Includes the aggregation
        rule, because changing how ratings combine does change what a score
        means.
        """
        payload = {
            "rating_levels": [l.strip() for l in self.rating_levels],
            "aggregation": self.aggregation.model_dump(),
            "domains": [
                {
                    "name": d.name.strip(),
                    "essential_question": (d.essential_question or "").strip(),
                    "sub_descriptors": [
                        {
                            "name": s.name.strip(),
                            "level_text": {
                                lt.level.strip(): lt.text.strip()
                                for lt in sorted(s.level_text, key=lambda x: x.level)
                            },
                        }
                        for s in d.sub_descriptors
                    ],
                }
                for d in self.domains
            ],
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(blob).hexdigest()


# ---------------------------------------------------------------------------
# Deterministic aggregation
# ---------------------------------------------------------------------------

class DomainOutcome(BaseModel):
    """Result of aggregating one domain. Carries its own justification."""

    domain: str
    rating: Optional[str] = Field(
        default=None, description="None when the domain could not be scored."
    )
    insufficient: bool = False
    counts: Dict[str, int] = Field(default_factory=dict)
    scored_count: int = 0
    total_count: int = 0
    rule: str = ""
    explanation: str = ""


def aggregate_domain(
    spec: RubricSpec,
    domain_name: str,
    descriptor_ratings: Dict[str, Optional[str]],
) -> DomainOutcome:
    """Combine sub-descriptor ratings into one domain rating.

    `descriptor_ratings` maps sub-descriptor name -> rating label, or None where
    the sub-descriptor lacked enough verified evidence to score. Unknown rating
    labels raise, because a label outside the rubric means the scoring run and
    the instrument have diverged and the result should not be trusted.
    """
    rule = spec.aggregation
    scored = {k: v for k, v in descriptor_ratings.items() if v and v != INSUFFICIENT}
    total = len(descriptor_ratings)

    base = DomainOutcome(
        domain=domain_name,
        total_count=total,
        scored_count=len(scored),
        rule=rule.describe(),
    )

    if total == 0:
        base.insufficient = True
        base.explanation = "No sub-descriptors defined for this domain."
        return base

    if len(scored) / total < rule.min_scored_fraction:
        base.insufficient = True
        base.explanation = (
            f"Only {len(scored)} of {total} sub-descriptors had enough verified "
            f"evidence to score; the rule requires at least "
            f"{rule.min_scored_fraction:g} of them."
        )
        return base

    # Validate every label before counting, so a bad label fails loudly rather
    # than silently skewing the tally.
    for name, rating in scored.items():
        spec.score_for(rating)

    counts = Counter(scored.values())
    base.counts = dict(counts)
    top = max(counts.values())
    tied = [lvl for lvl, c in counts.items() if c == top]

    if len(tied) == 1:
        winner = tied[0]
        base.rating = winner
        base.explanation = (
            f"{top} of {len(scored)} scored sub-descriptors at {winner}; "
            f"a clear plurality."
        )
        return base

    # Tie. Resolve by the configured direction rather than by model judgment.
    # This is the rule that keeps a single strong sub-descriptor from lifting a
    # domain: it can tie the count at best, and a tie breaks downward.
    tied_sorted = sorted(tied, key=spec.score_for)
    winner = tied_sorted[-1] if rule.tie_break == "higher" else tied_sorted[0]
    base.rating = winner
    base.explanation = (
        f"{top} each at {' and '.join(tied_sorted)}; the rule breaks ties "
        f"{rule.tie_break}, giving {winner}."
    )
    return base


# ---------------------------------------------------------------------------
# Bridge to the legacy Rubric object the prompt builder consumes
# ---------------------------------------------------------------------------

def _render_rubric_text(spec: "RubricSpec") -> str:
    """Render a spec as the text the scorer reads in place of a PDF.

    A built-in rubric is attached to the scoring call as its source PDF. An
    ingested one has no PDF to attach — and attaching one would reintroduce the
    licensing problem this whole path exists to remove — so the district's own
    wording is rendered from the spec instead.
    """
    lines = [f"# {spec.name}", ""]
    if spec.version:
        lines.append(f"Version: {spec.version}")
    lines.append(f"Rating levels, weakest to strongest: {', '.join(spec.rating_levels)}")
    lines.append("")
    for d in spec.domains:
        lines.append(f"## {d.name}")
        if d.essential_question:
            lines.append(f"Essential question: {d.essential_question}")
        lines.append("")
        for s in d.sub_descriptors:
            lines.append(f"### {s.name}")
            by_level = s.text_by_level()
            for lvl in spec.rating_levels:
                if lvl in by_level:
                    lines.append(f"- **{lvl}:** {by_level[lvl]}")
            lines.append("")
    return "\n".join(lines).strip()


def _render_scoring_notes(spec: "RubricSpec") -> str:
    """State the aggregation rule to the model as context.

    The model does not apply this — aggregate_domain does — but it explains why
    a sub-descriptor judgment matters and discourages the model from reaching
    for a domain-level verdict of its own.
    """
    rule = spec.aggregation
    tie = "the LOWER" if rule.tie_break == "lower" else "the HIGHER"
    return (
        f"Rate each sub-descriptor independently and support each with evidence. "
        f"Do NOT decide the overall rating for a performance area — that is "
        f"computed from your sub-descriptor ratings by a fixed rule: the level "
        f"held by the most sub-descriptors wins, and a tie takes {tie} level. "
        f"A sub-descriptor with fewer than {rule.min_evidence_per_descriptor} "
        f"pieces of verifiable evidence should be left unrated rather than "
        f"guessed; an honest gap is more useful than a fabricated score."
    )


def spec_to_rubric(spec: "RubricSpec", rubric_id: str):
    """Build the legacy Rubric the prompt builder expects from a RubricSpec."""
    from .rubric import Rubric

    return Rubric(
        id=rubric_id,
        name=spec.name,
        pdf_path=None,
        domains=spec.domain_names,
        rating_levels=list(spec.rating_levels),
        essential_questions={
            d.name: (d.essential_question or "") for d in spec.domains
        },
        core_teacher_skill_note=spec.core_teacher_skill_note or "",
        vocabulary_examples=list(spec.vocabulary_examples),
        coaching_philosophy=spec.coaching_philosophy or "",
        scoring_notes=_render_scoring_notes(spec),
        sub_descriptor_hints={
            d.name: [s.name for s in d.sub_descriptors] for d in spec.domains
        },
        rubric_text=_render_rubric_text(spec),
    )
