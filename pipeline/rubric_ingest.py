"""Parse an arbitrary district rubric document into a validated RubricSpec.

The product shipped with one rubric compiled into the source tree, its PDF
bundled and attached to every scoring call. That is a licensing problem — the
instrument belongs to its publisher — and a product ceiling, since a district
that scores on its own framework could not use the tool at all.

This module takes the text of whatever rubric a district already owns and
produces a RubricSpec: structured, validated, content-hashed, and reviewable by
a human before anything is scored against it. Parsing is a model call; accepting
the result is a person's decision. `review_warnings` exists to make that review
informed rather than ceremonial, because a plausible-looking rubric that quietly
dropped half its sub-descriptors would otherwise be approved on sight.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple

import anthropic
from pydantic import ValidationError

from .rubric_spec import AggregationRule, RubricSpec

MODEL = "claude-opus-4-7"

# The structured-outputs API rejects several JSON Schema keywords that Pydantic
# emits. Scoring has its own sanitizer that also injects rubric-specific enums;
# this one is deliberately separate and generic, so changing ingestion can never
# perturb the scoring path.
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


SYSTEM_PROMPT = """You convert a teacher-observation rubric into a structured schema.

You are transcribing, not authoring. Every name and every piece of descriptor
text must come from the document. Do not invent a domain, a sub-descriptor, or a
rating level that is not there, do not improve the wording, and do not fill a
gap the document leaves empty.

# What to extract

- `rating_levels`: the performance levels, ordered LOW to HIGH. This order is
  load-bearing — scores are derived from position — so read carefully. Many
  rubrics print their strongest level first; your output must still run weakest
  to strongest.
- `domains`: the performance areas, in document order, each with its guiding or
  essential question if the rubric states one.
- `sub_descriptors`: the scored elements within each domain. For each, capture
  `level_text` — the rubric's own wording at each level, verbatim. Where the
  document gives text for only some levels, include only those.
- `coaching_philosophy`, `vocabulary_examples`, `core_teacher_skill_note`: only
  if the document actually discusses coaching approach or characteristic
  language. Leave empty otherwise.

# Aggregation

If the document states how sub-descriptor ratings combine into an overall rating
for a domain, set `aggregation.tie_break`: "lower" when an even split takes the
weaker rating, "higher" otherwise. If the document says nothing about it, leave
it alone.

Set nothing else under `aggregation`. The evidence thresholds there are the
platform's policy rather than the rubric's content, and values you supply for
them are discarded.

# Rules

- Verbatim means verbatim. Preserve the rubric's wording, including wording you
  would phrase differently.
- A rubric with four levels and three domains produces exactly four levels and
  three domains. Completeness matters more than tidiness.
- If a section is unreadable or the document does not appear to be a rubric at
  all, return what you can and leave the rest empty. A human reviews this before
  it is used; a partial honest parse is useful and an invented one is not."""


def _build_content(source) -> list:
    """Build the user content block for a rubric document.

    A PDF is attached natively rather than text-extracted. Rubrics are matrices,
    and the thing that matters most — which column and which row a descriptor
    belongs to — lives in the page layout. Worse, publishers often render the
    domain names as images or rotated sidebar labels: extracting the TNTP rubric
    with `pdftotext -layout` yields every descriptor sentence and not one of the
    four domain names, which parses into a rubric with no domains to hang
    anything on. The model reads the rendered page, so it sees both.
    """
    if isinstance(source, Path):
        if source.suffix.lower() == ".pdf":
            data = base64.standard_b64encode(source.read_bytes()).decode("utf-8")
            return [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": data},
                    "title": source.name,
                },
                {"type": "text", "text": "Convert the attached rubric into the schema."},
            ]
        from .text_extract import extract_text
        text = extract_text(source)
        if not text:
            raise ValueError(
                f"Could not read text from {source.name}. Supported: PDF, DOCX, "
                f"TXT, MD. A scanned image-only file in another format will not "
                f"extract — convert it to PDF first."
            )
        source = text

    if not source or len(str(source).strip()) < 200:
        raise ValueError(
            "Document text is too short to be a rubric. Extraction may have "
            "failed — a scanned file with no text layer produces this."
        )
    return [{"type": "text", "text": f"<rubric_document>\n{source}\n</rubric_document>"}]


def parse_rubric_document(
    source,
    *,
    source_note: Optional[str] = None,
    client: Optional[anthropic.Anthropic] = None,
    max_tokens: int = 32000,
) -> RubricSpec:
    """Parse a rubric document into a validated RubricSpec.

    `source` is a Path to the rubric file (preferred — a PDF is sent to the
    model intact) or a string of already-extracted text.

    Raises ValueError when the document cannot be read, and ValidationError when
    the model returns something that is not a usable spec.
    """
    content = _build_content(source)
    client = client or anthropic.Anthropic(max_retries=4)
    schema = _sanitize_schema(RubricSpec.model_json_schema())

    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    ) as stream:
        final = stream.get_final_message()

    raw = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")

    diag_dir = Path(os.environ.get("OBSERVER_DIAG_DIR", "/tmp"))
    try:
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / "observer_last_rubric_parse.json").write_text(raw)
        (diag_dir / "observer_last_rubric_meta.json").write_text(json.dumps({
            "stop_reason": getattr(final, "stop_reason", None),
            "usage": getattr(final, "usage", None).model_dump()
                     if hasattr(getattr(final, "usage", None), "model_dump") else None,
            "raw_chars": len(raw),
        }, indent=2, default=str))
    except OSError:
        pass  # diagnostics are a convenience, never a reason to fail the parse

    # A truncated response is a budget problem, not a bad rubric. Say so plainly
    # rather than surfacing a confusing Pydantic error about missing fields.
    if getattr(final, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"The model hit its output limit after {len(raw)} characters while "
            f"transcribing this rubric. Retry with a larger max_tokens."
        )

    spec = RubricSpec.model_validate_json(raw)

    # tie_break is the rubric's business; evidence thresholds are ours. A parse
    # of the TNTP rubric came back having quietly lowered
    # min_evidence_per_descriptor to 1, which would have weakened every score
    # made against it. Keep what the document can legitimately say and reset the
    # rest to platform defaults, which an administrator can still override.
    policy = AggregationRule(tie_break=spec.aggregation.tie_break,
                             method=spec.aggregation.method)
    spec.aggregation = policy

    if source_note:
        spec.source_note = source_note
    return spec


def review_warnings(spec: RubricSpec) -> List[str]:
    """Things a human should look at before approving this rubric.

    Not validation — each of these describes a spec that is structurally legal
    but may indicate the parse lost something. The goal is that an administrator
    approving a rubric knows where to look rather than skimming and clicking.
    """
    out: List[str] = []

    empty = [d.name for d in spec.domains if not d.sub_descriptors]
    if empty:
        out.append(
            f"{len(empty)} domain(s) have no sub-descriptors and cannot be "
            f"scored: {', '.join(empty)}. Check whether the document lists them "
            f"in a layout the parser missed."
        )

    for d in spec.domains:
        thin = [s.name for s in d.sub_descriptors if not s.level_text]
        if thin:
            out.append(
                f"{d.name}: {len(thin)} sub-descriptor(s) carry no descriptor "
                f"text at any level ({', '.join(thin[:3])}"
                f"{'…' if len(thin) > 3 else ''}). Scoring will have no rubric "
                f"language to quote."
            )

    partial = []
    for d in spec.domains:
        for s in d.sub_descriptors:
            if s.level_text and len(s.level_text) < spec.num_levels:
                partial.append(f"{d.name}/{s.name} ({len(s.level_text)}/{spec.num_levels})")
    if partial:
        out.append(
            f"{len(partial)} sub-descriptor(s) have text for only some levels: "
            f"{', '.join(partial[:4])}{'…' if len(partial) > 4 else ''}. Normal "
            f"for some rubrics; a parsing gap in others."
        )

    missing_eq = [d.name for d in spec.domains if not d.essential_question]
    if missing_eq and len(missing_eq) < len(spec.domains):
        out.append(
            f"Some domains have a guiding question and these do not: "
            f"{', '.join(missing_eq)}. Worth confirming the document really "
            f"omits them."
        )

    if len(spec.domains) < 2:
        out.append(
            "Only one domain was found. Most observation rubrics have several; "
            "this often means extraction captured part of the document."
        )

    # Ordering is the single most consequential thing to get wrong: reversed
    # levels invert every score the rubric will ever produce.
    weak_words = ("ineffective", "unsatisfactory", "beginning", "below", "needs", "emerging", "level 1")
    strong_words = ("effective", "exemplary", "distinguished", "advanced", "proficient", "mastery")
    first, last = spec.rating_levels[0].lower(), spec.rating_levels[-1].lower()
    if any(w in first for w in strong_words) and not any(w in first for w in weak_words):
        out.append(
            f"Check level order. {spec.rating_levels[0]!r} is listed as the "
            f"LOWEST level and {spec.rating_levels[-1]!r} as the highest. If "
            f"that is backwards, every score will be inverted."
        )

    return out


def summarize(spec: RubricSpec) -> str:
    """One-line summary for logs and CLI output."""
    n_sub = sum(len(d.sub_descriptors) for d in spec.domains)
    return (
        f"{spec.name} v{spec.version} — {len(spec.domains)} domains, "
        f"{n_sub} sub-descriptors, {spec.num_levels} levels "
        f"({spec.rating_levels[0]} → {spec.rating_levels[-1]}), "
        f"hash {spec.content_hash()[:12]}"
    )
