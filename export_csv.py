#!/usr/bin/env python3
"""Export a CSV roll-up of observations under reports/, for a chosen rubric.

Usage:
    python export_csv.py                              # TNTP rubric, all reports
    python export_csv.py --rubric tntp_core_4pt_2014  # explicit
    python export_csv.py --out my_export.csv

The script walks reports/<name>/scores.json, filters to reports scored against
the requested rubric, and emits one row per observation. Column names are
derived from the rubric's domain list, so different rubrics produce differently
shaped CSVs — you can't meaningfully compare a TNTP report against a Danielson
report in the same spreadsheet, so we don't try.

Long text fields (narrative, evidence) are flattened with newlines inside cells;
Excel and Google Sheets render them correctly when "Wrap Text" is on.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pipeline.rubric import DEFAULT_RUBRIC_ID, RUBRICS, Rubric, get_rubric


def _domain_short_name(domain: str) -> str:
    """Turn 'Student Engagement' into 'student_engagement' for column prefixing."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", domain).strip("_").lower()
    return slug


def _teacher_name_from_dir(dir_name: str) -> str:
    base = dir_name.split("_")[0]
    return base.replace("-", " ").title()


def _format_duration(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def _flatten_priority(rec: Optional[dict]) -> Tuple[str, str, str, str]:
    if not rec:
        return ("", "", "", "")
    indicators = "\n".join(f"- {i}" for i in rec.get("success_indicators", []))
    return (
        rec.get("core_teacher_skill", ""),
        rec.get("related_domain", ""),
        rec.get("bite_sized_action", ""),
        indicators,
    )


def build_row(scores_json: dict, rubric: Rubric) -> dict:
    meta = scores_json.get("metadata", {})
    report = scores_json["report"]

    # Support the current merged schema AND the older split schema for backwards read.
    assessments_by_name = {d["domain"]: d for d in report.get("domain_assessments", [])}
    legacy_scores_by_name = {d["domain"]: d for d in report.get("domain_scores", [])}
    legacy_narratives_by_name = {dn["domain"]: dn for dn in report.get("domain_narratives", [])}

    row = {
        "teacher": "",  # filled in by caller
        "video_filename": meta.get("video_filename", ""),
        "duration": _format_duration(meta.get("duration_seconds", 0)),
        "scored_at": meta.get("scored_at", ""),
        "transcription_model": meta.get("transcription_model", ""),
        "rubric_id": meta.get("rubric_id", ""),
    }

    score_total = 0
    for domain in rubric.domains:
        short = _domain_short_name(domain)
        da = assessments_by_name.get(domain)
        if da:
            row[f"{short}_rating"] = da.get("overall_rating", "")
            row[f"{short}_score"] = da.get("overall_score", "")
            row[f"{short}_preponderance"] = da.get("preponderance_summary", "")
            row[f"{short}_rubric_descriptor"] = da.get("rubric_descriptor_text", "")
            row[f"{short}_observed"] = da.get("what_was_observed", "")
            row[f"{short}_distance_from_target"] = da.get("distance_from_target", "")
            row[f"{short}_core_teacher_skills"] = "\n".join(
                f"- {s}" for s in da.get("relevant_core_teacher_skills", [])
            )
            if da.get("overall_score"):
                score_total += da["overall_score"]
        else:
            # Legacy split-schema fallback
            d = legacy_scores_by_name.get(domain, {})
            dn = legacy_narratives_by_name.get(domain, {})
            row[f"{short}_rating"] = d.get("overall_rating", "")
            row[f"{short}_score"] = d.get("overall_score", "")
            row[f"{short}_preponderance"] = d.get("summary", "")
            row[f"{short}_rubric_descriptor"] = dn.get("rubric_descriptor_text", "")
            row[f"{short}_observed"] = dn.get("what_was_observed", "")
            row[f"{short}_distance_from_target"] = dn.get("distance_from_target", "")
            row[f"{short}_core_teacher_skills"] = "\n".join(
                f"- {s}" for s in dn.get("relevant_core_teacher_skills", [])
            )
            if d.get("overall_score"):
                score_total += d["overall_score"]

    row["total_score"] = score_total
    max_total = len(rubric.domains) * rubric.num_levels
    row["max_total_score"] = max_total
    row["average_score"] = round(score_total / len(rubric.domains), 2) if score_total else ""

    row["opening_paragraph"] = report.get("opening_paragraph", "")
    row["overall_summary"] = report.get("overall_summary", "")

    recs = report.get("coaching_recommendations", [])
    p1 = _flatten_priority(recs[0] if len(recs) > 0 else None)
    p2 = _flatten_priority(recs[1] if len(recs) > 1 else None)
    row["priority_1_skill"], row["priority_1_domain"], row["priority_1_action"], row["priority_1_success_indicators"] = p1
    row["priority_2_skill"], row["priority_2_domain"], row["priority_2_action"], row["priority_2_success_indicators"] = p2

    return row


def build_column_order(rubric: Rubric) -> List[str]:
    """Column order is derived from the rubric so each column is meaningful and stable."""
    cols = [
        "teacher", "video_filename", "duration", "scored_at",
        "transcription_model", "rubric_id",
    ]
    # Scores first for at-a-glance reading.
    for domain in rubric.domains:
        short = _domain_short_name(domain)
        cols += [f"{short}_rating", f"{short}_score"]
    cols += ["total_score", "max_total_score", "average_score"]
    # Rubric-tight narrative content per domain.
    for domain in rubric.domains:
        short = _domain_short_name(domain)
        cols += [
            f"{short}_rubric_descriptor",
            f"{short}_observed",
            f"{short}_distance_from_target",
            f"{short}_core_teacher_skills",
            f"{short}_preponderance",
        ]
    cols += [
        "opening_paragraph", "overall_summary",
        "priority_1_skill", "priority_1_domain", "priority_1_action", "priority_1_success_indicators",
        "priority_2_skill", "priority_2_domain", "priority_2_action", "priority_2_success_indicators",
    ]
    return cols


def _matches_rubric(scores_json: dict, rubric: Rubric) -> bool:
    """True if this report was scored against the requested rubric.

    Older reports don't carry rubric_id in metadata. In that case, sniff the
    domain names — if they match the rubric's domains, we treat it as a match
    (necessary for older baselines that predate the rubric_id field).
    """
    meta = scores_json.get("metadata", {})
    if meta.get("rubric_id") == rubric.id:
        return True
    if meta.get("rubric_id"):
        return False  # explicitly a different rubric
    report = scores_json.get("report", {})
    das = report.get("domain_assessments") or report.get("domain_scores") or []
    domains_in_report = {d["domain"] for d in das}
    return domains_in_report == set(rubric.domains)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rubric", default=DEFAULT_RUBRIC_ID, choices=sorted(RUBRICS.keys()),
                        help="Rubric to build the CSV against. Reports scored with a different rubric are skipped.")
    parser.add_argument("--reports-dir", type=Path, default=Path("reports"),
                        help="Directory containing per-observation subfolders.")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output CSV path. Default: <reports-dir>/scores_rollup.csv")
    parser.add_argument("--teacher-overrides", type=str, default=None,
                        help="Override auto-derived teacher names: 'folder1=Name 1,folder2=Name 2'")
    args = parser.parse_args()

    rubric = get_rubric(args.rubric)
    out_path = args.out or (args.reports_dir / "scores_rollup.csv")
    overrides = {}
    if args.teacher_overrides:
        for pair in args.teacher_overrides.split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                overrides[k.strip()] = v.strip()

    rows = []
    skipped_other_rubric = 0
    for sub in sorted(args.reports_dir.iterdir()):
        scores_path = sub / "scores.json"
        if not scores_path.exists():
            continue
        try:
            data = json.loads(scores_path.read_text())
        except json.JSONDecodeError as e:
            print(f"  Skipping {sub.name}: invalid JSON ({e})", file=sys.stderr)
            continue
        if not _matches_rubric(data, rubric):
            skipped_other_rubric += 1
            continue
        row = build_row(data, rubric)
        row["teacher"] = overrides.get(sub.name, _teacher_name_from_dir(sub.name))
        rows.append((sub.name, row))

    if not rows:
        print(f"No scores.json files matching rubric {rubric.id!r} found under {args.reports_dir}/",
              file=sys.stderr)
        if skipped_other_rubric:
            print(f"(Skipped {skipped_other_rubric} report(s) scored against a different rubric.)")
        return

    columns = build_column_order(rubric)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for _, row in rows:
            writer.writerow(row)

    print(f"Wrote {len(rows)} row(s) to: {out_path}")
    print(f"Rubric: {rubric.name}\n")
    if skipped_other_rubric:
        print(f"(Skipped {skipped_other_rubric} report(s) scored against a different rubric.)\n")
    print("Teachers included:")
    for folder, row in rows:
        print(f"  - {row['teacher']:20s} (from {folder}/)  "
              f"total={row['total_score']}/{row['max_total_score']}, "
              f"avg={row['average_score']}")


if __name__ == "__main__":
    main()
