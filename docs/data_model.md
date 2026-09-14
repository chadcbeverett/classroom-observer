# Data Model — Multi-Tenant Migration Design

*A schema you can hand to a DB (Postgres/Supabase/Firebase) and start building
against. Rubric-agnostic. FERPA-friendly. Concrete enough to critique.*

---

## Design principles

1. **One row scope: `org_id`.** Every table (except a small set of global
   registries) carries an `org_id`. Every query is filtered by it. In Postgres
   this becomes row-level security (RLS); in Firebase, security rules.
2. **AI outputs are versioned.** The model produces a report; a coach may
   edit it before publishing. Both versions must be retrievable and
   attributable.
3. **Consent is a first-class artifact, not a checkbox.** Each observation
   references a consent record with the form version signed.
4. **Every access is logged.** For district contracts and FERPA breach
   response, we need to be able to answer "who read this teacher's data?"
5. **Retention is per-org configurable and enforced by scheduled jobs**, not
   by hoping people manually delete things.
6. **Blob storage separates from metadata.** Videos, transcripts, frame
   images, and rendered PDFs live in object storage; the DB stores references.

---

## Entities

Nine tables. Grouped by concern.

### Tenancy

#### `organizations`
The tenant boundary. A school, district, EMO, or single-user account.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| name | text | Display name. |
| slug | text unique | URL-safe identifier. |
| plan_tier | text | `free` / `pilot` / `district`. |
| is_educational_institution | bool | Triggers FERPA compliance path. |
| signed_dpa_at | timestamptz null | When the district signed our data protection agreement. Null = no signed DPA on file. |
| dpa_document_ref | text null | Path to signed DPA in blob storage. |
| default_rubric_id | uuid null | Which rubric shows up by default in the UI. |
| retention_policy_id | uuid null → `retention_policies` | |
| created_at | timestamptz | |
| archived_at | timestamptz null | Soft-delete for winding down a customer. |

#### `retention_policies`
Per-org retention rules, applied by a nightly job.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| video_retention_days | int | e.g. 90 |
| transcript_retention_days | int | e.g. 365 |
| report_retention_days | int | e.g. 1825 (5 years — common EDU retention) |
| audit_log_retention_days | int | e.g. 2555 (7 years) |
| updated_at | timestamptz | |

### Identity

#### `users`
People who log in.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| email | text | Unique per org. |
| name | text | |
| role | text | `admin` / `coach` / `teacher_self_serve` / `viewer`. See "Product decision A" below. |
| auth_provider | text | `email` / `google` / `microsoft` / `district_sso`. |
| auth_provider_id | text null | External subject id when SSO. |
| is_active | bool | Deactivated ≠ deleted; preserves audit references. |
| created_at | timestamptz | |
| last_login_at | timestamptz null | |

### Domain

#### `teachers`
People being observed. **May or may not be a `user`.** A district might observe 50 teachers, only 10 of whom log in.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| name | text | |
| email | text null | |
| employee_id | text null | District's own identifier. |
| user_id | uuid null → `users` | Populated only if the teacher self-serves. |
| assigned_coach_user_id | uuid null → `users` | Primary coach. |
| grade_levels | text[] null | e.g. `{'6','7','8'}`. |
| subjects | text[] null | e.g. `{'ELA','Math'}`. |
| created_at | timestamptz | |
| archived_at | timestamptz null | Teacher left / no longer observed. |

#### `rubrics`
Instantiated rubrics available to an org.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid null | Null = built-in (TNTP shipped by default). |
| kind | text | `tntp_core_4pt_2014` / `danielson_2013` / `class_k3` / `custom`. |
| name | text | Human-readable. |
| version | text | Rubric version — a district may customize TNTP. |
| pdf_ref | text | Blob storage path for the reference PDF. |
| config_json | jsonb | The rubric definition (domains, ratings, essential_questions, vocabulary, coaching_philosophy, scoring_notes) — the `Rubric` dataclass shape. |
| created_at | timestamptz | |
| archived_at | timestamptz null | Rubric no longer in use for new observations. |

### Observations & reports

#### `observations`
The main object. One per uploaded video.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| teacher_id | uuid fk | |
| observer_user_id | uuid fk → `users` | Coach who initiated / uploaded. |
| rubric_id | uuid fk | Snapshot of which rubric this was scored against. |
| coaching_cycle_id | uuid null → `coaching_cycles` | See Product decision B. |
| consent_record_id | uuid null → `consent_records` | See Product decision C. |
| video_ref | text | Blob storage path. |
| video_filename | text | Original upload name. |
| video_duration_s | numeric | |
| video_bytes | bigint | |
| transcript_ref | text null | Blob storage path. |
| transcript_model | text | e.g. `faster-whisper:small` or `replicate:whisper-large-v3`. |
| frames_prefix | text null | Blob storage path prefix for sampled frames. |
| frame_count | int | |
| frame_interval_s | numeric | |
| status | text | `pending` / `transcribing` / `scoring` / `complete` / `failed` / `deleted`. |
| failure_reason | text null | If status = failed. |
| observed_at | timestamptz null | When the lesson was originally taught. Coach-supplied. |
| uploaded_at | timestamptz | |
| scored_at | timestamptz null | When the AI produced the initial report. |
| deleted_at | timestamptz null | Soft-delete for retention/consent-revocation. |

#### `report_versions`
Versioned reports for an observation. Version 1 is always the AI's raw output. Version 2+ exists only if a coach edited before publishing.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| observation_id | uuid fk | |
| version_number | int | 1 = AI raw; ≥2 = coach edits. |
| authored_by | text | `ai` / `human`. |
| authored_by_user_id | uuid null → `users` | Set when human. |
| opening_paragraph | text | |
| overall_summary | text | |
| domain_assessments | jsonb | Full `DomainAssessment` list. |
| coaching_recommendations | jsonb | |
| rendered_markdown | text | Cached compose output. |
| published_at | timestamptz null | If set, this version is visible to the teacher. |
| published_by_user_id | uuid null | |
| notes | text null | Coach's private notes about the edit. |
| created_at | timestamptz | |

Constraint: at most one row per `observation_id` with `published_at IS NOT NULL`.

#### `consent_records`
Legal artifact — teacher (and where applicable, guardians) consented to be recorded and analyzed.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| teacher_id | uuid fk | |
| scope | text | `single_observation` / `academic_year` / `perpetual_until_revoked`. |
| form_version | text | Which consent form the teacher signed. |
| method | text | `electronic_signature` / `paper_scan` / `verbal_witnessed`. |
| consented_at | timestamptz | |
| revoked_at | timestamptz null | Populated on revocation; downstream deletion is triggered. |
| consent_artifact_ref | text | Signed PDF / scan / recording. |

#### `coaching_cycles` (optional; see Product decision B)
Groups related observations, goals, and follow-ups.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| teacher_id | uuid fk | |
| coach_user_id | uuid fk → `users` | |
| opened_at | timestamptz | |
| closed_at | timestamptz null | |
| goal_focus_skills | text[] | Which coaching-construct names the cycle is targeting. |
| notes | text | |

### Compliance & audit

#### `audit_log`
Append-only. Every read, write, publish, share, export, delete.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | Even system actions get org_id when known. |
| actor_user_id | uuid null | Null = system action (nightly jobs, etc.). |
| actor_ip | inet null | |
| action | text | `view_report` / `edit_report` / `publish_report` / `download_video` / `export_csv` / `delete_observation` / `revoke_consent` / `add_user` / … |
| target_type | text | e.g. `observation` / `teacher` / `report_version`. |
| target_id | uuid | |
| metadata | jsonb null | Action-specific detail (e.g., version_number for publishes). |
| occurred_at | timestamptz | |

Indexes: `(org_id, occurred_at desc)`; `(target_type, target_id, occurred_at desc)`.

#### `usage_events`
Cost and volume tracking — for billing and for surfacing runaway spend.

| column | type | notes |
|---|---|---|
| id | uuid pk | |
| org_id | uuid fk | |
| observation_id | uuid null fk | |
| kind | text | `transcription_seconds` / `anthropic_input_tokens` / `anthropic_output_tokens` / `anthropic_cache_write_tokens` / `anthropic_cache_read_tokens` / `blob_storage_bytes_day`. |
| quantity | numeric | |
| unit_cost_cents | numeric null | If we know the unit price at time of event. |
| occurred_at | timestamptz | |

---

## Blob storage layout

Object storage (S3 / R2 / Supabase Storage). Every path prefixed by `org_id/`.

```
<org_id>/
  videos/<observation_id>/original.<ext>
  transcripts/<observation_id>/transcript.json
  frames/<observation_id>/frame_0001.jpg … frame_NNNN.jpg
  reports/<observation_id>/report.md
  reports/<observation_id>/report.pdf         # if generated
  rubrics/<rubric_id>.pdf
  consent/<consent_record_id>.pdf
  dpa/<org_id>-signed.pdf
```

Lifecycle rules on the bucket enforce retention policies (auto-delete after
N days). RLS or bucket policies enforce that a user's session only reads
paths starting with their `org_id`.

---

## Access patterns

Common queries the schema is optimized for:

| Query | Path |
|---|---|
| Teacher opens dashboard → their published reports | `report_versions` where `observation.teacher_id = me AND published_at IS NOT NULL` |
| Coach opens dashboard → observations for assigned teachers | `observations` join `teachers` on `assigned_coach_user_id = me` |
| Admin exports district roll-up | filter `observations` by `org_id` + date range |
| Nightly retention job | delete `observations.video_ref` where `deleted_at + video_retention_days < now()` |
| Compliance query "who accessed teacher X's data" | `audit_log` where `target_type = 'teacher' AND target_id = ?` |

---

## Product decisions that would change the shape

Flagging these because the schema will differ meaningfully depending on the answers. These are business/product decisions, not technical ones.

### Decision A — Roles and self-service
> Do teachers log in to see their own reports, or do coaches always deliver reports through some other channel (e.g., email, in-person)?

- **If teachers self-serve** → `teachers.user_id` is populated; `role='teacher_self_serve'` is a real user role; you need teacher-facing UI.
- **If not** → `teachers` and `users` are almost disjoint (only coaches log in). You can skip teacher-facing UI entirely.

### Decision B — Coaching cycles
> Is an observation a one-shot event, or one step in an ongoing coaching cycle where the coach references prior observations, goals, and follow-ups?

- **One-shot** → drop `coaching_cycles`; `observations` is standalone. Simpler product.
- **Cycle-based** → `coaching_cycles` is core; teachers accumulate goal history; coaching_cycle_id becomes a required field on observations.

### Decision C — Consent flow
> Is consent a single lifetime signature per teacher, an annual re-up, or per-observation?

- **Perpetual/annual** → one `consent_records` row per teacher per year; observations reference the current active one.
- **Per-observation** → consent record required for every observation; upload is blocked until signed.

Both flows fit the schema (via `scope` field on `consent_records`). But the UX and legal artifacts differ significantly.

### Decision D — Report editing workflow
> Does the coach edit the AI's report before the teacher sees it? Or is the AI output published directly?

- **Direct-publish** → `report_versions` has only version 1. `published_at` on `observations` is enough.
- **Coach-edited** → `report_versions` is core; version 1 is AI, version 2+ is coach edits; publish is a separate action.

The schema above supports both by treating `report_versions` as always present. Direct-publish is just "version 1 is auto-published on scoring completion."

### Decision E — Rubric customization
> Do districts customize the rubric (edit descriptor language, add domains), or do they pick from a fixed registry?

- **Fixed registry** → `rubrics.org_id` is always null; `kind` selects from a small set.
- **Customizable** → `rubrics.org_id` is populated for custom rubrics; districts can edit `config_json`. Adds significant complexity — the rubric-independence refactor already prepared for this technically.

### Decision F — Multi-role users
> Can one person be both a coach and an admin?

- **Single-role** → keep the `role` enum column on `users`.
- **Multi-role** → move to a `user_roles` join table.

Multi-role is more common in real orgs (a lead coach who is also an admin). Recommendation: multi-role from day one; it's a small extra table but avoids a painful migration later.

---

## FERPA-relevant fields (audit checklist)

Every one of these needs to work correctly before you sell to a K-12 customer.

- [ ] `organizations.signed_dpa_at` populated for every district customer.
- [ ] `consent_records` exists for every observation involving a minor's classroom video.
- [ ] `consent_records.revoked_at` triggers cascading deletion of downstream artifacts.
- [ ] `retention_policies` scheduled deletion runs and is monitored.
- [ ] `audit_log` captures every read of teacher-identifiable data.
- [ ] `users.role` enforces least-privilege access (RLS in DB).
- [ ] Blob storage lifecycle rules mirror `retention_policies` retention.
- [ ] Data export (`export_csv`, downloading reports) writes an `audit_log` entry.
- [ ] Data deletion (retention or on-request) is verified — not just soft-delete.
- [ ] `usage_events` are visible to org admins for cost transparency (school budgets are tight).

---

## What this schema does NOT yet cover

Things that will need their own schema when relevant:

- **Billing** — Stripe customer id, subscription, invoices. Live in Stripe primarily; store references only.
- **Notifications** — email/SMS delivery records, opt-outs. Separate concern.
- **Integrations** — Google Classroom / Clever / ClassLink roster sync. Big topic; separate design.
- **Video conferencing capture** — recording from Zoom/Google Meet directly. Big topic.
- **Longitudinal analytics** — cohort dashboards, trend charts. These are read-side views over the tables above; not new tables.
- **Multi-org shared users** — a coach who works across districts. Would need `user_org_memberships` table.

---

## Next-step candidates once this is settled

1. Convert this doc to migrations (Alembic / Supabase migrations / Prisma).
2. Build a thin Python adapter layer so `observe.py` can write to the DB instead of JSON files. Keep the CLI working locally by wrapping with a SQLite-backed adapter.
3. Write RLS policies for Postgres/Supabase enforcing `org_id` scoping.
4. Draft the consent form and DPA template (legal work; do before the first pilot).
