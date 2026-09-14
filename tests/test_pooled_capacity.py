"""Tests for pooled capacity across accounts with staggered resets."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from claude_swap.capacity import (
    AccountCapacity,
    WindowCapacity,
    next_refill,
    pool_window,
    pooled_capacity,
)

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc).timestamp()


def _w(window, used, *, forecast=0.0, reserve=10.0, resets_at=None, blackout=False):
    spare = None if used is None else 100.0 - used - forecast - (100.0 if blackout else reserve)
    return WindowCapacity(
        window=window, used_pct=used, resets_at=resets_at, remaining_s=None,
        recent_rate_pct_h=None, recent_forecast_pct=None, typical_forecast_pct=forecast,
        forecast_pct=forecast, reserve_pct=100.0 if blackout else reserve,
        reserve_source=None, spare_pct=spare, samples=0,
    )


def _acct(number, windows):
    return AccountCapacity(number=number, email=f"a{number}@x", windows=windows, usage_age_s=5.0)


class TestPoolWindow:
    def test_sums_remaining_and_counts_reserve_once(self):
        """Two accounts at 40% used each: 120 points remain, one 10% reserve."""
        caps = [
            _acct("1", {"7d": _w("7d", 40, reserve=10)}),
            _acct("2", {"7d": _w("7d", 40, reserve=10)}),
        ]
        pooled = pool_window(caps, "7d")
        assert pooled.remaining_pct == 120.0
        assert pooled.reserve_pct == 10.0          # once, not 20
        assert pooled.spare_pct == 110.0
        assert pooled.accounts == 2
        assert pooled.per_account == {"1": 60.0, "2": 60.0}

    def test_forecast_counted_once_not_summed(self):
        """One appetite: the largest single forecast, not their sum."""
        caps = [
            _acct("1", {"7d": _w("7d", 40, forecast=50, reserve=0)}),
            _acct("2", {"7d": _w("7d", 40, forecast=30, reserve=0)}),
        ]
        pooled = pool_window(caps, "7d")
        assert pooled.forecast_pct == 50.0
        assert pooled.spare_pct == 120.0 - 50.0

    def test_exhausted_account_still_contributes_zero(self):
        """Matt's real 2026-09-14 shape: one account spent, one half-used."""
        caps = [
            _acct("1", {"7d": _w("7d", 42, forecast=58, reserve=10)}),
            _acct("2", {"7d": _w("7d", 100, forecast=0, reserve=10)}),
        ]
        pooled = pool_window(caps, "7d")
        assert pooled.remaining_pct == 58.0
        assert pooled.per_account == {"1": 58.0, "2": 0.0}
        # Per-account both showed -10 spare; pooled shows the real headroom.
        assert pooled.spare_pct == pytest.approx(-10.0)
        assert pooled.best_single() == 58.0

    def test_unknown_usage_excluded(self):
        caps = [
            _acct("1", {"7d": _w("7d", 40)}),
            _acct("2", {"7d": _w("7d", None)}),
        ]
        pooled = pool_window(caps, "7d")
        assert pooled.accounts == 1 and pooled.remaining_pct == 60.0

    def test_missing_window_returns_none(self):
        assert pool_window([_acct("1", {"5h": _w("5h", 10)})], "7d") is None
        assert pool_window([], "7d") is None

    def test_blackout_only_when_every_account_is(self):
        both = [_acct("1", {"7d": _w("7d", 10, blackout=True)}),
                _acct("2", {"7d": _w("7d", 10, blackout=True)})]
        assert pool_window(both, "7d").blackout is True
        one = [_acct("1", {"7d": _w("7d", 10, blackout=True)}),
               _acct("2", {"7d": _w("7d", 10)})]
        assert pool_window(one, "7d").blackout is False


class TestStaggeredResets:
    def test_soonest_reset_is_reported(self):
        """Account 1 resets Wed 04:00, account 2 Wed 20:00 — 16h apart."""
        early, late = NOW + 16 * 3600, NOW + 32 * 3600
        caps = [
            _acct("1", {"7d": _w("7d", 42, resets_at=early)}),
            _acct("2", {"7d": _w("7d", 100, resets_at=late)}),
        ]
        pooled = pool_window(caps, "7d")
        assert pooled.next_reset == early and pooled.next_reset_account == "1"

    def test_next_refill_across_windows(self):
        caps = [
            _acct("1", {"5h": _w("5h", 20, resets_at=NOW + 3600),
                        "7d": _w("7d", 42, resets_at=NOW + 16 * 3600)}),
            _acct("2", {"5h": _w("5h", 0, resets_at=NOW + 7200),
                        "7d": _w("7d", 100, resets_at=NOW + 32 * 3600)}),
        ]
        pool = pooled_capacity(caps)
        when, window, account = next_refill(pool, ("5h", "7d"))
        assert window == "5h" and account == "1" and when == NOW + 3600
        # Restricted to the weekly window, the answer changes.
        when, window, account = next_refill(pool, ("7d",))
        assert window == "7d" and account == "1"

    def test_next_refill_none_without_reset_times(self):
        caps = [_acct("1", {"7d": _w("7d", 40)})]
        assert next_refill(pooled_capacity(caps), ("7d",)) is None
        assert next_refill({}, ("7d",)) is None


class TestPooledCapacity:
    def test_covers_every_window_including_scoped(self):
        caps = [
            _acct("1", {"5h": _w("5h", 16), "7d": _w("7d", 42), "Fable": _w("Fable", 26)}),
            _acct("2", {"5h": _w("5h", 0), "7d": _w("7d", 100)}),
        ]
        pool = pooled_capacity(caps)
        assert set(pool) == {"5h", "7d", "Fable"}
        assert pool["Fable"].accounts == 1          # only account 1 reports it
        assert pool["5h"].remaining_pct == 184.0

    def test_empty_input(self):
        assert pooled_capacity([]) == {}
