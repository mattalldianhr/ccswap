"""Tests for capacity.py: burn forecast and spare arithmetic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from claude_swap.capacity import (
    account_capacity,
    hourly_profile,
    recent_rate,
    typical_forecast,
    window_capacity,
    windows_for_job,
)
from claude_swap.reserves import Reserve, ReserveStore
from claude_swap.settings import JobsSettings
from claude_swap.usage_history import Sample, UsageHistory
from claude_swap.usage_store import UsageEntry

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _s(t: float, five: float | None, seven: float | None = 0.0, **scoped) -> Sample:
    return Sample(
        t=t, email="a@x", org="", five_hour=five, five_hour_reset=None,
        seven_day=seven, seven_day_reset=None, scoped={k: float(v) for k, v in scoped.items()},
    )


class TestRates:
    def test_recent_rate_needs_span(self):
        assert recent_rate([(NOW - 100, 1.0), (NOW, 2.0)], now=NOW) is None
        series = [(NOW - 3600, 10.0), (NOW - 1800, 15.0), (NOW, 20.0)]
        assert recent_rate(series, now=NOW) == 10.0  # 10 pct over 1h

    def test_recent_rate_ignores_resets(self):
        series = [(NOW - 3600, 90.0), (NOW - 1800, 2.0), (NOW, 6.0)]
        assert recent_rate(series, now=NOW) == 4.0

    def test_hourly_profile_and_typical(self):
        # 2 pct per 30 min during a single hour slot → 4 pct/h in that bucket.
        base = datetime(2026, 9, 7, 9, 0).astimezone()  # Monday 09:00 local
        t0 = base.timestamp()
        series = [(t0, 0.0), (t0 + 1800, 2.0), (t0 + 3600 - 1, 4.0)]
        profile = hourly_profile(series, lookback_s=7 * 86400, now=t0 + 3 * 86400)
        assert profile[(0, 9)] == [4.0]
        # Forecast one hour starting exactly at Monday 09:00 a week later
        later = (base + timedelta(days=7)).timestamp()
        total, n = typical_forecast(profile, now=later, remaining_s=3600, default_pct_h=0.0)
        assert round(total, 3) == 4.0 and n == 1
        # An hour with no data uses the default rate
        total, n = typical_forecast(profile, now=later + 3600, remaining_s=3600, default_pct_h=1.5)
        assert round(total, 3) == 1.5 and n == 0


class TestWindowCapacity:
    def test_spare_arithmetic_with_reserve(self, tmp_path):
        settings = JobsSettings(reserve_pct=15.0)
        reserves = ReserveStore(tmp_path)
        reserves.add(Reserve(id="r", window="5h", pct=30))
        # steady 5 pct/h over the last hour, 2h left → forecast 10
        samples = [_s(NOW - 3600, 40.0), _s(NOW - 1800, 42.5), _s(NOW, 45.0)]
        cap = window_capacity(
            window="5h", used_pct=45.0, resets_at=_iso(NOW + 7200), now=NOW,
            samples=samples, settings=settings, reserves=reserves, email="a@x",
            floor_pct=settings.reserve_pct,
        )
        assert cap.recent_rate_pct_h == 5.0
        assert cap.reserve_pct == 30.0 and cap.reserve_source.id == "r"
        assert cap.forecast_pct >= 10.0
        assert cap.spare_pct == 100 - 45 - cap.forecast_pct - 30
        assert cap.fits(cap.spare_pct) and not cap.fits(cap.spare_pct + 1)

    def test_blackout_never_fits(self, tmp_path):
        reserves = ReserveStore(tmp_path)
        reserves.add(Reserve(id="b", window="7d", pct=100))
        cap = window_capacity(
            window="7d", used_pct=0.0, resets_at=_iso(NOW + 86400), now=NOW,
            samples=[], settings=JobsSettings(), reserves=reserves, email="a@x", floor_pct=0.0,
        )
        assert cap.blackout and not cap.fits(0.0)

    def test_unknown_usage_gives_no_spare(self):
        cap = window_capacity(
            window="5h", used_pct=None, resets_at=None, now=NOW, samples=[],
            settings=JobsSettings(), reserves=None, email=None, floor_pct=10.0,
        )
        assert cap.spare_pct is None and cap.remaining_s is None

    def test_forecast_capped_at_remaining(self):
        samples = [_s(NOW - 3600, 50.0), _s(NOW, 95.0)]  # 45 pct/h
        cap = window_capacity(
            window="5h", used_pct=95.0, resets_at=_iso(NOW + 4 * 3600), now=NOW,
            samples=samples, settings=JobsSettings(), reserves=None, email=None, floor_pct=0.0,
        )
        assert cap.forecast_pct == 5.0 and cap.spare_pct == 0.0


class TestAccountCapacity:
    def _entry(self) -> UsageEntry:
        return UsageEntry(
            last_good={
                "five_hour": {"pct": 20.0, "resets_at": _iso(NOW + 3600)},
                "seven_day": {"pct": 50.0, "resets_at": _iso(NOW + 3 * 86400)},
                "scoped": [{"name": "Fable", "pct": 25.0, "resets_at": _iso(NOW + 3 * 86400)}],
            },
            fetched_at=NOW - 30,
            age_s=30.0,
        )

    def test_windows_present_and_scoped_scaled(self, tmp_path):
        history = UsageHistory(tmp_path)
        cap = account_capacity(
            number="1", email="a@x", entry=self._entry(), now=NOW, history=history,
            settings=JobsSettings(), reserves=None,
        )
        assert set(cap.windows) == {"5h", "7d", "Fable"}
        fable = cap.window("fable")
        weekly = cap.window("7d")
        assert fable is not None and weekly is not None
        # No Fable history: forecast is the weekly forecast scaled by used share (25/50).
        assert fable.samples == 0
        assert round(fable.forecast_pct, 6) == round(weekly.forecast_pct * 0.5, 6)

    def test_no_usage_yields_empty_windows(self, tmp_path):
        cap = account_capacity(
            number="1", email="a@x", entry=UsageEntry(sentinel="token expired"), now=NOW,
            history=UsageHistory(tmp_path), settings=JobsSettings(), reserves=None,
        )
        assert cap.windows == {} and cap.usage_error == "token expired"
        assert cap.spare_for(("5h",)) is None

    def test_windows_for_job(self):
        avail = {"5h": None, "7d": None, "Fable": None}
        assert windows_for_job(None, avail) == ("5h", "7d")
        assert windows_for_job("claude-fable-5-1", avail) == ("5h", "7d", "Fable")
        assert windows_for_job("opus", avail) == ("5h", "7d")
