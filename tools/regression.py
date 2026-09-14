#!/usr/bin/env python3
"""Regression harness for the TNTP observer pipeline.

Every prompt tweak changes model behavior on unseen inputs, but we have a
fixed set of 4 baseline observations that shouldn't drift silently. This
tool freezes the stable fields of each baseline scores.json and compares
future runs against them.

What counts as "stable" (comparable across runs):
    - Domain ratings for all 4 performance areas.
    - Sub-descriptor names + ratings per domain.
    - Core Teacher Skills implicated per domain (as a set).
    - Priority coaching skills (as a set).

What we intentionally IGNORE (varies by run and is not evaluated):
    - Narrative prose wording.
    - Specific timestamps cited as evidence.
    - Bite-sized action wording.
    - overall_summary / opening_paragraph text.

Usage:
    # Freeze current outputs as the "known good" baseline:
    python tools/regression.py freeze

    # Compare current outputs against the frozen baseline:
    python tools/regression.py diff

    # Restrict to specific observations:
    python tools/regression.py diff --observations dulaney lopez
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple


ROOT = Path(__file__).resolve().parent.parent
BASELINE_DIR = Path(__file__).resolve().parent / "baselines"
REPORTS_DIR = ROOT / "reports"

DOMAIN_ORDER = [
    "Student Engagement",
    "Essential Content",
    "Academic Ownership",
    "Demonstration of Learning",
]

# Map observation short name → reports/ subfolder. Add rows here as new
# baseline observations are captured.
OBSERVATIONS = {
    "summey": "summey_test",
    "dulaney": "dulaney_2026-05-11",
    "lopez": "lopez_2026-05-11",
    "livingston": "livingston_2026-05-18",
}


def _extract_stable(scores_json: dict) -> dict:
    """Reduce a full scores.json to the fields that should be stable across runs."""
    report = scores_json["report"]
    das = report.get("domain_assessments") or []
    domain_ratings = {da["domain"]: da["overall_rating"] for da in das}
    sub_descriptors = {
        da["domain"]: sorted(
            [{"descriptor": d["descriptor"], "rating": d["rating"]} for d in da.get("descriptor_scores", [])],
            key=lambda x: x["descriptor"],
        )
        for da in das
    }
    cts_per_domain = {
        da["domain"]: sorted(da.get("relevant_core_teacher_skills", []))
        for da in das
    }
    priority_skills = sorted(
        r["core_teacher_skill"] for r in report.get("coaching_recommendations", [])
    )
    return {
        "domain_ratings": domain_ratings,
        "sub_descriptors": sub_descriptors,
        "core_teacher_skills_per_domain": cts_per_domain,
        "priority_skills": priority_skills,
    }


def _load_current(short_name: str) -> dict:
    folder = OBSERVATIONS[short_name]
    scores_path = REPORTS_DIR / folder / "scores.json"
    if not scores_path.exists():
        raise FileNotFoundError(f"Missing: {scores_path}")
    return _extract_stable(json.loads(scores_path.read_text()))


def _load_baseline(short_name: str) -> dict:
    path = BASELINE_DIR / f"{short_name}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No frozen baseline at {path}. Run: python tools/regression.py freeze"
        )
    return json.loads(path.read_text())


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def freeze(names: List[str]) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for short in names:
        stable = _load_current(short)
        out = BASELINE_DIR / f"{short}.json"
        out.write_text(json.dumps(stable, indent=2))
        print(f"  Froze {short:15s} → {out.relative_to(ROOT)}")
    return 0


def _compare(short: str, current: dict, baseline: dict) -> Tuple[List[str], List[str]]:
    """Return (findings, warnings). Findings are hard regressions; warnings are drift."""
    findings: List[str] = []
    warnings: List[str] = []

    # 1. Domain ratings — hard match required
    for domain in DOMAIN_ORDER:
        cur = current["domain_ratings"].get(domain)
        base = baseline["domain_ratings"].get(domain)
        if cur != base:
            findings.append(
                f"    Domain rating shift: {domain}: {base!r} → {cur!r}"
            )

    # 2. Sub-descriptors — flag any rating changes; new/missing descriptors are warnings.
    for domain in DOMAIN_ORDER:
        cur_subs = {d["descriptor"]: d["rating"] for d in current["sub_descriptors"].get(domain, [])}
        base_subs = {d["descriptor"]: d["rating"] for d in baseline["sub_descriptors"].get(domain, [])}
        all_names = set(cur_subs) | set(base_subs)
        for name in sorted(all_names):
            if name not in base_subs:
                warnings.append(f"    New sub-descriptor: {domain}/{name} = {cur_subs[name]!r}")
            elif name not in cur_subs:
                warnings.append(f"    Removed sub-descriptor: {domain}/{name} (was {base_subs[name]!r})")
            elif cur_subs[name] != base_subs[name]:
                findings.append(
                    f"    Sub-descriptor rating shift: {domain}/{name}: "
                    f"{base_subs[name]!r} → {cur_subs[name]!r}"
                )

    # 3. Core Teacher Skills per domain — Jaccard overlap; warn on drift under 0.5
    for domain in DOMAIN_ORDER:
        cur_cts = current["core_teacher_skills_per_domain"].get(domain, [])
        base_cts = baseline["core_teacher_skills_per_domain"].get(domain, [])
        j = _jaccard(cur_cts, base_cts)
        added = set(cur_cts) - set(base_cts)
        removed = set(base_cts) - set(cur_cts)
        if added or removed:
            msg = f"    CTS drift in {domain} (Jaccard={j:.2f}): +{sorted(added)} -{sorted(removed)}"
            if j < 0.5:
                findings.append(msg)
            else:
                warnings.append(msg)

    # 4. Priority skills — small set, exact-match ideal
    cur_pri = set(current["priority_skills"])
    base_pri = set(baseline["priority_skills"])
    if cur_pri != base_pri:
        added = cur_pri - base_pri
        removed = base_pri - cur_pri
        warnings.append(
            f"    Priority skill drift: +{sorted(added)} -{sorted(removed)}"
        )

    return findings, warnings


def diff(names: List[str]) -> int:
    total_findings = 0
    total_warnings = 0
    for short in names:
        try:
            baseline = _load_baseline(short)
            current = _load_current(short)
        except FileNotFoundError as e:
            print(f"  {short:15s} SKIP: {e}")
            continue
        findings, warnings = _compare(short, current, baseline)
        total_findings += len(findings)
        total_warnings += len(warnings)
        status = "OK" if not findings and not warnings else ("FINDINGS" if findings else "WARNINGS")
        print(f"  {short:15s} [{status}]  {len(findings)} finding(s), {len(warnings)} warning(s)")
        for f in findings:
            print(f)
        for w in warnings:
            print(w)
    print()
    print(f"Total: {total_findings} finding(s), {total_warnings} warning(s)")
    return 1 if total_findings else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_freeze = sub.add_parser("freeze", help="Snapshot current reports as the baseline.")
    p_freeze.add_argument("--observations", nargs="*", default=list(OBSERVATIONS.keys()),
                          choices=OBSERVATIONS.keys())

    p_diff = sub.add_parser("diff", help="Compare current reports against the frozen baseline.")
    p_diff.add_argument("--observations", nargs="*", default=list(OBSERVATIONS.keys()),
                        choices=OBSERVATIONS.keys())

    args = p.parse_args()
    if args.cmd == "freeze":
        sys.exit(freeze(args.observations))
    elif args.cmd == "diff":
        sys.exit(diff(args.observations))


if __name__ == "__main__":
    main()
