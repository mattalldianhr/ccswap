"""Completed usage cycles, recorded per account so the scheduler can learn.

``usage_history.jsonl`` is a stream of samples with no notion of a cycle, so
every analysis has to reconstruct boundaries itself — and getting that wrong
is easy. On 2026-09-15 an analysis folded two accounts whose weekly windows
reset 16 hours apart onto one shared "fraction elapsed" axis and concluded
Matt front-loads his week; the dip it saw was one account resetting while the
other was mid-cycle. The conclusion was an artifact of the aggregation.

This module records each *completed* cycle once, keyed by the account and the
cycle's own end time, so later analysis compares like with like:

    {"email": ..., "window": "7d", "start": ..., "end": ...,
     "peak_pct": 83.0, "final_pct": 83.0, "unspent_pct": 17.0,
     "samples": 2060, "coverage": 0.9,
     "trajectory": [[0.0, 0.0], [0.1, 22.0], ...]}

``trajectory`` is the used-percentage at ten points through the cycle, on the
cycle's *own* clock. ``coverage`` is the fraction of the cycle that actually
had samples — a cycle observed for two of seven days should not be read as a
week of light use, and ``complete_cycles`` filters on it.

Nothing here feeds the live scheduler yet. It exists so that when there are
enough cycles to see a shape, the shape comes from measurement.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from claude_swap.locking import FileLock
from claude_swap.pace import WEEKLY_PERIOD_S
from claude_swap.usage_history import Sample, UsageHistory

CYCLES_FILENAME = "usage_cycles.jsonl"
# Below this fraction observed, a cycle says more about our sampling than
# about the user, so it is not "complete" for analysis.
MIN_COVERAGE = 0.6
TRAJECTORY_POINTS = 11  # 0%, 10%, … 100% of the cycle


@dataclass(frozen=True)
class Cycle:
    email: str
    window: str
    start: float
    end: float
    peak_pct: float
    final_pct: float
    samples: int
    coverage: float
    trajectory: list[list[float]] = field(default_factory=list)

    @property
    def unspent_pct(self) -> float:
        """Budget left on the table when the window reset."""
        return max(0.0, 100.0 - self.peak_pct)

    @property
    def complete(self) -> bool:
        return self.coverage >= MIN_COVERAGE

    def to_json(self) -> dict:
        data = asdict(self)
        data["unspent_pct"] = self.unspent_pct
        return data

    @classmethod
    def from_json(cls, raw: dict) -> "Cycle | None":
        try:
            return cls(
                email=str(raw["email"]),
                window=str(raw["window"]),
                start=float(raw["start"]),
                end=float(raw["end"]),
                peak_pct=float(raw["peak_pct"]),
                final_pct=float(raw["final_pct"]),
                samples=int(raw.get("samples") or 0),
                coverage=float(raw.get("coverage") or 0.0),
                trajectory=[list(p) for p in (raw.get("trajectory") or [])],
            )
        except (KeyError, TypeError, ValueError):
            return None


def _window_value(sample: Sample, window: str) -> float | None:
    if window == "5h":
        return sample.five_hour
    if window == "7d":
        return sample.seven_day
    for name, pct in sample.scoped.items():
        if name.lower() == window.lower():
            return pct
    return None


def _reset_ts(value: str | None) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def cycle_end_for(t: float, anchor: float, period_s: float) -> float:
    """The end of the cycle containing ``t``, given any known reset time.

    Resets run on a fixed cadence, so one observed reset locates every other
    one, forwards and backwards. Each account has its own anchor — that is
    the whole point of keying cycles per account.

    A sample landing exactly on a reset belongs to the cycle that is
    *starting*, not the one that just ended: at the reset instant the window
    reads 0, which is the new cycle's first observation.
    """
    steps = (t - anchor) / period_s
    whole = math.ceil(steps)
    if math.isclose(steps, whole, rel_tol=0.0, abs_tol=1e-9):
        whole += 1  # exactly on a boundary → the cycle now beginning
    return anchor + whole * period_s


def _trajectory(points: list[tuple[float, float]], start: float, period_s: float) -> list[list[float]]:
    """Used-pct sampled at even fractions of the cycle, on its own clock."""
    if not points:
        return []
    out: list[list[float]] = []
    for i in range(TRAJECTORY_POINTS):
        frac = i / (TRAJECTORY_POINTS - 1)
        target = start + frac * period_s
        # last observation at or before this mark
        seen = [v for t, v in points if t <= target]
        if seen:
            out.append([round(frac, 2), float(seen[-1])])
    return out


def build_cycles(
    samples: list[Sample],
    *,
    window: str = "7d",
    period_s: float = WEEKLY_PERIOD_S,
    now: float | None = None,
) -> list[Cycle]:
    """Group one account's samples into completed cycles.

    Only cycles that have *ended* are returned: an in-progress cycle has no
    final peak. ``samples`` must be for a single account.
    """
    points: list[tuple[float, float]] = []
    anchor: float | None = None
    email = ""
    for s in samples:
        value = _window_value(s, window)
        if value is None:
            continue
        email = email or s.email
        points.append((s.t, value))
        if anchor is None:
            reset = s.seven_day_reset if window == "7d" else s.five_hour_reset
            anchor = _reset_ts(reset)
    if not points:
        return []
    if anchor is None:
        # No reset stamp anywhere: infer boundaries from drops in usage.
        anchor = _infer_anchor(points, period_s)
        if anchor is None:
            return []
    points.sort()
    now = now if now is not None else points[-1][0]

    grouped: dict[float, list[tuple[float, float]]] = {}
    for t, value in points:
        grouped.setdefault(round(cycle_end_for(t, anchor, period_s), 3), []).append((t, value))

    cycles: list[Cycle] = []
    for end, group in sorted(grouped.items()):
        if end > now:
            continue  # still in progress
        start = end - period_s
        observed = max(t for t, _ in group) - min(t for t, _ in group)
        cycles.append(Cycle(
            email=email,
            window=window,
            start=start,
            end=end,
            peak_pct=max(v for _, v in group),
            final_pct=group[-1][1],
            samples=len(group),
            coverage=min(1.0, observed / period_s),
            trajectory=_trajectory(group, start, period_s),
        ))
    return cycles


def _infer_anchor(points: list[tuple[float, float]], period_s: float) -> float | None:
    """Locate a reset from a drop in usage, when no stamp is available."""
    points = sorted(points)
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        if v1 < v0 - 5:  # a real drop, not noise
            return t1
    return None


class CycleStore:
    """Appends completed cycles to ``<cache_dir>/usage_cycles.jsonl``."""

    def __init__(self, cache_dir: Path):
        self.path = Path(cache_dir) / CYCLES_FILENAME
        self._lock_path = Path(cache_dir) / ".usage_cycles.lock"

    def all(self) -> list[Cycle]:
        out: list[Cycle] = []
        try:
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        continue
                    cycle = Cycle.from_json(raw)
                    if cycle is not None:
                        out.append(cycle)
        except OSError:
            return []
        return out

    def known_keys(self) -> set[tuple[str, str, float]]:
        return {(c.email, c.window, round(c.end, 3)) for c in self.all()}

    def record(self, cycles: list[Cycle]) -> int:
        """Append cycles not already recorded. Returns how many were added."""
        if not cycles:
            return 0
        written = 0
        try:
            with FileLock(self._lock_path):
                known = self.known_keys()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as fh:
                    for cycle in sorted(cycles, key=lambda c: c.end):
                        key = (cycle.email, cycle.window, round(cycle.end, 3))
                        if key in known:
                            continue
                        fh.write(json.dumps(cycle.to_json(), separators=(",", ":")) + "\n")
                        known.add(key)
                        written += 1
        except Exception:  # noqa: BLE001 - recording is best-effort
            return written
        return written

    def complete_cycles(self, *, email: str | None = None, window: str = "7d") -> list[Cycle]:
        return [
            c for c in self.all()
            if c.complete and c.window == window and (email is None or c.email == email)
        ]


def update_from_history(
    history: UsageHistory,
    store: CycleStore,
    emails: list[str],
    *,
    window: str = "7d",
    period_s: float = WEEKLY_PERIOD_S,
    now: float | None = None,
) -> int:
    """Record any newly-completed cycles for each account. Returns the count."""
    total = 0
    for email in emails:
        samples = history.samples(email)
        if not samples:
            continue
        total += store.record(
            build_cycles(samples, window=window, period_s=period_s, now=now)
        )
    return total
