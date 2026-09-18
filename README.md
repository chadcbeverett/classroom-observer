# Classroom Observer

AI-assisted teacher observation and coaching. A coach uploads a video of a lesson; the app transcribes it, samples frames, and scores the observation against a district-selected rubric (TNTP Core Teaching Rubric by default). The coach reviews the AI's read, publishes a coaching move for the teacher, and tracks the arc across coaching cycles.

Architecture: FastAPI + SQLite + faster-whisper + Anthropic API + a small in-process job queue. Designed to run on one server behind a reverse proxy for a district-sized pilot.

**Status:** ready for a small pilot (1 district, up to ~5 coaches). See the [scaling notes in the runbook](PILOT_RUNBOOK.md#12-whats-not-in-scope-for-pilot) for the thresholds where the current architecture starts to bind.

## How it works

```
video.mp4
   │
   ├─ ffmpeg          ──►  audio.wav (mono 16kHz)
   ├─ faster-whisper  ──►  timestamped transcript
   ├─ ffmpeg          ──►  sampled frames (1 / minute)
   │
   ├─ context.py      ──►  teacher profile + goals + prior obs trajectory
   │                       + district priorities + coach's running narrative
   │                       (fenced as untrusted-data in the system prompt)
   │
   └─ Claude (adaptive thinking + prompt caching)
         system:  rubric PDF + scoring instructions + GBF scope-and-sequence
                  + teacher/district context block                 ◄── cached
         user:    transcript + frame images
         output:  ObservationReport (Pydantic-validated + rubric-checked)
                    ↓
         coach reviews AI's read → publishes a coaching move for the teacher
         teacher responds → coach acknowledges → cycle continues
```

The rubric PDF and static scoring instructions sit in the system prompt with a cache breakpoint, so the second observation onward is ~90% cheaper on the cached portion. Every compute step outside the Anthropic call runs on the server.

Coach-mediates-AI is a hard architectural constraint: teachers never see raw AI output. The `published_coach_moves` table holds the coach's edited version — with a supersede pattern for revisions — and the teacher-facing view reads only from there.

## Deploying

Full deployment instructions live in **[PILOT_RUNBOOK.md](PILOT_RUNBOOK.md)** — read it top to bottom before deploying. It covers prereqs, environment configuration, first boot, onboarding, verification, day-2 ops, and troubleshooting.

The short version:

```bash
git clone https://github.com/chadcbeverett/classroom-observer
cd classroom-observer

# Configure — the runbook explains each var
cp .env.example .env
$EDITOR .env

# Bring the stack up
docker compose up -d

# Onboard your first district + coach (prints a magic-link URL)
docker compose exec app python tools/onboard_district.py \
    --org-name "Testville Unified" --org-slug testville \
    --coach-email jane@testville.k12.us --coach-name "Jane Rivera" \
    --seed-district-context
```

Hand the printed URL to the coach; they sign in and see their (empty) roster. From there they upload rosters, set district context, and start uploading observations.

## What you'll need

- A host with Docker + a mounted volume for persistent state
- A public HTTPS URL and TLS certificate (put a reverse proxy in front)
- An Anthropic API key (`ANTHROPIC_API_KEY`)
- SMTP relay credentials for magic-link email delivery

## Repository layout

```
├── PILOT_RUNBOOK.md           ← start here for deployment
├── Dockerfile                 ← multi-stage image (Python + ffmpeg + poppler + pandoc)
├── docker-compose.yml         ← service definition + env contract
├── .env.example               ← copy to .env
├── app/                       ← FastAPI web app
│   ├── main.py                ← routes, viewer resolution, guards
│   ├── jobs.py                ← persistent job queue + background worker
│   ├── smtp_sender.py         ← outbound_mail queue drainer
│   ├── rate_limit.py          ← /signin sliding-window limiter
│   ├── logging_setup.py       ← opt-in JSON logging
│   └── templates/             ← Jinja2 views
├── pipeline/                  ← AI + media pipeline (rubric-agnostic)
│   ├── schema.sql             ← storage contract
│   ├── db.py                  ← DB helpers + additive migrations
│   ├── score.py               ← Anthropic scoring call + retry
│   ├── context.py             ← teacher/district context assembly
│   ├── schemas.py             ← Pydantic response shapes
│   ├── rubric.py              ← rubric definitions (TNTP today)
│   ├── gbf.py                 ← Get Better Faster scope-and-sequence
│   ├── audio.py, frames.py, transcribe.py, text_extract.py
│   └── ...
├── tools/
│   ├── onboard_district.py    ← one-shot district onboarding ceremony
│   ├── import_baselines.py    ← import pre-scored baseline reports (dev)
│   ├── seed_sample_data.py    ← dev-only sample data (see runbook §12)
│   └── regression.py          ← regression harness
├── observe.py                 ← CLI scoring wrapper for one video
└── export_csv.py              ← CSV export tool
```

## Development

Local dev without Docker, if you have Python 3.9+ and the system deps installed (see runbook §1):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Local single-user mode — persona-switcher cookie stands in for real auth
export OBSERVER_DEV_LOGIN=1
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --port 8000 --reload
```

Visit http://localhost:8000. The persona-switcher chip in the header lets you flip between coach / principal / district / teacher without going through magic-link email. `/dev/mail` shows the outbound-mail queue for local sign-in testing.

## Security posture

The codebase has been through eight rounds of agent-driven security + correctness audits (44 real defects found and fixed). What holds:

- Magic-link auth with server-side sessions (HttpOnly, SameSite=lax, Secure-on-HTTPS)
- Coach-scoped views: coaches see only their assigned caseload; principals see the org; teachers see their own record
- Cross-caseload write-guards on every teacher-scoped POST route
- Consent gate: no observation upload without an active consent record for the teacher
- Prompt-injection defense: user-authored text and PDF-extracted district docs are fenced as untrusted-data inside the AI system prompt
- Stored-XSS defense: AI-rendered report HTML runs through a bleach whitelist that strips anything the markdown renderer wouldn't produce
- Media path timeouts + host-path scrubbing before failure_reason lands in the DB
- Persistent job queue with atomic-claim + bounded retry — restarts don't lose queued work

See the git log for the round-by-round audit history if you're auditing this yourself.

## License

Private — not open source. Contact the maintainers for use.
