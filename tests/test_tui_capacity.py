"""Pilot tests for the Capacity screen and its sparkline helper."""

from __future__ import annotations

import time

import pytest
from textual.widgets import Static

from claude_swap.jobs import Job, JobStore, new_job_id
from claude_swap.tui.capacity import sparkline
from claude_swap.usage_history import Sample
from tests.test_tui import make_account, make_app, make_entry, settle
from tests.test_tui_jobs import JobsFakeSwitcher


class TestSparkline:
    def test_buckets_and_scale(self):
        series = [(0.0, 0.0), (5.0, 50.0), (9.0, 100.0)]
        s = sparkline(series, start=0.0, end=10.0, columns=5)
        assert len(s) == 5
        assert s[0] == "▁" and s[2] == "▅" and s[4] == "█"
        assert s[1] == " " and s[3] == " "  # empty buckets stay blank

    def test_degenerate(self):
        assert sparkline([], start=0, end=0, columns=5) == ""
        assert sparkline([], start=0, end=10, columns=0) == ""


@pytest.fixture
def fake(tmp_path):
    return JobsFakeSwitcher(
        [
            make_account(1, active=True, entry=make_entry(10, 10, scoped=[("Fable", 40.0)])),
            make_account(2, entry=make_entry(60, 30)),
        ],
        tmp_path,
    )


async def _open(pilot):
    await settle(pilot)
    await pilot.press("b")
    await settle(pilot)
    await pilot.press("c")
    await settle(pilot)
    await pilot.pause()
    await settle(pilot)


@pytest.mark.asyncio
class TestCapacityScreen:
    async def test_renders_table_charts_and_fit_matrix(self, fake, tmp_path):
        store = JobStore(tmp_path)
        store.add(Job(id=new_job_id(), name="small", folder=str(tmp_path), prompt="p", estimate_pct=2.0))
        store.add(Job(id=new_job_id(), name="huge", folder=str(tmp_path), prompt="p", estimate_pct=99.0))
        now = time.time()
        history = fake._usage_store.history
        for i in range(6):
            history.append(Sample(
                t=now - 3600 * (6 - i), email="user1@example.com", org="",
                five_hour=float(i * 10), five_hour_reset=None, seven_day=float(i), seven_day_reset=None,
            ))
        app = make_app(fake)
        async with app.run_test(size=(130, 44)) as pilot:
            await _open(pilot)
            from claude_swap.tui.capacity import CapacityScreen

            assert isinstance(app.screen, CapacityScreen)
            title = str(app.screen.query_one("#cap-title", Static).render())
            assert "#1 user1@example.com" in title and "1/2" in title
            table = str(app.screen.query_one("#cap-table", Static).render())
            assert "5h" in table and "7d" in table and "spare" in table
            charts = str(app.screen.query_one("#cap-charts", Static).render())
            assert "last 24h" in charts and any(ch in charts for ch in "▁▂▃▄▅▆▇█")
            fit = str(app.screen.query_one("#cap-fit", Static).render())
            assert "small" in fit and "huge" in fit
            assert "yes" in fit and "no:" in fit

    async def test_paging_between_accounts(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 44)) as pilot:
            await _open(pilot)
            await pilot.press("right")
            await pilot.pause()
            title = str(app.screen.query_one("#cap-title", Static).render())
            assert "#2 user2@example.com" in title and "2/2" in title
            await pilot.press("right")
            await pilot.pause()
            assert "#1" in str(app.screen.query_one("#cap-title", Static).render())
            await pilot.press("escape")
            await pilot.pause()
            from claude_swap.tui.jobs import JobsScreen

            assert isinstance(app.screen, JobsScreen)

    async def test_error_is_shown_not_raised(self, fake, tmp_path):
        def boom(*a, **k):
            raise RuntimeError("store exploded")

        fake.usage_entries_by_account = boom
        app = make_app(fake)
        async with app.run_test(size=(130, 44)) as pilot:
            await _open(pilot)
            title = str(app.screen.query_one("#cap-title", Static).render())
            assert "store exploded" in title


@pytest.mark.asyncio
class TestPooledPane:
    async def test_pool_row_shows_combined_budget_and_refill(self, fake, tmp_path):
        app = make_app(fake)
        async with app.run_test(size=(130, 48)) as pilot:
            await _open(pilot)
            pool = str(app.screen.query_one("#cap-pool", Static).render())
            assert "pooled" in pool and "counted once" in pool
            assert "5h" in pool and "7d" in pool
            assert "best" in pool and "next refill" in pool

    async def test_pool_hidden_on_error(self, fake, tmp_path):
        fake.usage_entries_by_account = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
        app = make_app(fake)
        async with app.run_test(size=(130, 48)) as pilot:
            await _open(pilot)
            assert str(app.screen.query_one("#cap-pool", Static).render()).strip() == ""
