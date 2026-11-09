"""Reset detection from usage history, and the extra usage-API fields."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from claude_swap import oauth, resets, resets_cli
from claude_swap.switcher import _format_usage_lines
from claude_swap.usage_history import Sample

FIXTURE = Path(__file__).parent / "fixtures" / "oauth_usage_2026-11.json"
H = 3600.0
D = 86400.0
T0 = datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp()


def iso(t: float) -> str:
    # Sub-second noise like the real API's re-stamped boundaries.
    return datetime.fromtimestamp(t + 0.58, tz=timezone.utc).isoformat()


def s(t, *, d7=None, d7_due=None, h5=None, h5_due=None, scoped=None, email="a@x"):
    return Sample(
        t=T0 + t, email=email, org="o",
        five_hour=h5, five_hour_reset=iso(T0 + h5_due) if h5_due is not None else None,
        seven_day=d7, seven_day_reset=iso(T0 + d7_due) if d7_due is not None else None,
        scoped=scoped or {},
    )


def kinds(events, window="7d"):
    return [e.kind for e in events if e.window == window]


class TestDetect:
    def test_on_time_rollover_is_scheduled(self):
        ev = resets.detect([
            s(6 * D, d7=70, d7_due=7 * D),
            s(7 * D + 120, d7=0, d7_due=14 * D),
        ])
        assert kinds(ev) == ["scheduled"]
        assert not ev[0].unscheduled

    def test_jitter_in_resets_at_is_not_an_event(self):
        ev = resets.detect([
            s(1 * D, d7=10, d7_due=7 * D),
            s(1 * D + 120, d7=11, d7_due=7 * D + 30),
        ])
        assert ev == []

    def test_drop_to_zero_with_schedule_kept_is_early(self):
        # Shape seen on 28 Sep: 100% -> 0% two days out, resets_at unchanged.
        ev = resets.detect([
            s(5 * D, d7=100, d7_due=7 * D),
            s(5 * D + 180, d7=0, d7_due=7 * D),
        ])
        assert kinds(ev) == ["early"]
        assert abs(ev[0].early_by_s - (2 * D - 180)) < 1

    def test_reset_time_jump_with_drop_is_early(self):
        ev = resets.detect([
            s(1 * H, h5=100, h5_due=4 * H),
            s(1 * H + 180, h5=0, h5_due=6 * H),
        ])
        assert kinds(ev, "5h") == ["early"]

    def test_near_total_drop_after_gap_is_early(self):
        ev = resets.detect([
            s(1 * D, d7=83, d7_due=5 * D),
            s(1 * D + 17 * H, d7=3, d7_due=5 * D),
        ])
        assert kinds(ev) == ["early"]

    def test_meaningful_partial_drop_is_partial(self):
        ev = resets.detect([
            s(6 * D, d7=100, d7_due=7 * D),
            s(6 * D + 600, d7=69, d7_due=7 * D),
        ])
        assert kinds(ev) == ["partial"]

    def test_small_drop_is_noise(self):
        ev = resets.detect([
            s(1 * D, d7=40, d7_due=7 * D),
            s(1 * D + 600, d7=37, d7_due=7 * D),
        ])
        assert ev == []

    def test_schedule_shift_without_drop_is_moved(self):
        ev = resets.detect([
            s(1 * D, d7=40, d7_due=7 * D),
            s(1 * D + 600, d7=40, d7_due=7 * D + H),
        ])
        assert kinds(ev) == ["moved"]

    def test_scoped_drop_with_weekly_event_is_deduped(self):
        ev = resets.detect([
            s(5 * D, d7=100, d7_due=7 * D, scoped={"Fable": 73}),
            s(5 * D + 180, d7=0, d7_due=7 * D, scoped={"Fable": 1}),
        ])
        assert [(e.window, e.kind) for e in ev] == [("7d", "early")]

    def test_scoped_drop_alone_is_reported(self):
        ev = resets.detect([
            s(5 * D, d7=50, d7_due=7 * D, scoped={"Fable": 90}),
            s(5 * D + 180, d7=50, d7_due=7 * D, scoped={"Fable": 0}),
        ])
        assert [(e.window, e.kind) for e in ev] == [("Fable", "early")]

    def test_accounts_are_independent(self):
        ev = resets.detect([
            s(5 * D, d7=100, d7_due=7 * D, email="a@x"),
            s(5 * D + 60, d7=0, d7_due=7 * D, email="b@x"),
        ])
        assert ev == []

    def test_describe_and_json(self):
        ev = resets.detect([
            s(5 * D, d7=100, d7_due=7 * D),
            s(5 * D + 180, d7=0, d7_due=7 * D),
        ])
        text = resets.describe(ev[0])
        assert text.startswith("7d reset early ")
        assert "100% → 0%" in text and "before its" in text
        payload = ev[0].to_json()
        assert payload["kind"] == "early" and payload["earlyBySeconds"] > 0


class TestUsageDetails:
    def data(self) -> dict:
        return json.loads(FIXTURE.read_text())

    def test_fixture_is_the_live_shape(self):
        data = self.data()
        assert {"five_hour", "seven_day", "limits", "extra_usage", "seven_day_breakdown"} <= set(data)

    def test_details_from_live_response(self):
        details = oauth.build_usage_result(self.data())["details"]
        assert details["five_hour_idle"] is True
        assert details["extra_usage"]["enabled"] is False
        assert details["extra_usage"]["disabled_reason"] == "out_of_credits"
        assert details["extra_usage"]["limit"] == 135.0
        assert any(lim["active"] and lim["kind"] == "weekly_all" for lim in details["limits"])
        assert details["breakdown"]["window_started_at"]
        assert details["scoped_shares"][0]["name"] == "Fable"
        assert "other" not in details  # every codename key is null today

    def test_unknown_non_null_field_is_kept_verbatim(self):
        data = self.data()
        data["tangelo"] = {"utilization": 3, "resets_at": "2026-11-20T00:00:00+00:00"}
        details = oauth.build_usage_result(data)["details"]
        assert details["other"] == {"tangelo": data["tangelo"]}

    def test_quiet_response_adds_no_details(self):
        usage = oauth.build_usage_result(
            {"five_hour": {"utilization": 1.0, "resets_at": None},
             "seven_day": {"utilization": 2.0, "resets_at": None}}
        )
        assert "details" not in usage

    def test_list_lines_show_idle_overage_and_new_fields(self):
        data = self.data()
        data["seven_day"]["locked_reason"] = "abuse_review"
        data["tangelo"] = {"x": 1}
        lines = _format_usage_lines(oauth.build_usage_result(data))
        text = "\n".join(lines)
        assert "5h:" in text and "idle" in text
        assert "off (out of credits)" in text
        assert "locked: abuse_review" in text
        assert "'tangelo'" in text

    def test_resets_view_lines(self):
        lines = resets_cli._account_lines(oauth.build_usage_result(self.data()))
        text = "\n".join(lines)
        assert "idle" in text
        assert "window opened" in text
        assert "of weekly so far" in text
        assert "(binding)" in text
        assert "Claude Code 100%" in text
