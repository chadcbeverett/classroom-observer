"""How long each stage of a coaching cycle is taking.

The loop is: a lesson is recorded, the coach names a move, they rehearse it
together, the teacher teaches again. Each handoff can stall, and a stall is
invisible — nothing in the product says "this move was named eleven days ago
and never rehearsed." This measures the gaps so a coach can see their own
queue.

Two decisions shape everything here.

Gaps are counted in **school days**. A move published on Friday and rehearsed
on Monday took one day, not three. Counting calendar days would make every
coach look slower than they are and would make the weekend read as neglect.

This is a coach's own queue, not a leaderboard. `open_loops_for_coach` is
scoped to one coach by design. Aggregates across coaches exist in
`coach_medians`, but nothing calls it by default: a clock that becomes a
performance metric stops measuring the work and starts shaping it, and the
numbers are small enough during a pilot that any comparison would be noise.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Dict, List, Optional, Sequence

# Beyond this a gap is reported as "stale" rather than counted. Guards the
# day-by-day walk below against a bad timestamp producing a decade-long loop.
MAX_SPAN_DAYS = 400


def parse_ts(value) -> Optional[datetime]:
    """Parse a stored timestamp to an aware UTC datetime.

    The database holds both shapes: `uploaded_at` is written with an offset,
    `scored_at` without. Subtracting one from the other raises TypeError, so
    every value is normalised here and naive values are read as UTC — which is
    what the writers meant, since they all used UTC clocks.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s[:19])
            except ValueError:
                return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def school_days_between(start, end) -> Optional[int]:
    """Weekdays elapsed from `start` to `end`, excluding the start day.

    Friday to Monday is 1. Friday to Friday is 0. Returns None when either end
    is missing, and a negative result when the pair is out of order rather than
    silently clamping — out-of-order timestamps are a data problem worth seeing.
    """
    a, b = parse_ts(start), parse_ts(end)
    if a is None or b is None:
        return None
    sign = 1
    if b < a:
        a, b, sign = b, a, -1
    d0, d1 = a.date(), b.date()
    if (d1 - d0).days > MAX_SPAN_DAYS:
        return None
    count, cur = 0, d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            count += 1
    return sign * count


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Stage:
    """One handoff in the cycle."""
    key: str
    label: str
    at: Optional[datetime]          # when it happened, None if still pending
    school_days: Optional[int]      # since the previous stage
    pending: bool = False


@dataclass
class CycleTiming:
    observation_id: str
    teacher_id: str
    teacher_name: str
    coach_user_id: Optional[str]
    stages: List[Stage]

    @property
    def stalled_at(self) -> Optional[Stage]:
        """The first stage still waiting, if any."""
        for s in self.stages:
            if s.pending:
                return s
        return None

    @property
    def days_waiting(self) -> Optional[int]:
        s = self.stalled_at
        return s.school_days if s else None


def cycle_timing_for_observation(conn, observation_id: str) -> Optional[CycleTiming]:
    """Timings for one observation's cycle, including where it is stuck."""
    obs = conn.execute(
        """SELECT o.id, o.teacher_id, o.observed_at, o.uploaded_at, o.scored_at,
                  o.status, t.name AS teacher_name, t.assigned_coach_user_id
           FROM observations o JOIN teachers t ON t.id = o.teacher_id
           WHERE o.id = ? AND o.deleted_at IS NULL""",
        (observation_id,),
    ).fetchone()
    if not obs:
        return None

    # observed_at is optional — a coach uploading the same day may not set it.
    lesson_at = parse_ts(obs["observed_at"]) or parse_ts(obs["uploaded_at"])

    move = conn.execute(
        """SELECT id, published_at FROM published_coach_moves
           WHERE observation_id = ? AND superseded_at IS NULL
           ORDER BY published_at DESC LIMIT 1""",
        (observation_id,),
    ).fetchone()
    move_at = parse_ts(move["published_at"]) if move else None

    practice_at = None
    if move:
        row = conn.execute(
            """SELECT MIN(ps.held_at) AS first_held
               FROM practice_sessions ps
               JOIN practice_plans pp ON pp.id = ps.practice_plan_id
               WHERE pp.coaching_move_id = ?""",
            (move["id"],),
        ).fetchone()
        practice_at = parse_ts(row["first_held"]) if row else None

    # The next lesson closes the loop: the point of rehearsing is the lesson
    # that follows it.
    nxt = conn.execute(
        """SELECT COALESCE(observed_at, uploaded_at) AS at FROM observations
           WHERE teacher_id = ? AND deleted_at IS NULL AND id <> ?
             AND COALESCE(observed_at, uploaded_at) > ?
           ORDER BY COALESCE(observed_at, uploaded_at) ASC LIMIT 1""",
        (obs["teacher_id"], observation_id, obs["uploaded_at"]),
    ).fetchone()
    next_at = parse_ts(nxt["at"]) if nxt else None

    now = _now()
    points = [
        ("lesson", "Lesson recorded", lesson_at),
        ("move", "Move named", move_at),
        ("practice", "Rehearsed together", practice_at),
        ("next_lesson", "Back in the room", next_at),
    ]

    stages: List[Stage] = []
    prev = lesson_at
    for i, (key, label, at) in enumerate(points):
        if i == 0:
            stages.append(Stage(key, label, at, None))
            continue
        if at is not None:
            stages.append(Stage(key, label, at, school_days_between(prev, at)))
            prev = at
        else:
            # Pending: measure from the last thing that did happen, to now.
            stages.append(Stage(key, label, None, school_days_between(prev, now), pending=True))
            # Everything after an unreached stage is also unreached, but only
            # the first one is the thing the coach can act on, so the rest are
            # recorded without a running clock.
            for k2, l2, _ in points[i + 1:]:
                stages.append(Stage(k2, l2, None, None, pending=True))
            break

    return CycleTiming(
        observation_id=obs["id"],
        teacher_id=obs["teacher_id"],
        teacher_name=obs["teacher_name"],
        coach_user_id=obs["assigned_coach_user_id"],
        stages=stages,
    )


def open_loops_for_coach(conn, coach_user_id: str, *, limit: int = 25) -> List[CycleTiming]:
    """Cycles on this coach's caseload that have not closed, slowest first.

    Scoped to one coach deliberately. The question this answers is "what is
    waiting on me", not "how do I compare".
    """
    rows = conn.execute(
        """SELECT o.id FROM observations o
           JOIN teachers t ON t.id = o.teacher_id
           WHERE t.assigned_coach_user_id = ?
             AND o.deleted_at IS NULL AND t.archived_at IS NULL
             AND o.status = 'complete'
           ORDER BY COALESCE(o.observed_at, o.uploaded_at) DESC
           LIMIT ?""",
        (coach_user_id, limit * 2),
    ).fetchall()

    out: List[CycleTiming] = []
    for r in rows:
        t = cycle_timing_for_observation(conn, r["id"])
        if t and t.stalled_at is not None:
            out.append(t)
    out.sort(key=lambda t: (t.days_waiting is None, -(t.days_waiting or 0)))
    return out[:limit]


# Stages a coach can act on, and how long is too long before a nudge. Chosen to
# match a weekly coaching cadence: a move named one week should be rehearsed
# before the next, and the teacher should be back in the room the week after.
NUDGE_AFTER_SCHOOL_DAYS: Dict[str, int] = {
    "move": 3,
    "practice": 5,
    "next_lesson": 10,
}


def nudges_for_coach(conn, coach_user_id: str) -> List[dict]:
    """Loops past their expected turnaround, as items a dashboard can render."""
    out = []
    for t in open_loops_for_coach(conn, coach_user_id):
        stage = t.stalled_at
        if stage is None or stage.school_days is None:
            continue
        threshold = NUDGE_AFTER_SCHOOL_DAYS.get(stage.key)
        if threshold is None or stage.school_days < threshold:
            continue
        out.append({
            "observation_id": t.observation_id,
            "teacher_id": t.teacher_id,
            "teacher_name": t.teacher_name,
            "stage": stage.key,
            "stage_label": stage.label,
            "school_days": stage.school_days,
            "threshold": threshold,
        })
    return out


def coach_medians(conn, org_id: str, *, min_cycles: int = 5) -> Optional[Dict[str, dict]]:
    """Median interval per coach, for a leader view.

    Returns None when any coach has fewer than `min_cycles` closed cycles. A
    median over two observations is not a measurement, and publishing one
    invites a comparison the data cannot support. Nothing calls this by
    default; it exists so the leader view can be switched on once the volume
    justifies it.
    """
    rows = conn.execute(
        """SELECT o.id, t.assigned_coach_user_id AS coach
           FROM observations o JOIN teachers t ON t.id = o.teacher_id
           WHERE t.org_id = ? AND o.deleted_at IS NULL AND o.status = 'complete'
             AND t.assigned_coach_user_id IS NOT NULL""",
        (org_id,),
    ).fetchall()

    by_coach: Dict[str, List[CycleTiming]] = {}
    for r in rows:
        t = cycle_timing_for_observation(conn, r["id"])
        if t:
            by_coach.setdefault(r["coach"], []).append(t)

    if not by_coach or any(len(v) < min_cycles for v in by_coach.values()):
        return None

    out: Dict[str, dict] = {}
    for coach, timings in by_coach.items():
        per_stage: Dict[str, List[int]] = {}
        for t in timings:
            for s in t.stages:
                if not s.pending and s.school_days is not None:
                    per_stage.setdefault(s.key, []).append(s.school_days)
        out[coach] = {k: median(v) for k, v in per_stage.items() if v}
    return out
