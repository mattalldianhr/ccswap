"""Pilot tests for the Antigravity screen and its bar helper."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from textual.widgets import Static

from claude_swap import antigravity
from claude_swap.tui import antigravity as screen_module
from claude_swap.antigravity import AntigravityError, parse_quota
from claude_swap.tui.antigravity import AntigravityScreen, bar
from tests.test_antigravity import QUOTA_PAYLOAD
from tests.test_tui import make_account, make_app, make_entry, settle
from tests.test_tui_jobs import JobsFakeSwitcher


class TestBar:
    def test_fills_proportionally_at_a_fixed_width(self):
        assert bar(0, width=10) == "░" * 10
        assert bar(100, width=10) == "█" * 10
        assert bar(50, width=10) == "█" * 5 + "░" * 5

    def test_out_of_range_values_are_clamped(self):
        assert bar(-20, width=4) == "░" * 4
        assert bar(500, width=4) == "█" * 4


@pytest.fixture
def fake(tmp_path):
    return JobsFakeSwitcher(
        [make_account(1, active=True, entry=make_entry(10, 10))], tmp_path
    )


def _usage(payload=None, email="matt@example.com"):
    return parse_quota(payload or QUOTA_PAYLOAD, email=email, now=time.time())


async def _open(pilot):
    await settle(pilot)
    await pilot.press("y")
    await settle(pilot)
    await pilot.pause()
    await settle(pilot)


@pytest.mark.asyncio
class TestAntigravityScreen:
    async def test_renders_every_group_and_window(self, fake):
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage()):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                assert isinstance(app.screen, AntigravityScreen)
                title = str(app.screen.query_one("#agy-title", Static).render())
                assert "antigravity" in title and "matt@example.com" in title
                groups = str(app.screen.query_one("#agy-groups", Static).render())
                assert "Gemini Models" in groups and "Claude and GPT models" in groups
                assert "5h" in groups and "Weekly" in groups
                assert "█" in groups or "░" in groups

    async def test_the_claude_group_is_flagged(self, fake):
        """The reason the screen exists: Claude capacity on a separate budget."""
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage()):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                groups = str(app.screen.query_one("#agy-groups", Static).render())
                assert "serves Claude models" in groups

    async def test_utilization_is_shown_not_the_remaining_fraction(self, fake):
        """The API reports what is left; every ccswap surface reports used."""
        payload = {"groups": [{"displayName": "G", "buckets": [
            {"bucketId": "x", "window": "5h", "remainingFraction": 0.25}]}]}
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage(payload)):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                groups = str(app.screen.query_one("#agy-groups", Static).render())
                assert "75.0% used" in groups

    async def test_an_unnamed_window_is_still_shown(self, fake):
        """Never silently drop quota the user is being charged for."""
        payload = {"groups": [{"displayName": "G", "buckets": [
            {"bucketId": "x", "window": "monthly", "remainingFraction": 0.5}]}]}
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage(payload)):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                assert "monthly" in str(app.screen.query_one("#agy-groups", Static).render())

    async def test_a_missing_login_is_shown_not_raised(self, fake):
        app = make_app(fake)
        with patch.object(screen_module, "read_usage",
                          side_effect=AntigravityError("no Antigravity login found")):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                assert isinstance(app.screen, AntigravityScreen)
                assert "unavailable" in str(app.screen.query_one("#agy-title", Static).render())
                groups = str(app.screen.query_one("#agy-groups", Static).render())
                assert "no Antigravity login found" in groups
                assert "agy" in str(app.screen.query_one("#agy-note", Static).render())

    async def test_an_unexpected_failure_does_not_crash_the_app(self, fake):
        """An undocumented endpoint on another tool's login may break in any
        way; it must never take down a TUI showing healthy Claude data."""
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", side_effect=RuntimeError("boom")):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                assert isinstance(app.screen, AntigravityScreen)
                assert "boom" in str(app.screen.query_one("#agy-groups", Static).render())

    async def test_escape_returns_to_the_dashboard(self, fake):
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage()):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                await pilot.press("escape")
                await pilot.pause()
                assert not isinstance(app.screen, AntigravityScreen)

    async def test_refresh_refetches(self, fake):
        app = make_app(fake)
        with patch.object(screen_module, "read_usage", return_value=_usage()) as read:
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
                before = read.call_count
                await pilot.press("f")
                await settle(pilot)
                await pilot.pause()
                await settle(pilot)
                assert read.call_count > before

    async def test_the_keychain_is_read_off_the_ui_thread(self, fake):
        """A wedged Keychain or a slow request must not freeze the UI."""
        seen = {}

        def slow(*args, **kwargs):
            import threading

            seen["thread"] = threading.current_thread().name
            return _usage()

        app = make_app(fake)
        with patch.object(screen_module, "read_usage", side_effect=slow):
            async with app.run_test(size=(130, 44)) as pilot:
                await _open(pilot)
        assert seen["thread"] != "MainThread"


@pytest.mark.asyncio
class TestMenuEntry:
    async def test_offered_when_a_login_exists(self, fake):
        from claude_swap.tui import dashboard

        app = make_app(fake)
        with patch.object(dashboard, "_antigravity_available", return_value=True):
            async with app.run_test(size=(130, 44)) as pilot:
                await settle(pilot)
                labels = [label for label, _ in app.screen._root_entries()]
        assert any("Antigravity" in label for label in labels)

    async def test_hidden_without_a_login(self, fake):
        """Noise for the many users who have no Antigravity login."""
        from claude_swap.tui import dashboard

        app = make_app(fake)
        with patch.object(dashboard, "_antigravity_available", return_value=False):
            async with app.run_test(size=(130, 44)) as pilot:
                await settle(pilot)
                labels = [label for label, _ in app.screen._root_entries()]
        assert not any("Antigravity" in label for label in labels)

    async def test_an_unusable_keychain_hides_the_entry_without_raising(self):
        from claude_swap.tui.dashboard import _antigravity_available

        with patch.object(antigravity, "available", side_effect=RuntimeError("locked")):
            assert _antigravity_available() is False
