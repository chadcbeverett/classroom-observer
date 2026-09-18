# Classroom Observer — Pilot Deployment Runbook

**Audience.** The person standing up Classroom Observer for a district's first
pilot. Assumes basic comfort with a terminal, Python, and setting environment
variables. Does **not** assume familiarity with this codebase.

**Time to first coach signed in.** ~30 minutes on a fresh host, plus whatever
your DNS / TLS provisioning takes.

**What you'll have when this runbook is done:**
- The app running on a host you control
- One district (org) provisioned in the database
- One coach with a real email address, signed in and looking at their roster
- SMTP delivery working, so the coach can request future sign-in links themselves
- District context set for the current academic year

---

## 1. Before you start

### What you need on the host

- **Python 3.9 or newer.** Check with `python3 --version`.
- **ffmpeg + ffprobe** on PATH. macOS: `brew install ffmpeg`. Debian/Ubuntu: `apt install ffmpeg`.
- **pdftotext** (from poppler) for lesson-plan / district-doc PDF extraction. macOS: `brew install poppler`. Debian/Ubuntu: `apt install poppler-utils`.
- **pandoc** for DOCX extraction. macOS: `brew install pandoc`. Debian/Ubuntu: `apt install pandoc`.
- **~20 GB free disk.** Uploaded video, transcripts, and cached Whisper model weights add up.
- **A public URL.** Coaches click magic-link URLs from their email; those URLs need to reach your host. A subdomain like `https://coach.your-district.org` is typical.
- **TLS certificate.** Magic-link cookies set `Secure` when the request scheme is HTTPS. On plain HTTP the cookie still works, but you're one wifi tap away from a session-hijack. Get real TLS before pilot day.

### Credentials you need in hand

- **Anthropic API key.** The AI scoring calls the Anthropic API. Get one at console.anthropic.com. Store as `ANTHROPIC_API_KEY`.
- **SMTP relay credentials.** Any provider that speaks SMTP works — Postmark, SendGrid, Amazon SES, Fastmail, a self-hosted Postfix, Gmail with an app-password. You'll need `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and a `SMTP_FROM` address the district is comfortable seeing as the sender.
- **The first coach's real email address.** This is what they'll type at `/signin` forever after.

### What you decide before you type anything

- **Org name and slug.** The name is what the coach sees ("Testville Unified"). The slug is a short URL-safe id used internally ("testville") — lowercase, letters/digits/hyphens only.
- **Coach's display name.** How it should appear in "Signed in as …" and as the author of coach-authored records.
- **Public URL** you'll deploy behind (e.g., `https://coach.testville.k12.us`).

---

## 2. Deploy the code

```bash
git clone <your fork of classroom-observer> classroom-observer
cd classroom-observer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Verify the imports resolve:

```bash
python -c "from app import main; print('ok')"
```

Expected: `ok`. If it errors on `faster-whisper`, that's a `pip install` still finishing — some Whisper deps compile natively.

---

## 3. Configure environment

Put these in your process manager's environment (systemd `Environment=` lines, `.env` loaded by your supervisor, or an `export` in a wrapper script — however you run production Python). **Do not commit them to git.**

### Required

| Variable | Purpose | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | The AI scoring calls this key | `sk-ant-…` |
| `OBSERVER_PUBLIC_URL` | Origin used in magic-link URLs sent by email | `https://coach.testville.k12.us` |
| `SMTP_HOST` | Your SMTP relay's hostname | `smtp.postmarkapp.com` |
| `SMTP_PORT` | 587 for STARTTLS, 465 for implicit TLS, 25 or 2525 for plain | `587` |
| `SMTP_USER` | SMTP auth username | provider-specific |
| `SMTP_PASSWORD` | SMTP auth password | provider-specific |
| `SMTP_FROM` | The `From:` header on outgoing mail | `coach@your-district.org` |

### Optional

| Variable | Default | Purpose |
|---|---|---|
| `OBSERVER_DB` | `reports/observations.sqlite` | Path to the SQLite DB file. Point at a mounted volume if you want it to survive container restarts. |
| `OBSERVER_UPLOADS` | `app/uploads` | Directory where uploaded videos land. Same caveat re: mount. |
| `SMTP_STARTTLS` | `1` | Set to `0` only for plain-SMTP dev relays. Real deploys keep this at `1`. |
| `OBSERVER_DEV_LOGIN` | *(unset)* | **Do NOT set in production.** When `1`, the persona-switcher cookie lets you flip between coach/principal/district/teacher roles without email. Only for local dev. |

Sanity-check the environment:

```bash
python -c "import os; print('SMTP_HOST=', os.environ.get('SMTP_HOST') or '(unset — mail will queue)')"
python -c "import os; assert os.environ.get('ANTHROPIC_API_KEY'), 'ANTHROPIC_API_KEY not set'"
```

---

## 4. First boot

Point uvicorn at the app. For a real deploy behind a reverse proxy on 8000:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On first boot, the app:

1. Creates `reports/observations.sqlite` if it doesn't exist.
2. Applies additive migrations (safe on empty and populated DBs).
3. Runs the stuck-job sweep (reclaims any observation left mid-pipeline by a prior crash — see §12).
4. Starts the SMTP sender thread if `SMTP_HOST` is set.

Watch the log. You should see:

```
INFO:     Uvicorn running on http://127.0.0.1:8000
INFO:     SMTP sender started (host=… port=… from=…)
```

Or, if `SMTP_HOST` isn't set:

```
INFO:     SMTP not configured (SMTP_HOST unset) — outbound_mail stays queued; use /dev/mail in dev.
```

If you see the second line and this is a production deploy, go back to §3.

Point your browser at `/signin`. You should see the sign-in form. Every other page redirects to `/signin?next=…` because you're not signed in yet — that's expected.

---

## 5. Onboard your first district

**Stop uvicorn** before this step so nothing races the writes. (Or run against a `--db` path that's not the live one, if you're deploying via blue/green.)

Run the ceremony from the project root:

```bash
python tools/onboard_district.py \
    --org-name "Testville Unified" \
    --org-slug testville \
    --coach-email jane@testville.k12.us \
    --coach-name "Jane Rivera" \
    --seed-district-context \
    --base-url https://coach.testville.k12.us
```

Output looks like:

```
Onboarding 'Testville Unified' into reports/observations.sqlite
------------------------------------------------------------
✓ Promoted auto-seeded org 'Local User' → 'Testville Unified' (slug: 'testville')
✓ Promoted auto-seeded coach 'you@localhost' → 'jane@testville.k12.us' ('Jane Rivera')
✓ Seeded empty district-context for 2026-2027 …
------------------------------------------------------------
Done. First sign-in:

  https://coach.testville.k12.us/auth/br3BwaforMQgHCBiO69Xynw-VsL6vbiFmyxO8nbbSww

Link expires in 15 minutes. …
```

**Copy that URL and send it to Jane** through whatever channel you and she agreed on (signal, text, in-person). She has 15 minutes before it expires.

If the link expires: re-run the ceremony with the same args plus `--rename` — it mints a fresh URL without duplicating rows.

**Optional flags:**
- `--send-signin-email` also queues the email through the outbound_mail queue. If SMTP is already configured, that's redundant with the printed URL; if not yet, the printed URL is the handoff.
- `--rename` updates an already-onboarded org's name in place. Use this when the first ceremony had a typo.

Start uvicorn again.

---

## 6. Verify the first sign-in

- Jane clicks the URL. Browser lands on `/` (dashboard), cookie is set.
- Header shows "Signed in as Jane Rivera".
- Roster (`/teachers`) shows: no teachers yet. That's correct — nothing has been imported.

If Jane's click lands on `/signin?error=bad_link`, the token expired or was already consumed. Re-run the ceremony as above.

If the header shows "Signed in as coach@example.com" or similar, the promotion path was skipped and a stale seed coach exists. Fix by re-running with `--rename` on the intended email, or by hand-editing `users.email` in the DB.

---

## 7. Import the district's roster (optional but common)

Prepare a CSV with columns `name, email, role, employee_id, assigned_coach_email, grade_levels, subjects` — see the `/admin/users/upload` page in the app for the exact schema and an example. Coaches must be listed before the teachers who reference them.

- Sign in as Jane.
- Go to `/admin/users/upload`.
- Upload the CSV.
- The result page shows per-row status: created / already-on-file / error.

Idempotent: re-uploading the same CSV skips already-existing rows. See the audit trail in `audit_log` for a record of every import.

**Consent.** Teachers imported via CSV do NOT auto-consent. Before a coach can upload the first observation of a teacher, that teacher must:
1. Sign in themselves via magic link (using the email on their teacher record).
2. Click the "I agree" button on their own teacher-view.

Or, if the district has already collected paper consent and you want to bypass this for pilot, hand-insert consent records via `python` or `sqlite3` — but do it deliberately, not by default. The web audit relies on the `consent_records.granted_by_user_id` matching a real signature.

---

## 8. Set district context (recommended)

If you passed `--seed-district-context` in step 5, an empty district_context row already exists for the current academic year. Have Jane visit `/admin/district-context` and fill in:

- **District priorities** — the specific instructional focuses coaches are asking teachers to work on this year. These roll up on the district dashboard and seed goal chips on teacher pages.
- **Initiatives in flight** — curriculum roll-outs, PD threads, testing prep windows. The AI sees these when scoring.
- **Observation cadence** — how many observations per teacher per year and the max days between. Drives the "time to be back in the room" cue on teacher hubs.
- **Year arc phases** — the specific timing of the district's coaching year (onboarding / baseline / cycles / renewal / testing prep). The AI uses the current phase to calibrate its recommendations.
- **Documents** — PDFs of the district's coaching framework, arc-of-year memo, curriculum pacing guides. Text is extracted server-side and fed to the AI. **Every uploaded document's text can influence the AI's read on every observation in this district** — treat these as trusted-source-of-truth, not "we'll add anything a coach asks for."

---

## 9. First observation upload

Have Jane pick a teacher (must have active consent — see §7):

- Click into the teacher's hub.
- Click "Bring in an observation".
- Upload a video (MP4 / MOV / WebM).
- Pick the rubric (default: TNTP 4-point 2014).
- Pick the observation date (defaults to today).

The upload page redirects to the observation detail with a live-polling status: `pending → transcribing → scoring → complete`. On a laptop-class CPU with the default `medium` Whisper model, a 45-minute observation takes ~10 minutes end-to-end.

When it lands as `complete`:
- Coach sees the AI's read across all four rubric domains.
- Coach can "name a move" (published_coach_moves) — the teacher-facing surface never shows raw AI output, only what the coach chose to publish.
- The AI's suggested `highest_leverage_move` is right there for the coach to accept, edit, or override.

---

## 10. Day-2 operations

### Restarting the app

The single-worker background executor is in-process. On restart:
- Any observation caught mid-pipeline is left in `transcribing` or `scoring` status.
- The startup sweep (`sweep_stuck_jobs`) marks anything older than 30 minutes as `failed` with a clear reason so the coach can re-upload.
- Fresh uploads pick up as normal.

Restart cadence should be off-hours — coaches uploading a video during a restart lose that upload and have to retry.

### Monitoring the mail queue

Every magic-link email goes through the `outbound_mail` table before SMTP send. Check the queue depth periodically:

```bash
sqlite3 reports/observations.sqlite "
SELECT
  COUNT(*) AS total,
  SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) AS sent,
  SUM(CASE WHEN failed_at IS NOT NULL THEN 1 ELSE 0 END) AS failed,
  SUM(CASE WHEN sent_at IS NULL AND failed_at IS NULL THEN 1 ELSE 0 END) AS pending
FROM outbound_mail;"
```

If `pending` climbs without `sent` climbing, the SMTP sender is stuck or misconfigured. Check the uvicorn error log — every send failure logs at `ERROR` with the underlying reason.

Failed rows stay failed on purpose (no automatic retry). If a coach needs a resend, they hit `/signin` again — that mints a fresh token; older failed rows are audit trail.

### Reading recent failures

```bash
sqlite3 reports/observations.sqlite "
SELECT to_email, subject, failed_at, failure_reason
FROM outbound_mail
WHERE failed_at IS NOT NULL
ORDER BY failed_at DESC LIMIT 20;"
```

### Watching for stuck observations

```bash
sqlite3 reports/observations.sqlite "
SELECT id, video_filename, status, uploaded_at
FROM observations
WHERE status IN ('transcribing', 'scoring')
  AND uploaded_at < datetime('now', '-30 minutes');"
```

Rows here should be zero — the sweep runs at every restart. A row that keeps showing up means either the sweep isn't firing (check startup log) or the worker is genuinely stuck (in which case a restart clears it).

### Backing up the DB

SQLite is a single file. Copy it while the app is quiesced (no in-flight writes):

```bash
systemctl stop classroom-observer  # or your equivalent
cp reports/observations.sqlite reports/backups/observations-$(date +%Y%m%d-%H%M%S).sqlite
systemctl start classroom-observer
```

Or use `.backup` for a hot backup that doesn't require quiescing:

```bash
sqlite3 reports/observations.sqlite ".backup reports/backups/observations-$(date +%Y%m%d-%H%M%S).sqlite"
```

The uploads directory (`app/uploads/`) is separate — back it up on the same cadence. Each observation's video + frames live under `app/uploads/<observation_id>/`.

### Adding a second coach

Have the existing coach (or you, via the DB) do it:
- Via UI: `/admin/users/upload` with a one-row CSV where `role=coach`.
- Via DB: `INSERT INTO users (id, org_id, email, name, role, created_at) VALUES (…, 'coach', …)`.

They then use `/signin` with their email — the app queues a magic-link email, SMTP delivers it.

Cross-caseload isolation is enforced: each coach sees only teachers where `assigned_coach_user_id = <their user_id>`. Teachers can be reassigned by editing the roster row.

---

## 11. Troubleshooting

### Sign-in email never arrives

1. Coach checks spam.
2. Operator checks the outbound_mail queue (§10).
3. If `failed`, read `failure_reason` — usually a bad `SMTP_USER` / `SMTP_PASSWORD` or a `SMTP_FROM` your provider doesn't allow.
4. If `pending` (never sent), the SMTP sender thread isn't running — check the uvicorn log for `SMTP sender started` at boot, or `SMTP not configured` (meaning `SMTP_HOST` isn't set).

### Coach signs in, but sees no teachers

Roster is coach-scoped. Every teacher row has an `assigned_coach_user_id`; the coach sees only teachers whose value matches their own `user_id`. If Jane is signed in but sees an empty roster:

```bash
sqlite3 reports/observations.sqlite "
SELECT t.name, u.email AS assigned_coach
FROM teachers t
LEFT JOIN users u ON u.id = t.assigned_coach_user_id
ORDER BY t.name;"
```

Reassign in bulk via the CSV upload flow (put the new email under `assigned_coach_email`), or in the DB directly:

```bash
sqlite3 reports/observations.sqlite "
UPDATE teachers SET assigned_coach_user_id = (
  SELECT id FROM users WHERE email='jane@testville.k12.us'
) WHERE assigned_coach_user_id IS NULL;"
```

### Upload lands `failed` with "hasn't consented"

The teacher hasn't consented yet. Options:
- Have the teacher sign in themselves and click "I agree" on their teacher-view.
- If paper consent was collected, insert a `consent_records` row by hand naming that paper form's version — but do this deliberately, and document who took responsibility.

### Upload lands `failed` with "cross-coach caseload"

Another coach has a teacher with the same name. Either coordinate with the other coach to hand off the record, or upload under a distinguishing name ("Ms. Rivera (5th)" vs. "Ms. Rivera (K)").

### Upload lands `failed` with an ffmpeg or Whisper timeout

The file is probably corrupt or in an unusual codec. Try re-exporting as MP4/H.264/AAC in QuickTime, DaVinci, or `ffmpeg -i in.mov -c:v h264 -c:a aac out.mp4`. Timeouts are 30 min per ffmpeg pass, 45 min for the Whisper transcription — a lesson longer than that (unlikely) needs the timeouts widened in `pipeline/audio.py`, `pipeline/frames.py`, `pipeline/transcribe.py`.

### Scoring lands `failed` with a Pydantic validation error

The AI returned a shape the code doesn't accept. The retry escalates to a bigger token budget on truncation errors; if it still fails, the underlying Anthropic response is diagnosable in the observation's `_work/` directory (kept for exactly this reason). Read the diag JSON, share with the maintainers.

### Anything else 500s

Read the uvicorn log for a Python traceback. The app never intentionally leaves a request as a 500 — every raised `HTTPException` is a specific status. A raw 500 is a bug; capture the traceback and the request path, share with the maintainers.

---

## 12. What's NOT in scope for pilot

The following exist for development and should not be enabled in production:

- **`tools/seed_sample_data.py`** — creates the four "Summey / Dulaney / Lopez / Livingston" test teachers. Guards refuse against a real DB, but do not run it. If you did, run `--dry-run` first.
- **`OBSERVER_DEV_LOGIN=1`** — the persona-switcher cookie that lets you flip between coach/principal/district/teacher roles without email. Only useful for local dev. Never set in production.
- **`/dev/mail`** — the outbound-mail viewer. Only reachable when `OBSERVER_DEV_LOGIN=1`; it would otherwise be a magic-link peek oracle.
- **`tools/import_baselines.py`** — imports pre-computed baseline reports for regression testing. Not used in production.

---

## 13. Getting help

If something goes wrong:
1. Grab the last 200 lines of the uvicorn log.
2. Grab the DB's audit_log rows around the incident: `sqlite3 reports/observations.sqlite "SELECT * FROM audit_log ORDER BY occurred_at DESC LIMIT 50;"`.
3. Note what the user was clicking when it broke.

Send those three things to the maintainers.

---

## Appendix: environment variable reference

| Var | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required.** AI scoring calls the Anthropic API. |
| `OBSERVER_PUBLIC_URL` | request scheme+host | Origin used in magic-link URLs sent by email. Set to your public HTTPS origin. |
| `OBSERVER_DB` | `reports/observations.sqlite` | Path to SQLite DB. |
| `OBSERVER_UPLOADS` | `app/uploads` | Where uploaded videos land. |
| `SMTP_HOST` | *(unset)* | SMTP relay hostname. When unset, outbound_mail queues indefinitely — dev-only. |
| `SMTP_PORT` | `587` | SMTP relay port. 465 for implicit TLS, 587 for STARTTLS, 25/2525 for plain (dev). |
| `SMTP_USER` | *(unset)* | SMTP auth username. Some relays don't need auth. |
| `SMTP_PASSWORD` | *(unset)* | SMTP auth password. |
| `SMTP_FROM` | `no-reply@classroom-observer.local` | `From:` header on outbound mail. |
| `SMTP_STARTTLS` | `1` | Set to `0` for plain SMTP relays only. |
| `OBSERVER_DEV_LOGIN` | *(unset)* | **DO NOT SET IN PRODUCTION.** Enables persona-switcher cookie + `/dev/mail`. |

## Appendix: file layout on the deployed host

```
classroom-observer/
├── reports/
│   └── observations.sqlite         # the DB (backup this)
├── app/
│   ├── uploads/<obs_id>/           # uploaded videos + extracted frames (backup this)
│   ├── lesson_plans/<plan_id>/vN/  # coach-uploaded lesson plans (backup this)
│   └── ...
├── district_documents/<doc_id>/    # district-uploaded PDFs (backup this)
└── ...
```

Everything else is code and can be rebuilt from git.
