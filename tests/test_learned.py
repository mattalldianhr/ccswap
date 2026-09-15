"""Tests for the learned weekly reserve."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from claude_swap.cycles import Cycle
from claude_swap.learned import (
    MIN_CYCLES,
    job_cost_in_cycle,
    learn_reserve,
    own_demand,
    percentile,
)

WEEK = 7 * 86400.0
T0 = datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()


def _cycle(peak: float, *, index: int = 0, coverage: float = 0.9) -> Cycle:
    start = T0 + index * WEEK
    return Cycle(email="a@x", window="7d", start=start, end=start + WEEK,
                 peak_pct=peak, final_pct=peak, samples=200, coverage=coverage)


def _run(cost_7d: float | None, at: float):
    stamp = datetime.fromtimestamp(at, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SimpleNamespace(cost_7d=cost_7d, finished_at=stamp)


class TestPercentile:
    def test_interpolates(self):
        assert percentile([0, 10], 0.5) == 5.0
        assert percentile([0, 100], 0.8) == 80.0

    def test_edges_and_single(self):
        vals = [10, 20, 30, 40]
        assert percentile(vals, 0.0) == 10 and percentile(vals, 1.0) == 40
        assert percentile([42], 0.8) == 42

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            percentile([], 0.5)


class TestOwnDemand:
    def test_subtracts_job_cost(self):
        assert own_demand(_cycle(90), job_cost_pct=12.0) == 78.0

    def test_never_negative_and_ignores_nonsense(self):
        assert own_demand(_cycle(5), job_cost_pct=40.0) == 0.0
        assert own_demand(_cycle(90), job_cost_pct=-5.0) == 90.0

    def test_job_cost_only_counts_runs_inside_the_cycle(self):
        cycle = _cycle(90)
        jobs = [SimpleNamespace(runs=[
            _run(4.0, cycle.start + 3600),        # inside
            _run(3.0, cycle.end - 60),            # inside
            _run(9.0, cycle.end + 3600),          # next cycle
            _run(7.0, cycle.start - 3600),        # previous cycle
            _run(None, cycle.start + 7200),       # unmeasured
        ])]
        assert job_cost_in_cycle(jobs, cycle) == 7.0

    def test_unparseable_timestamps_ignored(self):
        cycle = _cycle(90)
        jobs = [SimpleNamespace(runs=[SimpleNamespace(cost_7d=5.0, finished_at="not a date")])]
        assert job_cost_in_cycle(jobs, cycle) == 0.0

    def test_no_jobs(self):
        assert job_cost_in_cycle([], _cycle(90)) == 0.0


class TestLearnReserve:
    def _cycles(self, peaks: list[float]) -> list[Cycle]:
        return [_cycle(p, index=i) for i, p in enumerate(peaks)]

    def test_floor_stands_until_enough_cycles(self):
        result = learn_reserve(self._cycles([80] * (MIN_CYCLES - 1)), 10.0)
        assert result.pct == 10.0 and result.learned is False
        assert "need" in result.reason
        assert result.describe().endswith("cycles, have 7)")

    def test_partial_cycles_do_not_count(self):
        cycles = [_cycle(80, index=i, coverage=0.1) for i in range(MIN_CYCLES + 4)]
        assert learn_reserve(cycles, 10.0).learned is False

    def test_light_user_gets_the_full_floor_not_more(self):
        """Learning may only loosen: a light week cannot raise the reserve."""
        result = learn_reserve(self._cycles([40] * MIN_CYCLES), 10.0)
        assert result.learned is True
        assert result.pct == 10.0          # candidate was 55, capped at the floor
        assert result.own_demand_pct == 40.0

    def test_heavy_user_gets_a_smaller_reserve(self):
        result = learn_reserve(self._cycles([97] * MIN_CYCLES), 10.0)
        assert result.pct == 0.0           # 100-97-5 → below zero, clamped
        assert result.learned is True

    def test_moderate_user_lands_between(self):
        result = learn_reserve(self._cycles([92] * MIN_CYCLES), 10.0)
        assert result.pct == pytest.approx(3.0)   # 100-92-5
        assert result.describe() == f"3% (learned from {MIN_CYCLES} cycles)"

    def test_a_single_heavy_week_still_pulls_the_reserve_up(self):
        """Running out is worse than wasting: one 98% week among quiet ones
        must not be smoothed away by the percentile."""
        peaks = [60] * (MIN_CYCLES - 1) + [98]
        result = learn_reserve(self._cycles(peaks), 10.0)
        assert result.own_demand_pct > 70.0      # blended with the max, not 60
        # With a tighter floor the difference is visible in the reserve itself:
        # the heavy week leaves less headroom to hand to jobs.
        heavy = learn_reserve(self._cycles(peaks), 40.0)
        quiet = learn_reserve(self._cycles([60] * MIN_CYCLES), 40.0)
        assert heavy.pct < quiet.pct

    def test_peak_weight_is_tunable(self):
        peaks = [60] * (MIN_CYCLES - 1) + [98]
        only_percentile = learn_reserve(self._cycles(peaks), 10.0, peak_weight=0.0)
        only_max = learn_reserve(self._cycles(peaks), 10.0, peak_weight=1.0)
        assert only_percentile.own_demand_pct == 60.0
        assert only_max.own_demand_pct == 98.0

    def test_job_spend_does_not_ratchet_the_reserve_down(self):
        """The self-confirming trap: jobs eat slack, reserve must not shrink."""
        cycles = self._cycles([95] * MIN_CYCLES)   # peaks inflated by job spend
        jobs = [SimpleNamespace(runs=[_run(15.0, c.start + 3600) for c in cycles])]
        with_jobs = learn_reserve(cycles, 10.0, jobs=jobs)
        without = learn_reserve(cycles, 10.0)
        # Own demand is 80, not 95 — so the reserve stays at the floor.
        assert with_jobs.own_demand_pct == 80.0
        assert with_jobs.pct == 10.0 and without.pct == 0.0

    def test_result_is_attributable(self):
        result = learn_reserve(self._cycles([92] * MIN_CYCLES), 10.0)
        assert result.cycles == MIN_CYCLES and result.floor_pct == 10.0
        assert "learned from" in result.describe()

    def test_no_cycles_at_all(self):
        result = learn_reserve([], 10.0)
        assert result.pct == 10.0 and result.cycles == 0 and result.learned is False
