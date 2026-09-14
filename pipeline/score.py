"""Score a classroom observation against a pluggable rubric using Claude.

This module is RUBRIC-AGNOSTIC. It takes a ``Rubric`` instance (see
``pipeline.rubric``) and generates a per-run system prompt and JSON schema
that reflect that rubric's specific domains, rating levels, vocabulary, and
coaching philosophy. Add a new rubric by adding a ``Rubric(...)`` instance to
``pipeline.rubric.RUBRICS`` — no changes needed here.
"""
import base64
import os
from pathlib import Path
from typing import List, Optional

import anthropic
from pydantic import ValidationError

from .frames import SampledFrame
from .rubric import Rubric
from .schemas import ObservationReport, validate_against_rubric
from .transcribe import TranscriptSegment, to_dialogue_text


MODEL = "claude-opus-4-7"


def _build_system_prompt(rubric: Rubric, teacher_context_block: Optional[str] = None) -> str:
    """Build the system prompt from the rubric's fields.

    Everything rubric-specific — domain names, rating levels, vocabulary
    examples, coaching philosophy — is injected here so the same prompt
    scaffold works across rubrics.

    ``teacher_context_block`` (optional) is a pre-rendered markdown block
    from ``pipeline.context.render_context_for_prompt``. When present, it is
    appended after the rubric scaffolding so the AI can weight its scoring
    and recommendations against the teacher's stage, goals, prior trajectory,
    and district context. When absent (legacy path), the prompt behaves as
    it did in the rubric-only era.
    """
    domains_list = "\n".join(
        f"- {d}: {rubric.essential_questions.get(d, '')}"
        for d in rubric.domains
    )
    ratings_list = " → ".join(rubric.rating_levels)
    vocab_bullets = "\n".join(f"   - \"{v}\"" for v in rubric.vocabulary_examples)

    return f"""You are an expert instructional coach trained on the {rubric.name}.

Your job: score a single classroom observation against this rubric and produce a structured assessment whose feedback is tightly anchored in the rubric's own language.

The rubric is attached as a PDF document. It is the authoritative source for:
- The {len(rubric.domains)} performance areas:
{domains_list}
- The descriptor language at each of the {rubric.num_levels} rating levels ({ratings_list}).
- {rubric.core_teacher_skill_note}

# Output structure

You produce a structured JSON object (schema enforced). Top-level fields:
- `opening_paragraph` — neutral lesson context (no rubric judgments).
- `overall_summary` — 2-3 paragraph executive summary using rubric language.
- `domain_assessments` — EXACTLY {len(rubric.domains)} entries, one per performance area, in rubric order.
- `coaching_recommendations` — 1-2 prioritized coaching-construct entries.

The `domain_assessments` list is the heart of the report. Each entry must be COMPLETE and contain every required field — NO placeholders, NO empty strings, NO "TBD" values. If you write the literal word "placeholder" anywhere, the output will be rejected.

# Part 1: Scoring procedure (within each DomainAssessment)

**Step 1: Score each sub-descriptor.** Cite ≥2 pieces of evidence with [MM:SS] timestamps in each. Match observed behavior against the descriptor language at each of the {rubric.num_levels} levels. Pick the level the evidence most closely matches.

**Step 2: Count and assign overall rating.** Open `preponderance_summary` with an explicit count (e.g., "X of N sub-descriptors at {rubric.rating_levels[1]}, Y at {rubric.rating_levels[2]}"), then assign `overall_rating`.

**Scoring rules (from the rubric):**
{rubric.scoring_notes}

# Part 2: Rubric-tight narrative (within each DomainAssessment)

For every domain, complete these four narrative fields with the same care as the scoring:

1. **`rubric_descriptor_text`** — Quote VERBATIM from the rubric PDF the descriptor sentence(s) for the rating level you assigned. Pull the actual text from the PDF. Do NOT paraphrase loosely. If the rubric has multiple sub-items at this level, concatenate 2-3 of them.

2. **`what_was_observed`** — 3-6 sentences in rubric language ONLY. Allowed vocabulary includes phrases such as:
{vocab_bullets}
   - Names from the rubric's coaching-construct list under this performance area.

   Cite [MM:SS] timestamps; quote the transcript directly when helpful. BANNED: metaphors, similes, coach-poetry, aphorisms. If a phrase does not appear in the rubric's descriptor language or its coaching-construct list, do not use it.

3. **`distance_from_target`** — 1-2 sentences. Frame: "To move from [current] to [next], the rubric requires [paraphrase or quote next-level descriptor]." If already at {rubric.top_rating}, describe what would jeopardize that rating.

4. **`relevant_core_teacher_skills`** — 1-4 coaching-construct names (EXACT phrasing from the rubric's bullets under this performance area). Do not invent.

# Part 3: Coaching recommendations

Coaching philosophy:
{rubric.coaching_philosophy}

Each `CoachingRecommendation`:
- `core_teacher_skill` MUST already appear in the `relevant_core_teacher_skills` list of at least one domain assessment.
- `related_domain` MUST be one of the {len(rubric.domains)} performance areas listed above.
- `bite_sized_action`: specific, observable, executable within one week.
- `success_indicators`: 2-3 observable signs of progress for the next visit.

# Part 4: Evidence rules

- Every sub-descriptor cites at least 2 evidence items with [MM:SS] timestamps.
- Quote directly from the transcript when citing speech. Describe frames briefly when citing visuals.
- Mark each evidence item as source: `transcript`, `frame`, or `both`.
- Be specific. "Students seemed engaged" is not evidence. "[12:34] Six of eight visible students are looking at the teacher; two are working on the worksheet" is evidence.

# Part 5: Pre-submission check

Before finalizing, verify:
1. All {len(rubric.domains)} performance areas appear in `domain_assessments`, in rubric order, no duplicates.
2. Every `rubric_descriptor_text` quotes the rubric PDF, not paraphrased.
3. Every `what_was_observed` uses rubric vocabulary — strike any line that slipped into coach-poetry.
4. `coaching_recommendations` skills already appear in some domain's `relevant_core_teacher_skills`.
5. Every rating value is one of: {", ".join(rubric.rating_levels)}.
6. Every domain name is one of: {", ".join(rubric.domains)}.
7. No field contains the word "placeholder" or any abbreviated content.
{_context_aware_output_section() if teacher_context_block else ''}
{teacher_context_block or ''}"""


def _context_aware_output_section() -> str:
    """The two new context-aware output fields, described only when relational
    context is available. Kept out of the rubric-only path so legacy scoring
    behavior is unchanged."""
    return """

# Part 6: Context-aware output fields (only populated when relational context is provided below)

When relational context is provided in the section that follows, ALSO populate:

**`highest_leverage_move`** — the SINGLE most-impactful coaching move for THIS teacher AT THIS moment,
synthesizing the observation evidence with the teacher's stage, active goals, prior trajectory,
prior action follow-through, and district priorities. This is not just the top coaching_recommendation;
it should be more specific, more contextualized, and reference at least 2 specific context factors in
its rationale. Fields:
  - `move`: the concrete action.
  - `related_core_teacher_skill`: exact rubric name.
  - `related_domain`: performance area.
  - `rationale`: why THIS move over any other, with explicit reference to at least 2 context factors.
  - `contextual_factors`: enumerate the specific context items that shaped it (teacher stage, active goal, prior pattern, district priority).
  - `success_signals_next_visit`: 2-3 concrete signs.

**`prior_action_assessments`** — one entry per prior bite-sized action listed in the context. For each:
  - `tracking_id`: copy the ID verbatim from the context injection.
  - `core_teacher_skill`: copy from the tracking record.
  - `implementation`: full / partial / not_observed / regressed.
  - `evidence_notes`: 2-3 sentences of specific evidence from THIS observation supporting the rating.

If no prior actions are provided, leave `prior_action_assessments` as an empty list."""


# ---------------------------------------------------------------------------
# JSON schema sanitization + rubric-specific enum injection
# ---------------------------------------------------------------------------

_STRIP_KEYS = {
    "minLength", "maxLength",
    "minimum", "maximum", "multipleOf",
    "maxItems",
    "uniqueItems",
}


def _sanitize_schema(schema: dict, rubric: Rubric) -> dict:
    """Make a Pydantic-generated JSON schema acceptable to the structured-outputs API,
    then inject rubric-specific enum constraints on domain and rating fields.

    Also handles the API's constraints:
      - ``additionalProperties: false`` on every object.
      - Strips string-length, numeric, and array-size constraints.
      - Only ``minItems`` of 0 or 1 is allowed.

    Pydantic's own field-validator constraints still run client-side.
    """
    def walk(node: dict) -> dict:
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
                    walk(v)
            if "items" in node and isinstance(node["items"], dict):
                walk(node["items"])
            for key in ("anyOf", "allOf", "oneOf"):
                for v in node.get(key, []):
                    walk(v)
        return node

    walk(schema)

    # Inject rubric-specific enums for the domain and rating fields. This constrains
    # the model at the API level so it can't invent domain names or rating labels.
    def add_enum_to(defs_name: str, field_name: str, values: List[str]) -> None:
        defs = schema.get("$defs") or schema.get("definitions") or {}
        model = defs.get(defs_name)
        if not model:
            return
        prop = model.get("properties", {}).get(field_name)
        if prop:
            prop["enum"] = values

    add_enum_to("DomainAssessment", "domain", rubric.domains)
    add_enum_to("DomainAssessment", "overall_rating", rubric.rating_levels)
    add_enum_to("DescriptorScore", "rating", rubric.rating_levels)
    add_enum_to("CoachingRecommendation", "related_domain", rubric.domains)

    return schema


# ---------------------------------------------------------------------------
# User content builders
# ---------------------------------------------------------------------------


def _encode_image(path: Path) -> dict:
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def _encode_pdf(path: Path, title: str) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("utf-8")
    return {
        "type": "document",
        "source": {"type": "base64", "media_type": "application/pdf", "data": data},
        "title": title,
    }


def _build_user_content(
    rubric: Rubric,
    transcript: List[TranscriptSegment],
    frames: List[SampledFrame],
    video_filename: str,
    duration_seconds: float,
) -> List[dict]:
    """User message: rubric PDF (cached), then transcript, then frames."""
    content: List[dict] = []

    rubric_block = _encode_pdf(rubric.pdf_path, title=rubric.name)
    rubric_block["cache_control"] = {"type": "ephemeral"}
    content.append(rubric_block)

    duration_label = f"{int(duration_seconds // 60):02d}:{int(duration_seconds % 60):02d}"
    header = (
        f"# Classroom observation to score\n\n"
        f"- Rubric: {rubric.name}\n"
        f"- Source video: `{video_filename}`\n"
        f"- Duration: {duration_label}\n"
        f"- Sampled frames: {len(frames)} (one per ~minute, in order)\n"
        f"- Transcript segments: {len(transcript)}\n\n"
        f"## Transcript (timestamped)\n\n"
        f"```\n{to_dialogue_text(transcript)}\n```\n\n"
        f"## Sampled frames\n\n"
        f"Frames are provided below in chronological order with their timestamps. "
        f"Use them to assess visible behaviors (engagement, layout, student work, board content) "
        f"that audio alone cannot capture."
    )
    content.append({"type": "text", "text": header})

    for frame in frames:
        content.append({
            "type": "text",
            "text": f"### Frame at [{frame.timestamp_label}]"
        })
        content.append(_encode_image(frame.path))

    content.append({
        "type": "text",
        "text": (
            f"Now produce the structured observation report. Follow the {rubric.name} exactly, "
            f"score each of the {len(rubric.domains)} performance areas with descriptor-level "
            f"evidence, and finish with 1-2 prioritized coaching-construct entries and a "
            f"bite-sized action for the next week."
        ),
    })
    return content


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def _run_scoring_call(
    *,
    client: anthropic.Anthropic,
    rubric: Rubric,
    user_content: list,
    schema: dict,
    system_prompt: str,
    max_tokens: int,
    effort: str,
    attempt_label: str,
) -> ObservationReport:
    """Single scoring attempt. Raises on incomplete stop_reason, empty text,
    Pydantic validation failure, OR rubric-specific validation failure.
    """
    import json as _json
    from pathlib import Path as _Path

    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        system=system_prompt,
        messages=[{"role": "user", "content": user_content}],
    ) as stream:
        final = stream.get_final_message()

    diag_dir = _Path(os.environ.get("OBSERVER_DIAG_DIR", "/tmp"))
    diag_dir.mkdir(parents=True, exist_ok=True)
    diag_path = diag_dir / f"observer_last_response_{attempt_label}.json"
    diag = {
        "attempt": attempt_label,
        "rubric_id": rubric.id,
        "effort": effort,
        "max_tokens": max_tokens,
        "stop_reason": final.stop_reason,
        "stop_sequence": getattr(final, "stop_sequence", None),
        "usage": final.usage.model_dump() if hasattr(final.usage, "model_dump") else dict(final.usage),
        "content_blocks": [
            {"type": b.type, "preview": (b.text[:500] if b.type == "text" else "")}
            for b in final.content
        ],
        "text_concatenated": "".join(b.text for b in final.content if b.type == "text"),
    }
    diag_path.write_text(_json.dumps(diag, indent=2, default=str))
    print(f"  [{attempt_label}] Diagnostic: {diag_path}")
    print(f"  [{attempt_label}] stop_reason={final.stop_reason}, "
          f"content_types={[b.type for b in final.content]}")
    print(f"  [{attempt_label}] usage: {diag['usage']}")

    if final.stop_reason not in ("end_turn",):
        text_preview = next(
            (b.text[:200] for b in final.content if b.type == "text"), ""
        )
        raise RuntimeError(
            f"[{attempt_label}] Scoring call did not complete cleanly. "
            f"stop_reason={final.stop_reason!r}. Preview: {text_preview!r}"
        )

    text_block = "".join(b.text for b in final.content if b.type == "text")
    if not text_block.strip():
        raise RuntimeError(
            f"[{attempt_label}] No text content in response. "
            f"content_types={[b.type for b in final.content]}"
        )

    report = ObservationReport.model_validate_json(text_block)
    # Rubric-specific validation — raises ValueError on mismatched domain/rating names.
    validate_against_rubric(report, rubric)
    return report


def score_observation(
    rubric: Rubric,
    transcript: List[TranscriptSegment],
    frames: List[SampledFrame],
    video_filename: str,
    duration_seconds: float,
    client: Optional[anthropic.Anthropic] = None,
    max_tokens: int = 32000,
    teacher_context_block: Optional[str] = None,
) -> ObservationReport:
    """Score one observation against the given rubric.

    Uses streaming with ``output_config.format`` so the server enforces the JSON
    schema as it streams. Auto-retries once with escalated effort + max_tokens
    if the first attempt produces a truncated response (Pydantic ValidationError
    from missing/short fields — the Lopez failure mode).

    When ``teacher_context_block`` is provided (pre-rendered via
    ``pipeline.context.render_context_for_prompt``), the system prompt is
    extended with the context and asks the model to additionally populate
    ``highest_leverage_move`` and ``prior_action_assessments``.
    """
    # SDK retries handle transient API errors; the escalation retry below handles
    # model-produced truncated output.
    client = client or anthropic.Anthropic(max_retries=6)

    system_prompt = _build_system_prompt(rubric, teacher_context_block=teacher_context_block)
    user_content = _build_user_content(rubric, transcript, frames, video_filename, duration_seconds)
    schema = _sanitize_schema(ObservationReport.model_json_schema(), rubric)

    # Attempt 1: default effort.
    try:
        return _run_scoring_call(
            client=client,
            rubric=rubric,
            user_content=user_content,
            schema=schema,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            effort="high",
            attempt_label="attempt1_high",
        )
    except (ValidationError, ValueError) as e:
        # Model produced a schema-invalid or rubric-invalid response (usually because
        # it bailed partway). Escalate.
        err_summary = (
            f"{e.error_count()} pydantic error(s)" if isinstance(e, ValidationError)
            else str(e).split("\n")[0]
        )
        print(f"  Attempt 1 failed validation: {err_summary}. "
              f"Retrying with effort=xhigh, max_tokens={max_tokens * 2}...")

    # Attempt 2: xhigh effort, doubled max_tokens.
    return _run_scoring_call(
        client=client,
        rubric=rubric,
        user_content=user_content,
        schema=schema,
        system_prompt=system_prompt,
        max_tokens=max_tokens * 2,
        effort="xhigh",
        attempt_label="attempt2_xhigh",
    )
