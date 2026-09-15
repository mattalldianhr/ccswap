"""A weekly reserve learned from how much of the budget the user needs.

The reserve exists to keep background jobs from eating capacity the user will
want. Guessing 10% was a placeholder; this derives it from measurement once
there is enough of it.

**Why it is not derived from unspent budget.** The obvious rule — "reserve
what you typically leave unused" — is self-confirming: jobs consume the slack,
unspent falls, the reserve shrinks, jobs consume more. The measurement would
be polluted by the thing it controls. So the input is the user's *own* peak
demand per cycle (the cycle's peak minus what jobs spent during it), which
jobs cannot inflate.

The rule:

    own_demand(cycle) = peak_pct − job cost charged to that cycle
    headroom          = 100 − percentile(own_demand, RESERVE_PERCENTILE)
    reserve           = clamp(headroom − SAFETY_MARGIN_PCT, 0, configured floor)

Three guardrails, because a wrong reserve is worse than a crude one:

- It only ever *loosens*. The configured floor is the user's ceiling on how
  much jobs may claim; learning can hold back more, never less.
- It needs ``MIN_CYCLES`` complete cycles before it engages at all.
- It is always attributed, so a surprising number can be traced ("6%, learned
  from 9 cycles") rather than appearing as an unexplained change.

Accounts share one value. ccswap exists to make accounts interchangeable, so
estimating per account would halve the data to measure a distinction the user
does not make.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from claude_swap.cycles import Cycle

# Below this many complete cycles, the configured floor stands. Two months of
# weekly data before a number starts steering anything.
MIN_CYCLES = 8
# A high percentile so a heavy week is accommodated, not averaged away: the
# reserve should cover most weeks, not the median one.
RESERVE_PERCENTILE = 0.8
# How much weight the single heaviest observed week carries against that
# percentile. The costs here are asymmetric: leaving budget unused is a small
# waste, running out because a rare heavy week was smoothed away is a real
# cost. With 8 quiet weeks and one heavy one, the 80th percentile sits near
# the quiet weeks and ignores the exception entirely.
PEAK_WEIGHT = 0.5
# Held back beyond the measured demand, for the week that exceeds every week
# seen so far.
SAFETY_MARGIN_PCT = 5.0


@dataclass(frozen=True)
class LearnedReserve:
    """A reserve derived from measurement, with its provenance."""

    pct: float
    cycles: int
    own_demand_pct: float | None  # the percentile of the user's own demand
    floor_pct: float  # the configured value this may not exceed
    reason: str

    @property
    def learned(self) -> bool:
        return self.cycles >= MIN_CYCLES and self.own_demand_pct is not None

    def describe(self) -> str:
        if not self.learned:
            return f"{self.pct:.0f}% ({self.reason})"
        return f"{self.pct:.0f}% (learned from {self.cycles} cycles)"


def percentile(values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile; ``fraction`` in 0..1."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def own_demand(cycle: Cycle, job_cost_pct: float = 0.0) -> float:
    """The user's own peak demand in a cycle, excluding job spend.

    Jobs cannot inflate this, which is what keeps the learned reserve from
    ratcheting itself down.
    """
    return max(0.0, cycle.peak_pct - max(0.0, job_cost_pct))


def job_cost_in_cycle(jobs: list, cycle: Cycle) -> float:
    """Total measured 7d cost of job runs that finished inside ``cycle``."""
    from datetime import datetime, timezone

    total = 0.0
    for job in jobs:
        for run in getattr(job, "runs", ()) or ():
            if run.cost_7d is None:
                continue
            try:
                when = (
                    datetime.strptime(run.finished_at, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc).timestamp()
                )
            except (ValueError, TypeError):
                continue
            if cycle.start <= when < cycle.end:
                total += run.cost_7d
    return total


def learn_reserve(
    cycles: list[Cycle],
    floor_pct: float,
    *,
    jobs: list | None = None,
    min_cycles: int = MIN_CYCLES,
    percentile_fraction: float = RESERVE_PERCENTILE,
    safety_margin_pct: float = SAFETY_MARGIN_PCT,
    peak_weight: float = PEAK_WEIGHT,
) -> LearnedReserve:
    """The weekly reserve to use, learned where possible.

    ``floor_pct`` is the configured reserve and acts as a ceiling on the
    result: learning may hold back *more* of the budget than configured, never
    less. That keeps a surprising measurement from quietly handing jobs more
    capacity than the user agreed to.
    """
    complete = [c for c in cycles if c.complete]
    if len(complete) < min_cycles:
        return LearnedReserve(
            pct=floor_pct, cycles=len(complete), own_demand_pct=None, floor_pct=floor_pct,
            reason=f"need {min_cycles} cycles, have {len(complete)}",
        )
    demands = [own_demand(c, job_cost_in_cycle(jobs or [], c)) for c in complete]
    # Blend the percentile with the worst week seen, so one heavy cycle among
    # quiet ones still pulls the reserve up.
    measured = (
        (1.0 - peak_weight) * percentile(demands, percentile_fraction)
        + peak_weight * max(demands)
    )
    headroom = max(0.0, 100.0 - measured)
    candidate = max(0.0, headroom - safety_margin_pct)
    # Only ever loosen: the configured floor is the user's hard limit.
    pct = min(candidate, floor_pct)
    return LearnedReserve(
        pct=pct, cycles=len(complete), own_demand_pct=measured, floor_pct=floor_pct,
        reason="learned",
    )
