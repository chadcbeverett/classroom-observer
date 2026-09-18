# Classroom Observer — production container image
#
# Two-stage build. The builder stage installs Python deps into a venv;
# the final stage copies the venv into a slimmer image with just the
# runtime OS packages we need.
#
# Runtime OS deps come from PILOT_RUNBOOK.md §1:
#   - ffmpeg / ffprobe       (audio extract, frame sampling)
#   - poppler-utils          (pdftotext, for PDF district docs / lesson plans)
#   - pandoc                 (for DOCX extraction — big install, ~150 MB;
#                             see runbook for the "drop DOCX to shrink" option)
#
# Volumes:
#   /data                    persistent state — SQLite DB, uploads, lesson
#                            plans, district documents. Mount to a host path
#                            (or Docker volume) so restarts don't lose data.
#
# The app writes DB to $OBSERVER_DB and uploads under $OBSERVER_UPLOADS;
# defaults land inside the /data volume.

# -----------------------------------------------------------------------------
# Stage 1: builder — install Python deps into an isolated venv
# -----------------------------------------------------------------------------
FROM python:3.11-slim AS builder

# Build deps for wheels that compile from source (faster-whisper's ctranslate2
# uses precompiled wheels for x86_64 / arm64 linux, but keeping gcc around
# is cheap and covers the fallback path).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create the venv first so pip caches wire cleanly. Copy only requirements
# so `pip install` re-uses the layer when app code changes but deps don't.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r /tmp/requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: runtime
# -----------------------------------------------------------------------------
FROM python:3.11-slim

# Runtime OS deps. pandoc is optional — set DROP_DOCX_SUPPORT=1 at build
# time to skip it and shave ~150 MB off the image; app then refuses .docx
# uploads via the existing text_extract.py "return None on unsupported"
# path (no code change needed).
ARG DROP_DOCX_SUPPORT=0
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        poppler-utils \
        ca-certificates \
        curl \
    && ( [ "${DROP_DOCX_SUPPORT}" = "1" ] || apt-get install -y --no-install-recommends pandoc ) \
    && rm -rf /var/lib/apt/lists/* \
    && rm -rf /root/.cache

# Non-root user. The uploads dir needs to be writable by this user, so we
# chown /data at container start; see the entrypoint.
RUN groupadd -r observer && useradd -r -g observer -d /app -m observer

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=observer:observer . /app

# Persistent state lives here. Mount a host volume in production so DB +
# uploads survive container restart / rebuild.
ENV OBSERVER_DB=/data/observations.sqlite \
    OBSERVER_UPLOADS=/data/uploads \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The /data mount is chowned + created by the entrypoint (host mounts land
# as root); doing it inline here would be pointless (VOLUME wipes it).
VOLUME ["/data"]

# Health check the reverse proxy / orchestrator can probe. Uses curl (already
# installed) and hits /health, which is anon-allowlisted and returns 200 with
# a DB ping. 30 s interval, 5 s timeout, 3 retries before unhealthy.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health || exit 1

EXPOSE 8000

# Entrypoint prepares /data (chown + mkdir) then launches uvicorn as the
# observer user. Kept inline so a `docker run` without compose works.
COPY --chmod=755 <<'EOF' /entrypoint.sh
#!/bin/sh
set -e

# Ensure the persistent state dir is writable by the non-root user.
mkdir -p /data
chown -R observer:observer /data

# Drop privileges and exec so signals reach uvicorn cleanly.
exec runuser -u observer -- "$@"
EOF

ENTRYPOINT ["/entrypoint.sh"]

# Default command; docker-compose or `docker run` overrides for workers,
# migrations, one-shot ceremonies (e.g. onboard_district.py), etc.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
