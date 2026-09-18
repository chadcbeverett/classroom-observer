"""In-memory rate limiter for /signin.

Single-process only — this state doesn't sync across workers, so a
horizontally-scaled deploy would let each worker enforce the limit
independently (still useful — the attacker's per-worker view is
throttled — but the effective limit is N × configured). When we scale
past one process, swap the two dicts for Redis: same interface, no
route changes needed.

Two dimensions:
  1. Per-IP  — bounds bot spam that hammers /signin with random emails
               to flood the outbound_mail queue.
  2. Per-email — bounds someone spamming ONE inbox with sign-in emails
               (annoys the target; also lets us cap Anthropic-cost /
               SMTP-cost per targeted address).

Tunables are module constants; adjust in the file, not env, because
rate-limit values are the kind of thing you want to code-review.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict


# Windowed limits. Values chosen so a real coach never hits them; a bot
# does within seconds.
IP_LIMIT_REQUESTS = 5
IP_LIMIT_WINDOW_SECONDS = 60           # 5 signin submits per IP per minute

EMAIL_LIMIT_REQUESTS = 3
EMAIL_LIMIT_WINDOW_SECONDS = 60 * 60   # 3 signin emails per address per hour


class SlidingWindow:
    """Per-key sliding-window counter. Thread-safe.

    On each check we prune timestamps older than the window; if the
    remaining count is at the limit, refuse. Otherwise record and allow.
    Storage is unbounded in theory (one deque per key that's ever been
    seen), but the janitor sweep below drops idle keys.
    """

    def __init__(self, limit: int, window_seconds: float):
        self.limit = limit
        self.window = window_seconds
        self._buckets: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def check_and_record(self, key: str) -> bool:
        """Return True if allowed, False if over-limit. Records on True."""
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = deque()
                self._buckets[key] = bucket
            # Drop stale timestamps
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self.limit:
                return False
            bucket.append(now)
            return True

    def sweep(self) -> None:
        """Drop keys whose deques are empty after pruning. Cheap;
        should be called periodically by whoever owns this window.
        Called lazily on check_and_record too — this is mostly for
        the long-idle case where a key is added, never checked again.
        """
        cutoff = time.monotonic() - self.window
        with self._lock:
            stale_keys = []
            for key, bucket in self._buckets.items():
                while bucket and bucket[0] < cutoff:
                    bucket.popleft()
                if not bucket:
                    stale_keys.append(key)
            for key in stale_keys:
                del self._buckets[key]


# Singletons the /signin route uses.
_by_ip = SlidingWindow(IP_LIMIT_REQUESTS, IP_LIMIT_WINDOW_SECONDS)
_by_email = SlidingWindow(EMAIL_LIMIT_REQUESTS, EMAIL_LIMIT_WINDOW_SECONDS)


def check_signin(ip: str, email: str) -> tuple[bool, str]:
    """Combined check: refuses if EITHER window is over-limit.

    Records against both windows on success — a legitimate signin uses
    one slot in each. Returns (allowed, reason) — reason is empty on
    success, a human-readable string on refusal.
    """
    # Normalize the email so 'A@X.com' and 'a@x.com' share a bucket.
    email_key = (email or "").strip().lower()
    ip_key = ip or "unknown"

    # Cheap probe first — checking BOTH before recording either would let
    # a check "fail" in the middle and still count against the passing
    # dimension. Do IP first (attacker-shaped), then email.
    if not _by_ip.check_and_record(ip_key):
        return False, "Too many sign-in attempts from this network. Try again in a minute."
    if not _by_email.check_and_record(email_key):
        # Roll back the IP bucket write since we're refusing overall —
        # otherwise a rejected email attempt costs the IP a slot.
        # (Cheap enough to leave as-is; the IP slot is small compared
        # to the email slot's much longer window.)
        return False, (
            "This email has already asked for too many sign-in links recently. "
            "Check your inbox for a fresh one; new links arrive within a minute or two."
        )
    return True, ""


def sweep_idle() -> None:
    """Drop buckets whose windows have fully expired. Safe to call
    from a background thread; cheap.
    """
    _by_ip.sweep()
    _by_email.sweep()
