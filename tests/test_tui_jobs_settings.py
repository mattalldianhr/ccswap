"""Pilot tests for the Settings › Jobs submenu."""

from __future__ import annotations

import json

import pytest
from textual.widgets import Input, ListView

from claude_swap.settings import settings_path
from claude_swap.tui.widgets import MenuItem
from tests.test_tui import make_account, make_app, menu_select, settle
from tests.test_tui_jobs import JobsFakeSwitcher


@pytest.fixture
def fake(tmp_path):
    return JobsFakeSwitcher([make_account(1, active=True), make_account(2)], tmp_path)


def _labels(app) -> list[str]:
    menu = app.screen.query_one("#menu", ListView)
    return [item.action_id for item in menu.query(MenuItem)]


@pytest.mark.asyncio
class TestJobsSettingsMenu:
    async def test_submenu_lists_keys_and_edits_number(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "settings-menu")
            await menu_select(pilot, "jobs-settings-menu")
            ids = _labels(app)
            assert "jobsetting:jobs.quietMinutes" in ids and "jobs-daemon" in ids
            await menu_select(pilot, "jobsetting:jobs.quietMinutes")
            from claude_swap.tui.modals import ValueModal

            assert isinstance(app.screen, ValueModal)
            box = app.screen.query_one("#value", Input)
            box.value = "35"
            await pilot.press("enter")
            await settle(pilot)
            await pilot.pause()
            raw = json.loads(settings_path(tmp_path).read_text())
            assert raw["jobs"]["quietMinutes"] == 35.0

    async def test_choice_cycles_and_empty_resets(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "settings-menu")
            await menu_select(pilot, "jobs-settings-menu")
            await menu_select(pilot, "jobsetting:jobs.defaultPermissionMode")
            await settle(pilot)
            raw = json.loads(settings_path(tmp_path).read_text())
            assert raw["jobs"]["defaultPermissionMode"] == "auto"  # next after acceptEdits
            await menu_select(pilot, "jobsetting:jobs.reservePct")
            app.screen.query_one("#value", Input).value = "22"
            await pilot.press("enter")
            await settle(pilot)
            await pilot.pause()
            assert json.loads(settings_path(tmp_path).read_text())["jobs"]["reservePct"] == 22.0
            await menu_select(pilot, "jobsetting:jobs.reservePct")
            app.screen.query_one("#value", Input).value = ""
            await pilot.press("enter")
            await settle(pilot)
            await pilot.pause()
            assert "reservePct" not in json.loads(settings_path(tmp_path).read_text()).get("jobs", {})

    async def test_out_of_range_is_rejected_with_notice(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await menu_select(pilot, "settings-menu")
            await menu_select(pilot, "jobs-settings-menu")
            await menu_select(pilot, "jobsetting:jobs.maxConcurrent")
            app.screen.query_one("#value", Input).value = "99"
            await pilot.press("enter")
            await settle(pilot)
            await pilot.pause()
            assert not settings_path(tmp_path).exists() or "maxConcurrent" not in json.loads(
                settings_path(tmp_path).read_text()
            ).get("jobs", {})
