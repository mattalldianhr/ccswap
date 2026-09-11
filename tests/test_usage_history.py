"""Tests for usage_history.py: append/dedupe/prune and auto-log backfill."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from claude_swap.usage_history import (
    Sample,
    UsageHistory,
    backfill_from_auto_log,
    parse_auto_log,
)


def _sample(t: float, email: str = "a@x", five: float = 10.0, seven: float = 20.0) -> Sample:
    return Sample(
        t=t, email=email, org="", five_hour=five, five_hour_reset=None,
        seven_day=seven, seven_day_reset=None, scoped={"Fable": 5.0},
    )


class TestAppend:
    def test_append_and_read_back(self, tmp_path):
        h = UsageHistory(tmp_path)
        assert h.append(_sample(100.0))
        assert h.append(_sample(160.0))
        got = h.samples("a@x")
        assert [s.t for s in got] == [100.0, 160.0]
        assert got[0].scoped == {"Fable": 5.0}

    def test_duplicate_t_is_skipped_per_account(self, tmp_path):
        h = UsageHistory(tmp_path)
        assert h.append(_sample(100.0, "a@x"))
        assert not h.append(_sample(100.0, "a@x"))
        assert h.append(_sample(100.0, "b@x"))  # other account, same t
        assert not h.append(_sample(90.0, "a@x"))  # older than last
        assert len(h.samples()) == 2

    def test_from_last_good_maps_windows(self):
        lg = {
            "five_hour": {"pct": 12, "resets_at": "2026-01-01T00:00:00+00:00"},
            "seven_day": {"pct": 34.5, "resets_at": "2026-01-07T00:00:00+00:00"},
            "scoped": [{"name": "Fable", "pct": 7}, {"name": "bad"}],
        }
        s = Sample.from_last_good("a@x", "org", lg, 5.0)
        assert (s.five_hour, s.seven_day) == (12.0, 34.5)
        assert s.five_hour_reset == "2026-01-01T00:00:00+00:00"
        assert s.scoped == {"Fable": 7.0}

    def test_torn_line_is_skipped(self, tmp_path):
        h = UsageHistory(tmp_path)
        h.append(_sample(100.0))
        with h.path.open("a") as fh:
            fh.write('{"t": 200.0, "email": "a@x"')  # no newline, truncated
        assert [s.t for s in h.samples()] == [100.0]

    def test_prune_drops_old_lines(self, tmp_path):
        h = UsageHistory(tmp_path)
        h.append(_sample(100.0))
        h.append(_sample(5_000_000.0))
        import time

        removed = h.prune(max_age_s=time.time() - 1_000_000.0)
        assert removed == 1
        assert [s.t for s in h.samples()] == [5_000_000.0]

    def test_append_many_respects_existing_tail(self, tmp_path):
        h = UsageHistory(tmp_path)
        h.append(_sample(300.0))
        n = h.append_many([_sample(100.0), _sample(400.0), _sample(350.0, "b@x")])
        assert n == 2
        assert [s.t for s in h.samples("a@x")] == [300.0, 400.0]

    def test_samples_since_filter(self, tmp_path):
        h = UsageHistory(tmp_path)
        for t in (10.0, 20.0, 30.0):
            h.append(_sample(t))
        assert [s.t for s in h.samples(since=20.0)] == [20.0, 30.0]


ACCOUNTS = {"1": ("one@x", "org1"), "2": ("two@x", "org2")}


class TestBackfill:
    def test_reconstructs_dates_across_midnight(self):
        lines = [
            "23:58:00  Account-2 (two@x): 30% used (switch at 85%) | others: #1: 5h 53% · 7d 9%",
            "00:00:01  Account-2 (two@x): 31% used (switch at 85%) | others: #1: 5h 54% · 7d 9%",
            "00:02:01  no switch: below-threshold (31% < 85%)",
        ]
        end = datetime(2026, 9, 11, 14, 0).astimezone()
        got = parse_auto_log(lines, ACCOUNTS, end_date=end)
        ones = [s for s in got if s.email == "one@x"]
        assert len(ones) == 2
        d0 = datetime.fromtimestamp(ones[0].t).astimezone()
        d1 = datetime.fromtimestamp(ones[1].t).astimezone()
        assert d1.date() == end.date()
        assert d0.date() == (end - timedelta(days=1)).date()
        assert (ones[0].five_hour, ones[0].seven_day) == (53.0, 9.0)

    def test_active_binding_becomes_5h_only_when_above_known_7d(self):
        lines = [
            # first, the active account 1 tells us #2's 7d is 40
            "10:00:00  Account-1 (one@x): 10% used (switch at 85%) | others: #2: 5h 0% · 7d 40%",
            # then account 2 becomes active with binding 70 (> 40 → it's the 5h value)
            "10:02:00  Account-2 (two@x): 70% used (switch at 85%) | others: #1: 5h 10% · 7d 5%",
            # binding 40 (== its 7d) → ambiguous, no 5h reading for account 2
            "10:04:00  Account-2 (two@x): 40% used (switch at 85%) | others: #1: 5h 10% · 7d 5%",
        ]
        end = datetime(2026, 9, 11, 12, 0).astimezone()
        got = parse_auto_log(lines, ACCOUNTS, end_date=end)
        twos = sorted((s for s in got if s.email == "two@x"), key=lambda s: s.t)
        assert [s.five_hour for s in twos] == [0.0, 70.0]
        assert twos[1].seven_day == 40.0

    def test_backfill_writes_and_is_idempotent(self, tmp_path):
        log = tmp_path / "auto.log"
        log.write_text(
            "10:00:00  Account-1 (one@x): 10% used (switch at 85%) | others: #2: 5h 1% · 7d 2%\n"
            "10:02:00  Account-1 (one@x): 11% used (switch at 85%) | others: #2: 5h 1% · 7d 2%\n"
        )
        h = UsageHistory(tmp_path / "cache")
        assert backfill_from_auto_log(h, log, ACCOUNTS) >= 2
        assert backfill_from_auto_log(h, log, ACCOUNTS) == 0
        assert all(s.src == "backfill" for s in h.samples())
        raw = json.loads(h.path.read_text().splitlines()[0])
        assert raw["src"] == "backfill"
