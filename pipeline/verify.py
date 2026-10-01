"""Check evidence against the source media before it counts toward a score.

A 2026 study comparing SHAP attributions with LLM-written rationales concluded
that model explanations are unreliable as faithful justifications for
rubric-based scores. The practical consequence: a model's account of why it
rated something cannot itself be the check. Verification has to happen outside
the model, against the transcript and the frame index.

So every evidence item is checked here. A quoted line must actually appear in
the transcript near the timestamp it cites. A frame citation must point at a
frame that was really sampled in that window. A sub-descriptor that cannot
reach the rubric's evidence threshold on verified items is left unrated rather
than scored, and `aggregate_domain` then treats it as a gap.

One honesty constraint runs through this module. Verifying a transcript quote
is a real check — the words are either there or they are not. Verifying a frame
citation is not: it confirms only that a frame exists at that moment, never that
the frame shows what was claimed. Those two results are labelled differently and
counted separately, because collapsing them would overstate what the system
actually knows.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Sequence

# A cited timestamp rarely lands on a segment boundary, and a model summarising
# a stretch of lesson may cite its midpoint. Wide enough to tolerate that,
# narrow enough that "near 04:30" still means something in a 20-minute lesson.
DEFAULT_TOLERANCE_S = 45.0

# Fraction of a quoted fragment that must be found in the window. Below 1.0
# because models normalise punctuation, fix disfluencies, and trim filler from
# otherwise faithful quotes.
DEFAULT_MIN_COVERAGE = 0.82

_QUOTE_PAIRS = [("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")]
_ELISION = re.compile(r"\s*(?:\.\.\.|…)\s*")


def parse_timestamp(ts: str) -> Optional[float]:
    """'04:32' or '1:04:32' to seconds. None when unparseable."""
    if not ts:
        return None
    parts = str(ts).strip().strip("[]").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if not 1 <= len(nums) <= 3:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def normalize(text: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace.

    Matching happens on this form so a quote differing only in apostrophe style
    or a trailing comma still counts as found.
    """
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.casefold()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def extract_quotes(text: str) -> List[str]:
    """Pull quoted spans out of an evidence string.

    Evidence is typically `'quoted speech' - what it shows`. Only the quoted
    part is checkable against the transcript; the gloss after it is the model's
    own words and verifying them against the transcript would fail every time.
    """
    out: List[str] = []
    for open_q, close_q in _QUOTE_PAIRS:
        # For a pair that uses the same character both ends, the body must be
        # allowed to contain that character: "we're gonna flip it in" sits
        # inside a quote delimited by the same apostrophe. Excluding it broke
        # every quote containing a contraction. The word-boundary guards below
        # are what distinguish a delimiter from an apostrophe.
        body = (r"(.{3,}?)" if open_q == close_q
                else r"([^" + re.escape(close_q) + r"]{3,}?)")
        # An apostrophe inside a word is not a closing quote. Without this,
        # "what's being modified, Henry answers 'The misplaced modifier'" pairs
        # the apostrophe in what's with the real opener and checks the wrong
        # span against the transcript — a mis-extraction that silently changes
        # which claim is being verified.
        open_guard = r"(?<![\w])"
        close_guard = r"(?![\w])"
        pattern = open_guard + re.escape(open_q) + body + re.escape(close_q) + close_guard
        out.extend(m.group(1) for m in re.finditer(pattern, text or ""))
    # Longest first: a nested or repeated quote should not shadow the full span.
    return sorted({q.strip() for q in out if q.strip()}, key=len, reverse=True)


def window_text(segments: Sequence, at_s: float, tolerance_s: float) -> str:
    """Transcript text overlapping [at - tolerance, at + tolerance]."""
    lo, hi = at_s - tolerance_s, at_s + tolerance_s
    return " ".join(
        s.text for s in segments
        if getattr(s, "end", 0) >= lo and getattr(s, "start", 0) <= hi
    )


def coverage(fragment: str, haystack: str) -> float:
    """How much of `fragment` appears contiguously in `haystack`, 0..1.

    Longest common block rather than an overall similarity ratio: a short quote
    inside a long window would score near zero on whole-string similarity even
    when present verbatim.
    """
    f, h = normalize(fragment), normalize(haystack)
    if not f:
        return 0.0
    if f in h:
        return 1.0
    if not h:
        return 0.0
    match = SequenceMatcher(None, f, h, autojunk=False).find_longest_match(0, len(f), 0, len(h))
    return match.size / len(f)


def _fmt_ts(seconds: float) -> str:
    m, sec = divmod(int(seconds), 60)
    return f"{m:02d}:{sec:02d}"


def _find_elsewhere(fragment, segments, exclude_at, tolerance_s, min_coverage):
    """Seconds where `fragment` does match, outside the window already tried.

    Returns None when it appears nowhere. Used only to sharpen the message on a
    failure; it never turns a failure into a pass, because a quote attached to
    the wrong moment is still evidence the coach cannot act on as cited.
    """
    if not fragment:
        return None
    for seg in segments:
        start = getattr(seg, "start", None)
        if start is None or abs(start - exclude_at) <= tolerance_s:
            continue
        if coverage(fragment, getattr(seg, "text", "")) >= min_coverage:
            return start
    return None


@dataclass(frozen=True)
class EvidenceVerdict:
    status: str           # "verified" | "unverified" | "unverifiable"
    reason: str
    coverage: float = 0.0

    @property
    def counts_toward_score(self) -> bool:
        """Only a real check counts.

        "unverifiable" is the frame case: a frame was sampled there, but nothing
        here confirms it shows what was claimed. Treating that as verified would
        let a domain reach its evidence threshold on citations that were never
        actually checked.
        """
        return self.status == "verified"


def verify_evidence(
    item,
    *,
    segments: Sequence = (),
    frames: Sequence = (),
    duration_s: Optional[float] = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> EvidenceVerdict:
    """Check one evidence item against the source media.

    `item` is an EvidenceItem or any object/dict with timestamp, source and
    quote_or_description.
    """
    get = (lambda k: item.get(k)) if isinstance(item, dict) else (lambda k: getattr(item, k, None))
    ts_raw = get("timestamp")
    source = (get("source") or "").strip().lower()
    text = get("quote_or_description") or ""

    at = parse_timestamp(ts_raw)
    if at is None:
        return EvidenceVerdict("unverified", f"Timestamp {ts_raw!r} could not be read.")

    if duration_s and at > duration_s + tolerance_s:
        return EvidenceVerdict(
            "unverified",
            f"Cites {ts_raw} in a recording that runs {int(duration_s // 60):02d}:"
            f"{int(duration_s % 60):02d}.",
        )

    quotes = extract_quotes(text)

    if source in ("transcript", "both") or quotes:
        if not quotes:
            # Paraphrase, not fabrication. Nothing was checked and found wanting;
            # there was nothing checkable. It still cannot count toward a score —
            # same as a frame citation — but calling it "unverified" would imply
            # the transcript was searched and contradicted it.
            return EvidenceVerdict(
                "unverifiable",
                "Describes the lesson rather than quoting it, so the transcript "
                "cannot confirm or contradict it.",
            )
        hay = window_text(segments, at, tolerance_s)
        if not hay:
            return EvidenceVerdict(
                "unverified", f"No transcript within {int(tolerance_s)}s of {ts_raw}."
            )
        # Every fragment of an elided quote must be present; a quote joined by
        # "..." claims each piece was said.
        worst, worst_frag = 1.0, ""
        for quote in quotes[:1]:  # the longest span is the claim being made
            for frag in _ELISION.split(quote):
                if len(normalize(frag)) < 4:
                    continue
                c = coverage(frag, hay)
                if c < worst:
                    worst, worst_frag = c, frag
        if worst >= min_coverage:
            return EvidenceVerdict("verified", f"Quote found in transcript near {ts_raw}.", worst)
        # A quote that is real but mis-timed is a different problem from one that
        # was never said, and a coach checking the claim needs to know which.
        elsewhere = _find_elsewhere(worst_frag, segments, at, tolerance_s, min_coverage)
        if elsewhere is not None:
            return EvidenceVerdict(
                "unverified",
                f"Quote appears at {_fmt_ts(elsewhere)}, not the cited {ts_raw}.",
                worst,
            )
        return EvidenceVerdict(
            "unverified",
            f"Quoted text not found in the transcript near {ts_raw} "
            f"(best match {worst:.0%} on “{worst_frag.strip()[:48]}”).",
            worst,
        )

    if source == "frame":
        near = [f for f in frames
                if abs(getattr(f, "timestamp_seconds", -1e9) - at) <= tolerance_s]
        if not near:
            return EvidenceVerdict(
                "unverified", f"No frame was sampled within {int(tolerance_s)}s of {ts_raw}."
            )
        return EvidenceVerdict(
            "unverifiable",
            f"A frame exists at {ts_raw}, but its contents are not checked here.",
        )

    return EvidenceVerdict("unverified", f"Unknown evidence source {source!r}.")


@dataclass
class DescriptorVerification:
    descriptor: str
    claimed_rating: Optional[str]
    verdicts: List[EvidenceVerdict] = field(default_factory=list)
    rating: Optional[str] = None     # survives verification, or None
    reason: str = ""

    @property
    def verified_count(self) -> int:
        return sum(1 for v in self.verdicts if v.counts_toward_score)


def verify_domain_assessments(
    domain_assessments: Sequence,
    *,
    spec,
    segments: Sequence = (),
    frames: Sequence = (),
    duration_s: Optional[float] = None,
    tolerance_s: float = DEFAULT_TOLERANCE_S,
) -> Dict[str, List[DescriptorVerification]]:
    """Verify every evidence item, and decide which sub-descriptor ratings survive.

    Returns domain name -> per-sub-descriptor results. A rating is kept only when
    enough of its evidence verified; otherwise the rating is dropped, and the
    domain outcome follows from `aggregate_domain` on what remains.
    """
    need = spec.aggregation.min_evidence_per_descriptor
    out: Dict[str, List[DescriptorVerification]] = {}

    for da in domain_assessments:
        g = (lambda o, k: o.get(k)) if isinstance(da, dict) else (lambda o, k: getattr(o, k, None))
        domain = g(da, "domain")
        results: List[DescriptorVerification] = []

        for ds in g(da, "descriptor_scores") or []:
            dg = (lambda o, k: o.get(k)) if isinstance(ds, dict) else (lambda o, k: getattr(o, k, None))
            dv = DescriptorVerification(
                descriptor=dg(ds, "descriptor") or "(unnamed)",
                claimed_rating=dg(ds, "rating"),
            )
            for ev in dg(ds, "evidence") or []:
                dv.verdicts.append(verify_evidence(
                    ev, segments=segments, frames=frames,
                    duration_s=duration_s, tolerance_s=tolerance_s,
                ))

            if dv.verified_count >= need:
                dv.rating = dv.claimed_rating
                dv.reason = f"{dv.verified_count} of {len(dv.verdicts)} evidence items verified."
            else:
                dv.rating = None
                dv.reason = (
                    f"Only {dv.verified_count} of {len(dv.verdicts)} evidence items "
                    f"verified; {need} required. Left unrated."
                )
            results.append(dv)

        out[domain] = results

    return out


def verification_summary(by_domain: Dict[str, List[DescriptorVerification]]) -> dict:
    """Counts for logging and for showing a coach what was and was not checked."""
    total = verified = unverifiable = dropped = 0
    for results in by_domain.values():
        for dv in results:
            total += len(dv.verdicts)
            verified += dv.verified_count
            unverifiable += sum(1 for v in dv.verdicts if v.status == "unverifiable")
            if dv.claimed_rating and not dv.rating:
                dropped += 1
    return {
        "evidence_total": total,
        "evidence_verified": verified,
        "evidence_unverifiable": unverifiable,
        "evidence_failed": total - verified - unverifiable,
        "ratings_dropped": dropped,
    }
