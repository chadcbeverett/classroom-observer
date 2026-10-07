"""Did the practised move actually show up in the next lesson?

The loop names a move, the coach rehearses it, the teacher teaches again — and
until now nothing looked for the move in that next lesson. Implementation was a
coach ticking a box. This counts.

Three things keep it honest.

The model counts instances; it does not decide the status. It returns the
moments where the move was called for and the moments it was used, each with a
quoted line, and `derive_status` does the arithmetic. Ambiguity resolves
downward, the same convention the rubric aggregation uses: a teacher is not
credited with a habit on thin evidence.

Claimed uses are verified against the transcript before they count. A use is a
quoted line, so it is checkable by the same machinery as rubric evidence, and
an instance that cannot be found where it was cited does not support a claim
that the move is becoming automatic.

Some moves cannot be checked this way at all. Proximity, scanning the room,
where a teacher stands — none of it is in a transcript, and sparse frames will
not carry it either. The model is asked to say so rather than guess, and an
undetectable move returns a status of `not_checkable` so the coach assesses it
themselves instead of reading a fabricated count.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from statistics import median
from typing import List, Literal, Optional, Sequence

import anthropic
from pydantic import BaseModel, Field

MODEL = "claude-opus-4-7"

# Status ladder, weakest to strongest. `not_checkable` sits outside it.
STATUSES = ("not_yet_seen", "attempted", "consistent", "automatic")

# A move must be used in at least this share of the moments that called for it
# before it reads as consistent rather than attempted.
CONSISTENT_USE_RATIO = 0.6

# And at least this many verified uses, so a single lucky instance in a lesson
# with one opportunity cannot read as a habit.
CONSISTENT_MIN_USES = 2

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


class MoveUse(BaseModel):
    """One moment the teacher used the move."""

    timestamp: str = Field(description="MM:SS where the move happens.")
    quote: str = Field(
        description="The teacher's words at that moment, verbatim from the "
        "transcript. This is checked against the transcript afterwards, so it "
        "must be what was said rather than a summary of it."
    )
    trigger_timestamp: Optional[str] = Field(
        default=None,
        description="MM:SS of what called for the move — a wrong answer, a "
        "silence, an off-task moment — when that is identifiable. Leave empty "
        "when the move was not a response to anything locatable.",
    )


class MissedOpportunity(BaseModel):
    """A moment that called for the move where the teacher did something else."""

    timestamp: str = Field(description="MM:SS of the moment.")
    quote: str = Field(
        description="What was said instead, verbatim from the transcript."
    )
    what_happened: str = Field(
        description="One sentence on what the teacher did instead of the move."
    )


class TransferObservation(BaseModel):
    """The model's raw count. It proposes no status."""

    move_as_understood: str = Field(
        description="Your reading of what you were looking for, in one line. "
        "Lets the coach see whether you looked for the right thing."
    )
    detectable_in_transcript: bool = Field(
        description="False when this move cannot be seen in a transcript — "
        "where the teacher stands, who they look at, how they circulate. Say "
        "false rather than guessing; a count nobody can trust is worse than "
        "an honest gap."
    )
    not_detectable_reason: Optional[str] = Field(
        default=None,
        description="When not detectable, one line on why, written for the coach.",
    )
    uses: List[MoveUse] = Field(
        default_factory=list,
        description="Every moment the teacher used the move. Empty if none.",
    )
    missed: List[MissedOpportunity] = Field(
        default_factory=list,
        description="Every moment that called for the move where it was not used.",
    )


class TransferResult(BaseModel):
    """What the system records: counts, verified uses, derived status."""

    move_as_understood: str
    detectable: bool
    not_detectable_reason: Optional[str] = None
    opportunities: int = 0
    uses_claimed: int = 0
    uses_verified: int = 0
    median_latency_seconds: Optional[float] = None
    status: str = "not_yet_seen"
    rationale: str = ""
    uses: List[dict] = Field(default_factory=list)
    missed: List[dict] = Field(default_factory=list)


SYSTEM_PROMPT = """You are checking one specific thing: whether a teacher used a
particular coaching move during this lesson.

You are not scoring the lesson. You are not assessing teaching quality. You are
counting instances of one named move and the moments that called for it.

# What to return

`move_as_understood`: restate, in one line, what you looked for. The coach reads
this to check you looked for the right thing.

`detectable_in_transcript`: some moves leave no trace in a transcript — where a
teacher stands, who they make eye contact with, how they circulate during
independent work. If this move is one of those, say false and explain why in
`not_detectable_reason`. Do not estimate. A coach told "this cannot be checked
from audio" can assess it themselves; a coach given an invented count cannot.

`uses`: every moment the teacher performed the move. Quote the teacher's actual
words from the transcript — these are checked against it afterwards, and a
paraphrase in quotation marks will fail that check and be discarded. Where the
move was a response to something identifiable — a wrong answer, a silence, an
off-task moment — give that moment's timestamp as `trigger_timestamp`.

`missed`: every moment that called for the move where the teacher did something
else. Quote what was said instead. These matter as much as the uses: a move used
twice in two opportunities is a different result from a move used twice in
eleven.

# How to count

Count opportunities honestly, including the ones the teacher took. A lesson with
no opportunities is a real outcome — say so with empty lists rather than
stretching to find something.

Do not decide whether the move is consistent, automatic, or anything else. That
is computed from your counts. Your job is the counting, and a count that leans
generous corrupts the result more than a low one."""


def _user_content(move_text: str, transcript_text: str, duration_s: Optional[float]) -> str:
    dur = ""
    if duration_s:
        dur = f"\nThe lesson runs {int(duration_s // 60):02d}:{int(duration_s % 60):02d}.\n"
    return (
        f"## The move the teacher was practising\n\n{move_text.strip()}\n{dur}\n"
        f"## Transcript\n\n```\n{transcript_text}\n```\n\n"
        f"Count the uses and the missed opportunities."
    )


def derive_status(
    *,
    uses_verified: int,
    opportunities: int,
    detectable: bool,
    prior_status: Optional[str] = None,
) -> tuple:
    """Derive the status from counts. Returns (status, rationale).

    Deliberately not the model's call. The rules mirror the rubric aggregation:
    explicit counts, no rounding up, ambiguity resolves to the lower status.
    """
    if not detectable:
        return ("not_checkable",
                "This move does not leave a trace in a transcript, so it was not counted.")

    if uses_verified == 0:
        if opportunities == 0:
            return ("not_yet_seen",
                    "No moment in this lesson called for the move, and it was not used.")
        return ("not_yet_seen",
                f"{opportunities} moment(s) called for the move; it was not used in any of them.")

    total = max(opportunities, uses_verified)
    ratio = uses_verified / total if total else 0.0

    if uses_verified < CONSISTENT_MIN_USES:
        return ("attempted",
                f"Used {uses_verified} time(s) in {total} opportunity(ies) — a start, but "
                f"{CONSISTENT_MIN_USES} verified uses are needed before it reads as consistent.")

    if ratio < CONSISTENT_USE_RATIO:
        return ("attempted",
                f"Used {uses_verified} of {total} times ({ratio:.0%}). Consistent requires "
                f"{CONSISTENT_USE_RATIO:.0%} of the moments that called for it.")

    # Consistent twice running is what distinguishes a habit from a good day.
    if prior_status in ("consistent", "automatic"):
        return ("automatic",
                f"Used {uses_verified} of {total} times ({ratio:.0%}), and consistent in the "
                f"previous lesson too — it is holding without prompting.")

    return ("consistent",
            f"Used {uses_verified} of {total} times ({ratio:.0%}) in this lesson. One more "
            f"lesson at this level and it reads as automatic.")


def _latency(uses: Sequence, parse) -> Optional[float]:
    """Median seconds from trigger to move, over uses that name a trigger."""
    gaps = []
    for u in uses:
        t_use = parse(getattr(u, "timestamp", None) or u.get("timestamp"))
        t_trig = parse(getattr(u, "trigger_timestamp", None) or u.get("trigger_timestamp"))
        # A strictly positive gap only. When the model gives the same MM:SS for
        # the trigger and the move it has echoed the timestamp rather than
        # measured anything, and a reported latency of 0.0 reads to a coach as
        # "answered instantly" when it means "not measured".
        if t_use is not None and t_trig is not None and t_use > t_trig:
            gaps.append(t_use - t_trig)
    return median(gaps) if gaps else None


def check_move_transfer(
    *,
    move_text: str,
    transcript,
    duration_seconds: Optional[float] = None,
    prior_status: Optional[str] = None,
    frames: Sequence = (),
    client: Optional[anthropic.Anthropic] = None,
    max_tokens: int = 8000,
) -> TransferResult:
    """Count uses of `move_text` in a lesson, verify them, and derive a status.

    `transcript` is a sequence of segments with .start/.end/.text. Verification
    runs against the same segments, so a claimed use that is not in the
    transcript where it was cited does not count toward the status.
    """
    from .verify import parse_timestamp, verify_evidence

    if not (move_text or "").strip():
        raise ValueError("Cannot check transfer without a move.")

    text = "\n".join(
        f"[{int(getattr(s, 'start', 0) // 60):02d}:{int(getattr(s, 'start', 0) % 60):02d}] "
        f"{getattr(s, 'text', '')}" for s in transcript
    )

    client = client or anthropic.Anthropic(max_retries=4)
    schema = _sanitize_schema(TransferObservation.model_json_schema())

    with client.messages.stream(
        model=MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": "high", "format": {"type": "json_schema", "schema": schema}},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_content(move_text, text, duration_seconds)}],
    ) as stream:
        final = stream.get_final_message()

    raw = "".join(b.text for b in final.content if getattr(b, "type", None) == "text")
    diag = Path(os.environ.get("OBSERVER_DIAG_DIR", "/tmp"))
    try:
        diag.mkdir(parents=True, exist_ok=True)
        (diag / "observer_last_transfer_check.json").write_text(raw)
    except OSError:
        pass

    if getattr(final, "stop_reason", None) == "max_tokens":
        raise ValueError(
            f"The model hit its output limit after {len(raw)} characters counting "
            f"this move. Retry with a larger max_tokens."
        )

    obs = TransferObservation.model_validate_json(raw)

    # Verify each claimed use against the transcript. An unverifiable instance
    # is reported but does not count toward the status.
    verified_uses = []
    for u in obs.uses:
        verdict = verify_evidence(
            {"timestamp": u.timestamp, "source": "transcript",
             "quote_or_description": f'"{u.quote}"'},
            segments=transcript, frames=frames, duration_s=duration_seconds,
        )
        verified_uses.append({
            "timestamp": u.timestamp,
            "quote": u.quote,
            "trigger_timestamp": u.trigger_timestamp,
            "verified": verdict.counts_toward_score,
            "verify_reason": verdict.reason,
        })

    n_verified = sum(1 for u in verified_uses if u["verified"])
    opportunities = len(obs.missed) + len(obs.uses)

    status, rationale = derive_status(
        uses_verified=n_verified,
        opportunities=opportunities,
        detectable=obs.detectable_in_transcript,
        prior_status=prior_status,
    )

    dropped = len(verified_uses) - n_verified
    if dropped and obs.detectable_in_transcript:
        rationale += (f" {dropped} further claimed use(s) could not be found in the "
                      f"transcript and were not counted.")

    return TransferResult(
        move_as_understood=obs.move_as_understood,
        detectable=obs.detectable_in_transcript,
        not_detectable_reason=obs.not_detectable_reason,
        opportunities=opportunities,
        uses_claimed=len(obs.uses),
        uses_verified=n_verified,
        median_latency_seconds=_latency(
            [u for u in verified_uses if u["verified"]], parse_timestamp),
        status=status,
        rationale=rationale,
        uses=verified_uses,
        missed=[m.model_dump() for m in obs.missed],
    )
