#!/usr/bin/env python3
"""Import all reports/*/scores.json into a SQLite database and print a summary.

This is the acceptance test for the data-model design — if the schema in
``pipeline/schema.sql`` can round-trip real observation data cleanly, the
design is validated. If import fails or a query surfaces gaps, we know what
to fix before touching Postgres.

Usage:
    python tools/import_baselines.py                    # fresh DB at reports/observations.sqlite
    python tools/import_baselines.py --db /tmp/obs.db --reset
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.db import (
    connect, init_db, import_scores_json,
    list_observations, get_latest_report,
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "reports" / "observations.sqlite",
                    help="Path to SQLite DB file.")
    ap.add_argument("--reports-dir", type=Path, default=ROOT / "reports",
                    help="Directory of report subfolders.")
    ap.add_argument("--reset", action="store_true",
                    help="Delete the DB file before importing (fresh start).")
    # Coach identity for the imports. Defaults preserve the historical
    # "coach@example.com / Import Coach" author-of-record so re-imports on
    # an old DB don't create a second phantom coach; a pilot deploy should
    # override both so imported baselines land on a real coach's caseload.
    ap.add_argument("--observer-email", default="coach@example.com",
                    help="Email of the coach to attribute imports to (default: coach@example.com).")
    ap.add_argument("--observer-name", default="Import Coach",
                    help="Display name for that coach (default: 'Import Coach').")
    args = ap.parse_args()

    if args.reset and args.db.exists():
        args.db.unlink()
        print(f"Removed {args.db}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    init_db(conn)
    print(f"Initialized DB: {args.db}")
    print(f"Attributing imports to: {args.observer_name} <{args.observer_email}>")

    imported: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for sub in sorted(args.reports_dir.iterdir()):
        if not sub.is_dir():
            continue
        if not (sub / "scores.json").exists():
            continue
        try:
            obs_id = import_scores_json(
                conn, sub,
                observer_email=args.observer_email,
                observer_name=args.observer_name,
            )
            imported.append((sub.name, obs_id))
        except Exception as e:
            # Roll back any partial writes from this folder so a later
            # helper's commit() doesn't flush a half-done UPDATE onto disk.
            # (Before this rollback, a failed folder's uncommitted UPDATE
            # on `observations` was silently persisted by the next folder's
            # get_or_create_teacher commit — leaving observation metadata
            # updated but with no matching report_versions row.)
            try:
                conn.rollback()
            except Exception:
                pass
            failed.append((sub.name, f"{type(e).__name__}: {e}"))

    print(f"\nImported {len(imported)} observation(s):")
    for folder, obs_id in imported:
        print(f"  - {folder:30s} → {obs_id}")
    if failed:
        print(f"\nFAILED {len(failed)}:")
        for folder, err in failed:
            print(f"  - {folder}: {err}")

    # Round-trip verification: list observations and pull the latest report for one.
    print("\n=== Observations (round-tripped from DB) ===")
    for row in list_observations(conn):
        print(f"  {row['teacher']:15s} {row['video_filename']:30s} "
              f"{int(row['video_duration_s'] // 60):02d}:{int(row['video_duration_s'] % 60):02d}  "
              f"status={row['status']}  v{row['latest_version']}")

    if imported:
        obs_id = imported[0][1]
        latest = get_latest_report(conn, obs_id)
        if latest:
            import json as _json
            das = _json.loads(latest["domain_assessments"])
            print(f"\n=== Latest report for {imported[0][0]} (verify domain_assessments round-trip) ===")
            for da in das:
                print(f"  {da['domain']:30s} {da['overall_rating']:20s} "
                      f"({len(da['descriptor_scores'])} sub-descriptors)")

    conn.close()
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
