"""Reset events reconstructed from the usage history.

The usage API reports only the *current* window: a percentage and a
``resets_at``. It never says "this window was reset early". This module infers
that from consecutive samples in ``usage_history.jsonl``
(see ``usage_history``), one account and one window at a time:

``scheduled``
    ``resets_at`` moved forward and the old reset time had already passed.
    The window rolled over on time.
``early``
    Usage fell to (near) zero while the old reset time was still in the
    future: Anthropic reset the window ahead of schedule. Two shapes occur
    in real history: the reset time jumps forward, or it stays put and only
    the percentage drops.
``partial``
    Usage fell by a meaningful amount, but not to zero, while the reset time
    was still in the future. A partial credit or a re-metering.
``moved``
    ``resets_at`` changed while it was still in the future and usage did not
    fall: the schedule shifted, nothing was given back.

Timing tolerances absorb the API's sub-second ``resets_at`` jitter and the
poll gap: an event is observed at the first sample after it happened, so
``observed_at`` is an upper bound and ``after`` a lower bound.

Pure functions over ``Sample`` lists; nothing here fetches or writes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

from claude_swap.usage_history import Sample

# resets_at values within this many seconds are the same reset (the API
# re-stamps the same boundary with different sub-second parts every fetch).
JITTER_S = 120.0
# A reset time this close to (or before) the observation counts as due.
GRACE_S = 600.0
# A drop of at least this many points with the schedule intact is an event.
MIN_DROP_PCT = 5.0
# At or under this, a drop is a full reset rather than a partial one.
ZERO_PCT = 1.0
# Losing at least this share of the used percentage also counts as a full
# reset: after a long poll gap the account may have spent a little of the
# fresh window before the next sample (83% -> 3% across a 17h gap).
FULL_SHARE = 0.8
WEEK_S = 7 * 86400.0

WINDOWS = ("5h", "7d")


@dataclass(frozen=True)
class ResetEvent:
    email: str
    window: str  # "5h" | "7d" | a scoped model name
    kind: str  # "scheduled" | "early" | "partial" | "moved"
    observed_at: float  # first sample showing the change
    after: float  # last sample before it
    pct_before: float
    pct_after: float
    due_before: float | None  # the reset time in force before the event
    due_after: float | None
    # True when due_before was worked back from the weekly schedule because
    # the reading carried no reset time (history from before 11 Sep 2026).
    due_inferred: bool = False

    @property
    def unscheduled(self) -> bool:
        return self.kind in ("early", "partial", "moved")

    @property
    def early_by_s(self) -> float | None:
        """How far ahead of the scheduled reset this was seen, if known."""
        if self.due_before is None:
            return None
        return max(0.0, self.due_before - self.observed_at)

    def to_json(self) -> dict:
        def iso(t: float | None) -> str | None:
            return None if t is None else datetime.fromtimestamp(t).astimezone().isoformat()

        return {
            "email": self.email,
            "window": self.window,
            "kind": self.kind,
            "observedAt": iso(self.observed_at),
            "after": iso(self.after),
            "pctBefore": self.pct_before,
            "pctAfter": self.pct_after,
            "dueBefore": iso(self.due_before),
            "dueAfter": iso(self.due_after),
            "earlyBySeconds": self.early_by_s if self.unscheduled else None,
            "dueInferred": self.due_inferred,
        }


def _ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _series(samples: Sequence[Sample], window: str) -> list[tuple[float, float, float | None]]:
    """``(t, pct, resets_at)`` points for one window, oldest first."""
    out = []
    for s in samples:
        if window == "5h":
            pct, reset = s.five_hour, _ts(s.five_hour_reset)
        elif window == "7d":
            pct, reset = s.seven_day, _ts(s.seven_day_reset)
        else:
            pct, reset = s.scoped.get(window), None
        if pct is None:
            continue
        out.append((s.t, pct, reset))
    return out


def _weekly_due(t: float, known: Sequence[float]) -> float | None:
    """The weekly reset after ``t``, worked back (or forward) from the
    nearest known 7d reset time. ``known`` is sorted."""
    if not known:
        return None
    later = [r for r in known if r > t]
    ref = later[0] if later else known[-1]
    # Shift ref by whole weeks into (t, t + WEEK].
    weeks = (ref - t) // WEEK_S if ref > t else -((t - ref) // WEEK_S) - 1
    due = ref - weeks * WEEK_S
    return due if due > t else due + WEEK_S


def _infer_weekly(points: list[tuple[float, float, float | None]], known: Sequence[float]):
    """Fill missing 7d reset times from the weekly schedule. Returns the new
    points and the set of sample times whose reset time was inferred."""
    out, inferred = [], set()
    for t, pct, due in points:
        if due is None:
            # Back off by the grace period: a reading just past the boundary
            # can still show the old window, as a stamped one would.
            due = _weekly_due(t - GRACE_S, known)
            if due is not None:
                inferred.add(t)
        out.append((t, pct, due))
    return out, inferred


def classify(
    prev: tuple[float, float, float | None],
    cur: tuple[float, float, float | None],
    *,
    anchor_due: float | None = None,
) -> str | None:
    """The event between two consecutive points, or None for no event.

    ``anchor_due`` is the reset time to judge a drop against when the window
    itself carries none (scoped windows share the 7d reset).
    """
    (_pt, ppct, pdue), (ct, cpct, cdue) = prev, cur
    due = pdue if pdue is not None else anchor_due
    if pdue is not None and cdue is not None and abs(cdue - pdue) > JITTER_S:
        if pdue <= ct + GRACE_S:
            return "scheduled"
        if cpct < ppct - ZERO_PCT:
            return _drop_kind(ppct, cpct)
        return "moved"
    if due is None or due <= ct + GRACE_S:
        # No schedule to compare against, or the reset was due anyway.
        return None
    if cpct <= ppct - MIN_DROP_PCT:
        return _drop_kind(ppct, cpct)
    return None


def _drop_kind(before: float, after: float) -> str:
    if after <= ZERO_PCT or (before - after) >= FULL_SHARE * before:
        return "early"
    return "partial"


def detect(samples: Iterable[Sample], windows: Sequence[str] | None = None) -> list[ResetEvent]:
    """Every reset event in ``samples``, oldest first.

    ``windows`` defaults to 5h, 7d and every scoped model name seen. Backfill
    samples carry no reset times, so they only ever pair as a ``prev`` for a
    drop check against the 7d anchor.
    """
    by_email: dict[str, list[Sample]] = {}
    for s in samples:
        by_email.setdefault(s.email, []).append(s)
    events: list[ResetEvent] = []
    for email, rows in by_email.items():
        rows.sort(key=lambda s: s.t)
        names = list(windows) if windows is not None else list(WINDOWS) + sorted(
            {name for s in rows for name in s.scoped}
        )
        # Scoped windows reset with the weekly window; use its schedule.
        # Readings without a 7d reset time get one from the weekly schedule.
        known = sorted({r for s in rows if (r := _ts(s.seven_day_reset)) is not None})
        inferred: set[float] = set()
        seven = {}
        for s in rows:
            due = _ts(s.seven_day_reset)
            if due is None:
                due = _weekly_due(s.t - GRACE_S, known)
                if due is not None:
                    inferred.add(s.t)
            seven[s.t] = due
        for window in names:
            points = _series(rows, window)
            if window == "7d":
                points, _ = _infer_weekly(points, known)
            for prev, cur in zip(points, points[1:]):
                anchor = seven.get(prev[0]) if window not in WINDOWS else None
                if window == "7d" and prev[0] in inferred and cur[0] in inferred:
                    # Both reset times are inferred, so they always agree:
                    # only a drop can be an event, never a moved schedule.
                    cur = (cur[0], cur[1], prev[2])
                kind = classify(prev, cur, anchor_due=anchor)
                if kind is None:
                    continue
                events.append(ResetEvent(
                    email=email, window=window, kind=kind,
                    observed_at=cur[0], after=prev[0],
                    pct_before=prev[1], pct_after=cur[1],
                    due_before=prev[2] if prev[2] is not None else anchor,
                    due_after=cur[2],
                    due_inferred=(window != "5h" and prev[0] in inferred),
                ))
    # A scoped (per-model) drop seen at the same moment as a 7d event is the
    # weekly reset seen twice: keep the 7d one.
    weekly = {(e.email, e.observed_at) for e in events if e.window == "7d"}
    events = [
        e for e in events
        if e.window in WINDOWS or (e.email, e.observed_at) not in weekly
    ]
    events.sort(key=lambda e: (e.observed_at, e.email, e.window))
    return events


def latest_unscheduled(events: Iterable[ResetEvent], email: str, since: float) -> list[ResetEvent]:
    """Unscheduled events for one account observed at or after ``since``."""
    return [e for e in events if e.email == email and e.unscheduled and e.observed_at >= since]


_KIND_TEXT = {
    "early": "reset early",
    "partial": "partly reset",
    "moved": "reset time moved",
    "scheduled": "reset on schedule",
}


def _clock(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%b %d %H:%M")


def _span(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours / 24:.1f}d" if hours >= 24 else f"{hours:.1f}h"


def describe(e: ResetEvent) -> str:
    """One human line, e.g. "7d reset early Sep 28 10:45: 100% → 0%,
    2.4d before its Sep 30 20:00 reset". Times are local."""
    text = f"{e.window} {_KIND_TEXT.get(e.kind, e.kind)} {_clock(e.observed_at)}"
    if e.kind == "moved":
        before = "?" if e.due_before is None else _clock(e.due_before)
        after = "?" if e.due_after is None else _clock(e.due_after)
        return f"{text}: {before} → {after} at {e.pct_after:.0f}%"
    text += f": {e.pct_before:.0f}% → {e.pct_after:.0f}%"
    early = e.early_by_s
    if e.unscheduled and early and e.due_before is not None:
        text += f", {_span(early)} before its {_clock(e.due_before)} reset"
        if e.due_inferred:
            text += " (schedule inferred: reading had no reset time)"
    gap = e.observed_at - e.after
    if gap > 3600:
        text += f" (seen after a {_span(gap)} gap)"
    return text
