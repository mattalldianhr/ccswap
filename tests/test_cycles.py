"""Tests for per-account cycle recording."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from claude_swap.cycles import (
    MIN_COVERAGE,
    Cycle,
    CycleStore,
    build_cycles,
    cycle_end_for,
    update_from_history,
)
from claude_swap.usage_history import Sample, UsageHistory

WEEK = 7 * 86400.0
ANCHOR = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _s(t: float, seven: float, *, email="a@x", reset: float | None = ANCHOR) -> Sample:
    return Sample(
        t=t, email=email, org="", five_hour=None, five_hour_reset=None,
        seven_day=seven, seven_day_reset=_iso(reset) if reset else None,
    )


class TestCycleBoundaries:
    def test_cycle_end_projects_both_directions(self):
        """One known reset locates every other one on the fixed cadence."""
        assert cycle_end_for(ANCHOR - 1, ANCHOR, WEEK) == ANCHOR
        assert cycle_end_for(ANCHOR - WEEK - 1, ANCHOR, WEEK) == ANCHOR - WEEK
        assert cycle_end_for(ANCHOR + 1, ANCHOR, WEEK) == ANCHOR + WEEK

    def test_accounts_keep_their_own_anchors(self):
        """The 16h offset between Matt's accounts must not be averaged away."""
        other = ANCHOR + 16 * 3600
        t = ANCHOR - 3600
        assert cycle_end_for(t, ANCHOR, WEEK) == ANCHOR
        assert cycle_end_for(t, other, WEEK) == other


class TestBuildCycles:
    def _week(self, end: float, peaks: list[float], **kw) -> list[Sample]:
        start = end - WEEK
        step = WEEK / len(peaks)
        return [_s(start + i * step, v, **kw) for i, v in enumerate(peaks)]

    def test_completed_cycle_captured(self):
        samples = self._week(ANCHOR, [0, 20, 45, 70, 83, 83, 83, 83])
        cycles = build_cycles(samples, now=ANCHOR + 10)
        assert len(cycles) == 1
        c = cycles[0]
        assert c.peak_pct == 83.0 and c.final_pct == 83.0
        assert c.unspent_pct == 17.0
        assert c.end == ANCHOR and c.start == ANCHOR - WEEK
        assert c.complete is True

    def test_in_progress_cycle_excluded(self):
        samples = self._week(ANCHOR, [0, 20, 40]) + [_s(ANCHOR + 3600, 5.0)]
        cycles = build_cycles(samples, now=ANCHOR + 7200)
        assert [c.end for c in cycles] == [ANCHOR]

    def test_two_cycles_kept_separate(self):
        samples = self._week(ANCHOR - WEEK, [0, 50, 100]) + self._week(ANCHOR, [0, 40, 83])
        cycles = build_cycles(samples, now=ANCHOR + 10)
        assert [c.peak_pct for c in cycles] == [100.0, 83.0]

    def test_sparse_cycle_is_not_complete(self):
        start = ANCHOR - WEEK
        samples = [_s(start + i * 600, float(i)) for i in range(5)]  # 40 min of a week
        c = build_cycles(samples, now=ANCHOR + 10)[0]
        assert c.coverage < MIN_COVERAGE and c.complete is False

    def test_trajectory_is_on_the_cycles_own_clock(self):
        samples = self._week(ANCHOR, [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        c = build_cycles(samples, now=ANCHOR + 10)[0]
        fracs = [p[0] for p in c.trajectory]
        assert fracs[0] == 0.0 and fracs[-1] == 1.0
        assert all(0.0 <= f <= 1.0 for f in fracs)
        assert c.trajectory[-1][1] == 100.0

    def test_anchor_inferred_from_a_drop_when_no_stamp(self):
        start = ANCHOR - WEEK
        samples = ([_s(start + i * 3600, float(i * 10), reset=None) for i in range(8)]
                   + [_s(start + 8 * 3600, 2.0, reset=None)])
        cycles = build_cycles(samples, now=ANCHOR + WEEK)
        assert cycles and cycles[0].peak_pct == 70.0

    def test_no_usable_samples(self):
        assert build_cycles([]) == []
        assert build_cycles([_s(ANCHOR, 50.0)], window="Fable") == []


class TestCycleStore:
    def test_record_and_dedupe(self, tmp_path):
        store = CycleStore(tmp_path)
        cycles = build_cycles([_s(ANCHOR - WEEK + i * 3600, float(i)) for i in range(100)],
                              now=ANCHOR + 10)
        assert store.record(cycles) == 1
        assert store.record(cycles) == 0          # same cycle, not appended twice
        assert len(store.all()) == 1

    def test_accounts_do_not_collide(self, tmp_path):
        store = CycleStore(tmp_path)
        a = build_cycles([_s(ANCHOR - WEEK + i * 3600, float(i), email="a@x") for i in range(100)],
                         now=ANCHOR + 10)
        b = build_cycles([_s(ANCHOR - WEEK + i * 3600, float(i), email="b@x") for i in range(100)],
                         now=ANCHOR + 10)
        assert store.record(a) == 1 and store.record(b) == 1
        assert {c.email for c in store.all()} == {"a@x", "b@x"}

    def test_complete_cycles_filters_coverage_and_account(self, tmp_path):
        store = CycleStore(tmp_path)
        store.record([
            Cycle(email="a@x", window="7d", start=0, end=WEEK, peak_pct=83, final_pct=83,
                  samples=100, coverage=0.9),
            Cycle(email="a@x", window="7d", start=WEEK, end=2 * WEEK, peak_pct=10, final_pct=10,
                  samples=3, coverage=0.01),
            Cycle(email="b@x", window="7d", start=0, end=WEEK, peak_pct=100, final_pct=100,
                  samples=100, coverage=0.95),
        ])
        assert len(store.complete_cycles()) == 2
        assert [c.peak_pct for c in store.complete_cycles(email="a@x")] == [83.0]

    def test_corrupt_lines_skipped(self, tmp_path):
        store = CycleStore(tmp_path)
        store.path.write_text('{"not": "a cycle"}\nnot json\n', encoding="utf-8")
        assert store.all() == []

    def test_unspent_is_recorded(self, tmp_path):
        store = CycleStore(tmp_path)
        store.record([Cycle(email="a@x", window="7d", start=0, end=WEEK, peak_pct=83,
                            final_pct=83, samples=10, coverage=0.9)])
        raw = json.loads(store.path.read_text().splitlines()[0])
        assert raw["unspent_pct"] == 17.0


class TestUpdateFromHistory:
    def test_records_each_account_separately(self, tmp_path):
        history = UsageHistory(tmp_path)
        for email in ("a@x", "b@x"):
            for i in range(100):
                history.append(_s(ANCHOR - WEEK + i * 3600, float(i), email=email))
        store = CycleStore(tmp_path)
        assert update_from_history(history, store, ["a@x", "b@x"], now=ANCHOR + 10) == 2
        assert update_from_history(history, store, ["a@x", "b@x"], now=ANCHOR + 10) == 0

    def test_unknown_account_is_skipped(self, tmp_path):
        history = UsageHistory(tmp_path)
        store = CycleStore(tmp_path)
        assert update_from_history(history, store, ["nobody@x"], now=ANCHOR) == 0
