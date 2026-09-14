# Classroom Observer

Score a classroom observation video against the **TNTP Core Teaching Rubric (4-point, 2014)** and produce a coaching narrative aligned to TNTP's framework — locally, end-to-end.

## What it produces

For each video, three files in the output directory:

| File | Contents |
| --- | --- |
| `scores.json` | Structured rubric scores: 4 performance areas, each with sub-descriptor ratings, timestamped evidence, and rationale. Plus 1–2 prioritized Core Teacher Skills with bite-sized actions for the next week. |
| `report.md` | Markdown coaching narrative (~800–1200 words) addressed to the teacher, aligned to TNTP's coaching framework. |
| `transcript.json` | Timestamped transcript (for reference / verification). |

## How it works

```
video.mp4
   │
   ├─ ffmpeg          ──►  audio.wav (mono 16kHz)
   ├─ faster-whisper  ──►  timestamped transcript
   ├─ ffmpeg          ──►  sampled frames (1 / minute)
   │
   └─ Claude Opus 4.7 (adaptive thinking + prompt caching)
         system: TNTP rubric PDF + scoring instructions  ◄── cached
         user:   transcript + frame images
         output: ObservationReport (Pydantic-validated)
```

The rubric PDF and scoring instructions sit in the system prompt with a cache breakpoint, so **the second observation onward is ~90% cheaper** on the cached portion. All compute outside the Claude API call runs locally.

## Setup

```bash
# 1. System dependencies (one-time)
brew install ffmpeg poppler

# 2. Python dependencies — use a venv to avoid polluting system Python
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

## Usage

```bash
python observe.py path/to/observation.mp4
```

Outputs land in `reports/<timestamp>/`.

### Options

```bash
python observe.py path/to/observation.mp4 \
    --rubric rubrics/TNTPCoreTeachingRubric_4pt_2014.pdf \
    --out reports/jane_doe_2026-05-11 \
    --whisper-model medium \
    --frame-interval 60 \
    --keep-intermediate
```

| Flag | Default | Notes |
| --- | --- | --- |
| `--rubric` | `rubrics/TNTPCoreTeachingRubric_4pt_2014.pdf` | Path to the rubric PDF. |
| `--out` | `reports/<YYYYMMDD_HHMMSS>` | Output directory. |
| `--whisper-model` | `medium` | `tiny` / `base` / `small` / `medium` / `large-v3`. `medium` is the default — spot-check found `small` hallucinated words and dropped negations. `medium` is faster than `small` on Apple Silicon due to Metal acceleration; on plain-CPU VPS it's ~2× slower but still faster than real-time. |
| `--frame-interval` | `60` | Seconds between sampled frames. 60s ≈ 20–25 frames for a typical observation. |
| `--keep-intermediate` | off | Keep extracted audio and frames in `<out>/_work` for inspection. |

## Costs

Rough per-observation estimate at default settings (25-min video, `medium` Whisper, Opus 4.7):

- Transcription: free (runs locally).
- Frame sampling: free (runs locally).
- Claude API call: roughly **$0.30–$0.80 per observation** depending on transcript length and adaptive-thinking depth. First call writes the rubric to the cache (~1.25× write premium); subsequent calls within 5 minutes read from cache at ~0.1× normal rate.

Set `--whisper-model tiny` or `base` for faster (less accurate) transcription on slow CPUs.

## Calibration notes

- The schema enforces at least 2 pieces of evidence per descriptor and ties scores to TNTP's exact rating language.
- The system prompt instructs Claude to use the rubric as the authoritative source and to apply preponderance-of-evidence scoring (not averaging).
- The narrative is written in second person to the teacher and closes with TNTP's bite-sized-action pattern.
- A `Developing` (3/4) rating is the rubric's expectation for most observed lessons — don't read it as a failure.

## Web UI (local FastAPI app)

The pipeline can also be driven through a local web UI — teacher-centric coaching workflow, upload page, and reports rendered in the browser.

```bash
# One-time
brew install ffmpeg poppler
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Every session
export ANTHROPIC_API_KEY="sk-ant-..."
.venv/bin/uvicorn app.main:app --port 8080 --reload
```

Open **[http://localhost:8080](http://localhost:8080)**. Navigation:

| Page | What's there |
|---|---|
| `/teachers` | Roster with per-domain rating trends. |
| `/teachers/{id}` | Teacher hub: profile (both sides), coaching cycles, goals, private notes, action tracking, observations. |
| `/teachers/{id}/profile/teacher` | Teacher-authored profile form (quantitative + self-ratings). |
| `/teachers/{id}/profile/coach` | Coach-authored profile form (public-to-teacher observations). |
| `/cycles/{id}` | Cycle detail: goals, observations, impact metrics, close-cycle form. |
| `/cycles/{id}/print` | Print-friendly cycle report (Cmd+P → Save as PDF). |
| `/observations/{id}` | Observation detail: rendered report, video player (new uploads only), coaching cycle attachment, bite-sized action tracking. |
| `/admin/district-context` | Set the year arc + priorities + initiatives for an academic year. |
| `/` | Observation upload + list. |

### Walkthrough with sample data

Run once against the local DB to populate distinct scenarios per teacher:

```bash
.venv/bin/python tools/seed_sample_data.py
```

That gives you four different states to click through:

- **Summey** — Year 2, fresh open cycle, one active goal, observation not yet attached.
- **Dulaney** — Year 6, active cycle with observation attached, one private coach note.
- **Lopez** — Year 14, one closed-met cycle + a fresh active cycle.
- **Livingston** — Year 8, active cycle with observation attached.

Plus a district-context row for 2026-2027 with three priorities and two initiatives.

Then start the server and browse `/teachers` to work through them. The re-scored reports (produced under Phase B with context injected) each feature a **highest-leverage coaching move** at the top that names the teacher's specific stage, active goal, and prior trajectory.

## Limitations

- Vision is sampled (one frame per minute by default). Brief moments between samples can be missed; lower `--frame-interval` (e.g. 30s) for higher fidelity at higher cost.
- No speaker diarization — the transcript reads as a single voice stream. Most TNTP descriptors are about student/teacher *behaviors* rather than specific speaker attribution, so this is usually fine.
- The pipeline is single-observation. For longitudinal coaching, compare `scores.json` files across observations.
