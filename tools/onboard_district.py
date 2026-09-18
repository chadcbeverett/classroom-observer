#!/usr/bin/env python3
"""One-shot ceremony: onboard a district and its first coach.

Run this once per pilot deployment before pointing a coach at the app.
It creates the organization + the first coach user, optionally seeds
the district-context header, and mints a magic-link URL you can hand
to the coach so their first sign-in doesn't need SMTP.

Idempotent shapes:
  - Fresh DB: creates the org + coach.
  - DB that's already been through `_bootstrap` (started uvicorn once):
    finds the auto-seeded "Local User" / "you@localhost" and promotes
    them in place — no data lost, no duplicates.
  - Already-onboarded DB: refuses with a clear message; use
    ``--rename`` to update identity without minting a new coach.

The ceremony deliberately does NOT touch teachers, observations, or
any coaching data. Existing rows keep their `assigned_coach_user_id`
pointer, which continues to resolve because we promote the user_id
in place rather than creating a fresh row.

Typical usage:

    python tools/onboard_district.py \\
        --org-name "Testville Unified" \\
        --org-slug testville \\
        --coach-email jane@testville.k12.us \\
        --coach-name "Jane Rivera"

To also seed the current-year district-context header:

    ... --seed-district-context

Optional --send-signin-email queues an email through the outbound_mail
queue (delivered by the SMTP sender if configured; otherwise the
printed link is your handoff).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.db import (
    connect, init_db, _now_iso,
    create_magic_link_token, queue_email,
    upsert_district_context, get_district_context,
    MAGIC_LINK_TTL_MINUTES,
)


# Marker values used by the ceremony to detect an auto-seeded DB.
# See app.main._bootstrap where these strings originate.
AUTO_SEED_ORG_SLUG = "local"
AUTO_SEED_COACH_EMAIL = "you@localhost"
# The import-baseline seed also creates a coach with this email — offered
# as a promotion target when the pilot lead is starting from a DB that
# was seeded with the sample teachers.
IMPORT_SEED_COACH_EMAIL = "coach@example.com"


def _print_hr():
    print("-" * 60)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", type=Path,
                    default=ROOT / "reports" / "observations.sqlite",
                    help="Path to SQLite DB file (default: reports/observations.sqlite)")
    ap.add_argument("--org-name", required=True,
                    help='Display name of the district / school. E.g. "Testville Unified".')
    ap.add_argument("--org-slug", required=True,
                    help="URL-safe slug for the org. Lowercase, hyphens only.")
    ap.add_argument("--coach-email", required=True,
                    help="Email of the first coach. They'll use this at /signin.")
    ap.add_argument("--coach-name", required=True,
                    help="Display name of the first coach.")
    ap.add_argument("--rename", action="store_true",
                    help="Update the identity of an already-onboarded org/coach "
                         "in place (no new rows). Useful if a typo landed in "
                         "the first run.")
    ap.add_argument("--seed-district-context", action="store_true",
                    help="Also insert a district_context row for the current "
                         "academic year (empty priorities/initiatives, coach "
                         "can fill in via /admin/district-context).")
    ap.add_argument("--academic-year", default=None,
                    help="For --seed-district-context. Defaults to computed "
                         "year based on today's month (Aug-Jul convention).")
    ap.add_argument("--send-signin-email", action="store_true",
                    help="Queue a sign-in email to the coach through the "
                         "outbound_mail queue. Requires the SMTP sender for "
                         "actual delivery; without it, the printed magic link "
                         "is your handoff.")
    ap.add_argument("--base-url", default=None,
                    help="Origin used in the magic-link URL "
                         "(e.g. https://coach.testville.k12.us). Defaults to "
                         "OBSERVER_PUBLIC_URL env or http://localhost:8000.")
    args = ap.parse_args()

    coach_email = args.coach_email.strip().lower()
    org_slug = args.org_slug.strip().lower()
    if "@" not in coach_email:
        raise SystemExit(f"--coach-email doesn't look like an email: {coach_email!r}")
    if not org_slug or " " in org_slug or not org_slug.replace("-", "").isalnum():
        raise SystemExit(f"--org-slug must be lowercase hyphen-safe: {org_slug!r}")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(args.db)
    init_db(conn)

    print(f"Onboarding {args.org_name!r} into {args.db}")
    _print_hr()

    # ---- Decide the org row shape --------------------------------------
    # Preference order:
    #   1. An org already matches --org-slug → reuse.
    #   2. An auto-seeded "local" org exists → promote it in place.
    #   3. Otherwise create a fresh org.
    existing_by_slug = conn.execute(
        "SELECT id, name, slug FROM organizations WHERE slug = ?", (org_slug,)
    ).fetchone()
    auto_seed_org = conn.execute(
        "SELECT id, name, slug FROM organizations WHERE slug = ?", (AUTO_SEED_ORG_SLUG,)
    ).fetchone()

    if existing_by_slug and not args.rename:
        # Same slug → this looks like a re-run. Refuse unless --rename so
        # a typo doesn't create phantom orgs on repeat runs.
        raise SystemExit(
            f"An org with slug {org_slug!r} already exists (name: {existing_by_slug['name']!r}).\n"
            f"Re-run with --rename to update its name in place, or pick a different --org-slug."
        )
    if existing_by_slug and args.rename:
        org_id = existing_by_slug["id"]
        conn.execute(
            "UPDATE organizations SET name = ? WHERE id = ?",
            (args.org_name, org_id),
        )
        print(f"✓ Renamed org {org_slug!r}: {existing_by_slug['name']!r} → {args.org_name!r}")
    elif auto_seed_org:
        # Fresh pilot with a boot-seeded "Local User" org — promote in place.
        # Every teacher, observation, cycle, etc. already points at this
        # org_id; changing name+slug leaves those FKs intact.
        org_id = auto_seed_org["id"]
        conn.execute(
            "UPDATE organizations SET name = ?, slug = ? WHERE id = ?",
            (args.org_name, org_slug, org_id),
        )
        print(f"✓ Promoted auto-seeded org {auto_seed_org['name']!r} → {args.org_name!r} (slug: {org_slug!r})")
    else:
        # Fresh insert.
        import uuid
        org_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO organizations (id, name, slug, plan_tier, created_at)
               VALUES (?, ?, ?, 'pilot', ?)""",
            (org_id, args.org_name, org_slug, _now_iso()),
        )
        print(f"✓ Created org {args.org_name!r} (slug: {org_slug!r})")

    # ---- Decide the coach row shape ------------------------------------
    # Preference order:
    #   1. A user with the target email in this org → reuse (rename if
    #      --rename, else refuse).
    #   2. Exactly one auto-seeded coach in this org (you@localhost or
    #      coach@example.com) with no other coaches → promote in place.
    #   3. Otherwise insert a fresh coach.
    existing_coach = conn.execute(
        "SELECT id, email, name, role FROM users WHERE org_id = ? AND lower(email) = lower(?)",
        (org_id, coach_email),
    ).fetchone()
    all_coaches = conn.execute(
        "SELECT id, email, name FROM users WHERE org_id = ? AND role = 'coach' ORDER BY created_at ASC",
        (org_id,),
    ).fetchall()
    seed_candidates = [
        c for c in all_coaches if c["email"] in (AUTO_SEED_COACH_EMAIL, IMPORT_SEED_COACH_EMAIL)
    ]

    if existing_coach and existing_coach["role"] != "coach":
        raise SystemExit(
            f"A user with email {coach_email!r} exists in this org but has role "
            f"{existing_coach['role']!r}. Onboard a different coach or fix the role first."
        )
    if existing_coach and not args.rename:
        raise SystemExit(
            f"A coach with email {coach_email!r} is already in this org.\n"
            f"Re-run with --rename to update their display name in place, or use a different --coach-email."
        )
    if existing_coach and args.rename:
        conn.execute(
            "UPDATE users SET name = ? WHERE id = ?",
            (args.coach_name, existing_coach["id"]),
        )
        coach_id = existing_coach["id"]
        print(f"✓ Renamed coach {coach_email!r}: {existing_coach['name']!r} → {args.coach_name!r}")
    elif len(all_coaches) == 1 and all_coaches[0] in seed_candidates:
        # Exactly one auto-seeded coach in the org — promote in place.
        # Preserves author-of-record on any private notes, cycles, etc.
        # they've been (auto-)attributed to.
        prior = all_coaches[0]
        conn.execute(
            "UPDATE users SET email = ?, name = ? WHERE id = ?",
            (coach_email, args.coach_name, prior["id"]),
        )
        coach_id = prior["id"]
        print(f"✓ Promoted auto-seeded coach {prior['email']!r} → {coach_email!r} ({args.coach_name!r})")
    else:
        # Fresh insert.
        import uuid
        coach_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO users (id, org_id, email, name, role, created_at)
               VALUES (?, ?, ?, ?, 'coach', ?)""",
            (coach_id, org_id, coach_email, args.coach_name, _now_iso()),
        )
        print(f"✓ Created coach {args.coach_name!r} <{coach_email}>")
        if seed_candidates:
            print(f"  Note: {len(seed_candidates)} auto-seeded coach account(s) remain in this org")
            print(f"        ({', '.join(c['email'] for c in seed_candidates)}).")
            print(f"        These are safe to leave, or re-run this script with the seed email")
            print(f"        as --coach-email + --rename to fold their history into the new coach.")

    conn.commit()

    # ---- Optional: district context header -----------------------------
    if args.seed_district_context:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        year = args.academic_year
        if not year:
            # Aug-Jul convention: months 1-7 = last year's start; 8-12 = this year's.
            year = f"{now.year - 1}-{now.year}" if now.month < 8 else f"{now.year}-{now.year + 1}"
        existing_dc = get_district_context(conn, org_id=org_id, academic_year=year)
        if existing_dc:
            print(f"✓ District-context header already exists for {year} — left alone.")
        else:
            upsert_district_context(
                conn, org_id=org_id, academic_year=year,
                year_arc=None,
                district_priorities=None,
                district_initiatives=None,
            )
            print(f"✓ Seeded empty district-context for {year} (coach fills in via /admin/district-context).")

    # ---- Mint the magic-link URL ---------------------------------------
    base_url = (
        args.base_url
        or os.environ.get("OBSERVER_PUBLIC_URL", "").strip()
        or "http://localhost:8000"
    ).rstrip("/")
    token, resolved_uid = create_magic_link_token(
        conn, email=coach_email, next_url="/", ip=None,
    )
    if resolved_uid != coach_id:
        # Sanity — the email we just created should resolve.
        print(f"  WARNING: minted token resolved to user {resolved_uid[:8] if resolved_uid else 'None'} "
              f"instead of just-created coach {coach_id[:8]}. Something is off.")
    signin_url = f"{base_url}/auth/{token}"

    if args.send_signin_email:
        subject = f"Sign in to Classroom Observer — {args.org_name}"
        body = (
            f"Hi {args.coach_name.split()[0] if args.coach_name else 'there'},\n\n"
            f"You're set up as the first coach on Classroom Observer for {args.org_name}.\n"
            f"Click to sign in — this link works for {MAGIC_LINK_TTL_MINUTES} minutes:\n\n"
            f"{signin_url}\n\n"
            f"If it expires before you click, visit {base_url}/signin and enter "
            f"your email to get another.\n"
        )
        queue_email(
            conn, to_email=coach_email, subject=subject,
            body_text=body, related_token_id=token,
        )
        conn.commit()
        print(f"✓ Queued sign-in email to {coach_email} (SMTP sender delivers if configured).")

    _print_hr()
    print("Done. First sign-in:")
    print()
    print(f"  {signin_url}")
    print()
    print(f"Link expires in {MAGIC_LINK_TTL_MINUTES} minutes. If it lapses, the coach can")
    print(f"go to {base_url}/signin and ask for a fresh one — as long as SMTP is")
    print(f"configured (SMTP_HOST env), or you re-run this script with a new URL.")
    conn.close()


if __name__ == "__main__":
    main()
