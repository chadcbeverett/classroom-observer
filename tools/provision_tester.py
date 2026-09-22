#!/usr/bin/env python3
"""Provision a pilot tester with their own coach account and a private copy of
the sample teacher data.

Why copies and not sharing: `_scope_filter` in app/main.py scopes a coach to
``teachers.assigned_coach_user_id = <their id>``. A second coach in the same org
therefore sees an empty roster, and the org-wide roles (`principal`, `district`)
are rejected by the users table CHECK constraint, so they are not reachable for
a real magic-link account. Giving each tester their own clone is the only way to
put data in front of them without weakening the scoping rule that keeps real
caseloads separate.

Each tester's clone is independent: edits, ratings and archives by one tester are
invisible to every other tester and to the template coach.

Usage:
    python tools/provision_tester.py --email jane@school.org --name "Jane Doe"
    python tools/provision_tester.py --email jane@school.org --name "Jane Doe" --dry-run
    python tools/provision_tester.py --email jane@school.org --name "Jane Doe" --force
"""
from __future__ import annotations

import argparse
import os
import secrets
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Tables cloned per teacher, in insertion order. Each entry maps the table to
# the columns that must be re-pointed at the new tester / new parent rows.
# Every other column is copied verbatim, so schema additions ride along without
# editing this file — only new *foreign keys* need a line here.
LINK_TABLE = "teachers"
CHILD_TABLES = [
    # (table, column linking it to its parent, which id-map to resolve through)
    ("consent_records", "teacher_id", "teacher"),
    ("observations", "teacher_id", "teacher"),
    ("report_versions", "observation_id", "observation"),
]
# Columns that point at the owning coach, wherever they appear.
COACH_COLUMNS = {"assigned_coach_user_id", "observer_user_id", "granted_by_user_id"}
# Columns that must be blanked rather than copied: they link to rows we are not
# cloning, or to a login that belongs to someone else.
BLANK_COLUMNS = {"user_id", "coaching_cycle_id", "archived_at", "deleted_at"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _columns(db: sqlite3.Connection, table: str) -> list[str]:
    return [r["name"] for r in db.execute(f"PRAGMA table_info({table})")]


def _clone_row(db, table, row, *, overrides, dry_run):
    """Copy one row, applying overrides. Returns the new row id."""
    cols = _columns(db, table)
    values = {}
    for c in cols:
        if c in overrides:
            values[c] = overrides[c]
        elif c in BLANK_COLUMNS:
            values[c] = None
        else:
            values[c] = row[c]
    if not dry_run:
        placeholders = ",".join("?" for _ in values)
        db.execute(
            f"INSERT INTO {table}({','.join(values)}) VALUES({placeholders})",
            tuple(values.values()),
        )
    return values["id"]


def _mint_link(db: sqlite3.Connection, args, user_id: str) -> str:
    """Insert a single-use magic-link token and return the URL to hand over."""
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    db.execute(
        "INSERT INTO magic_link_tokens(id, email, user_id, purpose, created_at, expires_at)"
        " VALUES(?,?,?,?,?,?)",
        (token, args.email, user_id, "signin", now.isoformat(),
         (now + timedelta(minutes=args.link_minutes)).isoformat()),
    )
    db.commit()
    return f"{args.base_url.rstrip('/')}/auth/{token}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True, help="Tester's email (their sign-in identity).")
    ap.add_argument("--name", required=True, help="Display name, e.g. 'Jane Doe'.")
    ap.add_argument("--db", default=os.environ.get("OBSERVER_DB", "reports/observations.sqlite"))
    ap.add_argument("--from-email", default=None,
                    help="Coach whose teachers are the template. Default: the "
                         "org's first-created coach.")
    ap.add_argument("--base-url", default=os.environ.get("OBSERVER_PUBLIC_URL", "http://127.0.0.1:8000"))
    ap.add_argument("--link-minutes", type=int, default=60, help="Magic-link TTL (default 60).")
    ap.add_argument("--force", action="store_true",
                    help="Clone again even if this tester already has teachers.")
    ap.add_argument("--link-only", action="store_true",
                    help="Mint a fresh sign-in link for an existing tester; clone nothing. "
                         "Sign-in links are single-use, so this is the way to hand someone "
                         "a replacement without touching their data.")
    ap.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing.")
    args = ap.parse_args()

    # signin_submit lowercases what the user types and users.email has no
    # case-insensitive collation, so an account stored with any capital letter
    # can never be found at sign-in: the token is minted with user_id NULL and
    # the link is dead on arrival, indistinguishable from an unknown address.
    args.email = args.email.strip().lower()

    db = sqlite3.connect(args.db)
    db.row_factory = sqlite3.Row

    org = db.execute("SELECT id, name FROM organizations ORDER BY created_at ASC LIMIT 1").fetchone()
    if org is None:
        sys.exit("No organization exists. Run tools/onboard_district.py first.")

    if args.link_only:
        row = db.execute(
            "SELECT id, name FROM users WHERE email=? AND org_id=?", (args.email, org["id"])
        ).fetchone()
        if row is None:
            sys.exit(f"No account for {args.email}. Run without --link-only to create one.")
        n = db.execute(
            f"SELECT COUNT(*) FROM {LINK_TABLE} WHERE assigned_coach_user_id=?", (row["id"],)
        ).fetchone()[0]
        print(f"account: {row['name']} <{args.email}>  ({n} teachers)")
        print(f"\nSign-in link (expires in {args.link_minutes} min):\n")
        print(f"  {_mint_link(db, args, row['id'])}\n")
        return

    # Resolve the template coach.
    if args.from_email:
        template = db.execute(
            "SELECT id, email FROM users WHERE email=? AND org_id=?", (args.from_email, org["id"])
        ).fetchone()
        if template is None:
            sys.exit(f"No coach {args.from_email!r} in org {org['name']!r}.")
    else:
        template = db.execute(
            "SELECT id, email FROM users WHERE org_id=? AND role='coach' ORDER BY created_at ASC LIMIT 1",
            (org["id"],),
        ).fetchone()
        if template is None:
            sys.exit("No coach to use as a template. Run tools/onboard_district.py first.")

    template_teachers = db.execute(
        f"SELECT * FROM {LINK_TABLE} WHERE assigned_coach_user_id=? ORDER BY name", (template["id"],)
    ).fetchall()
    if not template_teachers:
        sys.exit(f"Template coach {template['email']} has no teachers to copy.")

    # Find or create the tester. Reuses an existing account so re-running after a
    # typo'd name doesn't strand a second row with the same email.
    tester = db.execute(
        "SELECT id, name FROM users WHERE email=? AND org_id=?", (args.email, org["id"])
    ).fetchone()
    if tester is None:
        tester_id = str(uuid.uuid4())
        action = "create"
    else:
        tester_id = tester["id"]
        action = "reuse"
        existing = db.execute(
            f"SELECT COUNT(*) FROM {LINK_TABLE} WHERE assigned_coach_user_id=?", (tester_id,)
        ).fetchone()[0]
        if existing and not args.force:
            sys.exit(
                f"{args.email} already has {existing} teacher(s). "
                f"Re-running would duplicate them. Pass --force if that is what you want."
            )

    print(f"org:      {org['name']}")
    print(f"template: {template['email']} ({len(template_teachers)} teachers)")
    print(f"tester:   {args.email} ({action} account)")
    print()

    if action == "create" and not args.dry_run:
        db.execute(
            "INSERT INTO users(id, org_id, email, name, role, auth_provider, is_active, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (tester_id, org["id"], args.email, args.name, "coach", "email", 1, _now()),
        )

    teacher_map: dict[str, str] = {}
    observation_map: dict[str, str] = {}
    consent_map: dict[str, str] = {}
    counts: dict[str, int] = {LINK_TABLE: 0}

    for t in template_teachers:
        new_tid = str(uuid.uuid4())
        teacher_map[t["id"]] = new_tid
        _clone_row(db, LINK_TABLE, t, dry_run=args.dry_run, overrides={
            "id": new_tid, "assigned_coach_user_id": tester_id, "created_at": _now(),
        })
        counts[LINK_TABLE] += 1
        print(f"  teacher: {t['name']}")

    for table, link_col, link_kind in CHILD_TABLES:
        id_map = teacher_map if link_kind == "teacher" else observation_map
        counts[table] = 0
        for old_parent, new_parent in list(id_map.items()):
            rows = db.execute(f"SELECT * FROM {table} WHERE {link_col}=?", (old_parent,)).fetchall()
            for row in rows:
                new_id = str(uuid.uuid4())
                overrides = {"id": new_id, link_col: new_parent}
                for c in _columns(db, table):
                    if c in COACH_COLUMNS:
                        overrides[c] = tester_id
                # An observation's consent row was cloned in an earlier pass;
                # re-point at the clone so the upload gate still sees consent.
                if table == "observations" and row["consent_record_id"]:
                    overrides["consent_record_id"] = consent_map.get(
                        row["consent_record_id"], row["consent_record_id"]
                    )
                _clone_row(db, table, row, dry_run=args.dry_run, overrides=overrides)
                counts[table] += 1
                if table == "consent_records":
                    consent_map[row["id"]] = new_id
                if table == "observations":
                    observation_map[row["id"]] = new_id

    link = None if args.dry_run else _mint_link(db, args, tester_id)

    print()
    print("cloned: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    if args.dry_run:
        print("\nDRY RUN — nothing written.")
    else:
        print(f"\nSign-in link (expires in {args.link_minutes} min):\n\n  {link}\n")


if __name__ == "__main__":
    main()
