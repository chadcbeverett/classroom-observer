"""Background SMTP sender for the outbound_mail queue.

Design (agreed with the coach):
  - Config from env: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER,
    SMTP_PASSWORD, SMTP_FROM, SMTP_STARTTLS (default '1').
  - Generic smtplib (stdlib) — works with Postmark, SendGrid, SES,
    Fastmail, Gmail app-passwords, or a self-hosted relay.
  - Silent queue on failure: a send that raises records failed_at +
    failure_reason on the outbound_mail row and moves on. /signin
    stays working; a stuck queue is visible via /dev/mail (or the
    server log — every failure logs at ERROR).
  - No automatic retry. A failed row stays failed. If a coach needs
    a resend, they hit /signin again — that mints a fresh token.
    Older failed rows are audit trail, not retry queue.
  - When SMTP_HOST is unset, this module's `start_if_configured`
    returns None and the sender never starts. Local dev keeps
    working through /dev/mail without SMTP configured.
  - Batch semantics: pull up to N rows per poll, open one SMTP
    connection for the whole batch, close it. Reduces connection
    churn on providers that count TLS handshakes.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

from pipeline.db import connect as db_connect


log = logging.getLogger("uvicorn.error")


# Tunables — sensible defaults; no reason to expose as env yet.
POLL_INTERVAL_SECONDS = 10.0     # trade off latency vs. wake-ups
BATCH_SIZE = 20                  # rows per poll cycle
SMTP_TIMEOUT_SECONDS = 30        # per-message timeout on the SMTP call


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SmtpSender:
    """Owns one background thread that drains outbound_mail via SMTP.

    Not thread-safe against multiple senders sharing one DB — the sender
    is a singleton per process. Reads env at construction, so a config
    change requires restarting the app (fine for pilot cadence).
    """

    def __init__(
        self,
        *,
        db_path: Path,
        host: str,
        port: int,
        user: Optional[str],
        password: Optional[str],
        from_addr: str,
        use_starttls: bool = True,
        poll_interval: float = POLL_INTERVAL_SECONDS,
    ):
        self.db_path = db_path
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.from_addr = from_addr
        self.use_starttls = use_starttls
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---- Lifecycle ----------------------------------------------------

    def start(self) -> None:
        """Idempotent — safe to call once from _bootstrap."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="smtp-sender", daemon=True,
        )
        self._thread.start()
        log.info("SMTP sender started (host=%s port=%d from=%s)",
                 self.host, self.port, self.from_addr)

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop to exit; wait up to `timeout` for it to drain."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        log.info("SMTP sender stopped")

    # ---- Loop ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                sent, failed = self._pump_batch()
                if sent or failed:
                    log.info("SMTP: sent=%d failed=%d", sent, failed)
            except Exception as e:
                # Never let the loop die on a transient DB or SMTP error —
                # a broken pump would silently leave the queue growing.
                log.exception("SMTP sender loop hit an unexpected error: %s", e)
            # Wait polls the stop event too — clean shutdown latency
            # is at most poll_interval seconds.
            self._stop.wait(self.poll_interval)

    def _pump_batch(self) -> tuple[int, int]:
        """Send up to BATCH_SIZE unsent rows. Returns (sent_count, failed_count).

        Opens the DB (a new connection is required per thread for SQLite)
        and reads the batch first, then opens one SMTP session for the
        whole batch. Marks rows one at a time so a mid-batch crash still
        records the earlier sends.
        """
        conn = db_connect(self.db_path)
        try:
            rows = conn.execute(
                """SELECT id, to_email, subject, body_text, body_html, related_token_id
                   FROM outbound_mail
                   WHERE sent_at IS NULL AND failed_at IS NULL
                   ORDER BY created_at ASC
                   LIMIT ?""",
                (BATCH_SIZE,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return (0, 0)

        sent = failed = 0
        server: Optional[smtplib.SMTP] = None
        try:
            server = self._open_smtp()
        except Exception as e:
            # Whole-batch connection failure. Record it on every row so
            # the /dev/mail viewer surfaces the problem and the queue
            # doesn't grow silently.
            log.error("SMTP connect to %s:%d failed: %s", self.host, self.port, e)
            reason = f"SMTP connect failed: {type(e).__name__}: {e}"
            conn = db_connect(self.db_path)
            try:
                for row in rows:
                    conn.execute(
                        "UPDATE outbound_mail SET failed_at = ?, failure_reason = ? WHERE id = ?",
                        (_now_iso(), reason[:2000], row["id"]),
                    )
                conn.commit()
            finally:
                conn.close()
            return (0, len(rows))

        try:
            for row in rows:
                conn = db_connect(self.db_path)
                try:
                    try:
                        msg = self._build_message(row)
                        server.send_message(msg)
                        conn.execute(
                            "UPDATE outbound_mail SET sent_at = ? WHERE id = ?",
                            (_now_iso(), row["id"]),
                        )
                        conn.commit()
                        sent += 1
                    except Exception as e:
                        log.error("SMTP send of %s to %s failed: %s",
                                  row["id"][:8], row["to_email"], e)
                        conn.execute(
                            "UPDATE outbound_mail SET failed_at = ?, failure_reason = ? WHERE id = ?",
                            (_now_iso(), f"{type(e).__name__}: {e}"[:2000], row["id"]),
                        )
                        conn.commit()
                        failed += 1
                finally:
                    conn.close()
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return (sent, failed)

    # ---- SMTP helpers -------------------------------------------------

    def _open_smtp(self) -> smtplib.SMTP:
        """Open a session. TLS-on-connect (port 465) or STARTTLS-upgrade
        (port 587) or plain (25/2525) — inferred from port + the
        use_starttls flag.
        """
        if self.port == 465:
            # Implicit TLS — the socket itself is TLS from the first byte.
            ctx = ssl.create_default_context()
            server = smtplib.SMTP_SSL(
                self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS, context=ctx,
            )
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_SECONDS)
            server.ehlo()
            if self.use_starttls:
                ctx = ssl.create_default_context()
                server.starttls(context=ctx)
                server.ehlo()
        if self.user and self.password:
            server.login(self.user, self.password)
        return server

    def _build_message(self, row) -> EmailMessage:
        """Assemble an RFC-5322 message. If body_html is present, send
        as multipart/alternative so clients that render HTML get the
        pretty version and text-only clients still see the body.
        """
        msg = EmailMessage()
        msg["From"] = self.from_addr
        msg["To"] = row["to_email"]
        msg["Subject"] = row["subject"] or "(no subject)"
        msg.set_content(row["body_text"] or "")
        if row["body_html"]:
            msg.add_alternative(row["body_html"], subtype="html")
        return msg


# ---------------------------------------------------------------------------
# App integration
# ---------------------------------------------------------------------------

_singleton: Optional[SmtpSender] = None


def start_if_configured(db_path: Path) -> Optional[SmtpSender]:
    """Called from app._bootstrap. Returns the started sender or None.

    Reads env at call time so a repeated startup (SIGHUP-triggered reload)
    picks up config changes. Returns None when SMTP_HOST is unset — the
    local-dev path where /dev/mail is the delivery channel.
    """
    global _singleton
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        log.info("SMTP not configured (SMTP_HOST unset) — outbound_mail stays queued; use /dev/mail in dev.")
        return None
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ.get("SMTP_USER") or None
    password = os.environ.get("SMTP_PASSWORD") or None
    from_addr = os.environ.get("SMTP_FROM") or "no-reply@classroom-observer.local"
    use_starttls = os.environ.get("SMTP_STARTTLS", "1").strip() != "0"
    if _singleton is not None:
        _singleton.stop()
    _singleton = SmtpSender(
        db_path=db_path,
        host=host, port=port, user=user, password=password,
        from_addr=from_addr, use_starttls=use_starttls,
    )
    _singleton.start()
    return _singleton


def stop_if_running() -> None:
    global _singleton
    if _singleton is not None:
        _singleton.stop()
        _singleton = None
